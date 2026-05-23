"""Local-debug Storage RBAC assignment helper.

Responsibility: Local-debug Storage RBAC assignment helper
Edit boundaries: Keep reusable Azure RBAC write logic here; routes should only validate and
shape HTTP responses.
Key entry points: `grant_local_debug_storage_roles`, `local_debug_credential_principal`
Risky contracts: Never run in deployed environments; callers must keep the Container App guard
before invoking this helper.
Validation: `uv run pytest -q api/tests/test_storage_local_rbac.py`.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.sanitise import sanitise

STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
STORAGE_TABLE_DATA_CONTRIBUTOR_ROLE_ID = "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3"
STORAGE_ACCOUNT_CONTRIBUTOR_ROLE_ID = "17d1049b-9a84-46fb-8f53-869881c3d3ab"

LOCAL_DEBUG_STORAGE_ROLES: tuple[tuple[str, str], ...] = (
    ("Storage Blob Data Contributor", STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID),
    ("Storage Table Data Contributor", STORAGE_TABLE_DATA_CONTRIBUTOR_ROLE_ID),
    ("Storage Account Contributor", STORAGE_ACCOUNT_CONTRIBUTOR_ROLE_ID),
)


def local_debug_credential_principal(
    credential: TokenCredential,
) -> tuple[str, str]:
    """Return the object id and principal type behind the local Azure credential."""

    token = credential.get_token("https://management.azure.com/.default").token
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("Azure credential token is not a JWT")
    payload_segment = parts[1]
    payload_segment += "=" * (-len(payload_segment) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_segment).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Azure credential token payload is not readable") from exc
    principal_id = str(payload.get("oid") or "").strip()
    if not principal_id:
        raise ValueError("Azure credential token does not contain an oid claim")
    try:
        principal_id = str(uuid.UUID(principal_id))
    except ValueError as exc:
        raise ValueError("Azure credential token oid claim is not a GUID") from exc
    principal_type = (
        "ServicePrincipal"
        if payload.get("appid") and not (payload.get("upn") or payload.get("preferred_username"))
        else "User"
    )
    return principal_id, principal_type


def _storage_scope(subscription_id: str, resource_group: str, account_name: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{account_name}"
    )


def _is_existing_assignment(exc: Exception) -> bool:
    text = str(exc)
    return "Conflict" in text or "RoleAssignmentExists" in text


def grant_local_debug_storage_roles(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    principal_id: str,
    *,
    principal_type: str = "User",
) -> dict[str, Any]:
    """Grant the local API Azure credential the Storage roles needed for local debug."""

    from azure.mgmt.authorization import AuthorizationManagementClient

    scope = _storage_scope(subscription_id, resource_group, account_name)
    auth_client = AuthorizationManagementClient(credential, subscription_id)
    roles: list[dict[str, str]] = []
    created = 0
    failed = 0

    for role_name, role_definition_id in LOCAL_DEBUG_STORAGE_ROLES:
        assignment_name = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}:{principal_id}:{role_definition_id}")
        )
        role_definition_resource_id = (
            f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
            f"roleDefinitions/{role_definition_id}"
        )
        try:
            auth_client.role_assignments.create(
                scope=scope,
                role_assignment_name=assignment_name,
                parameters={
                    "role_definition_id": role_definition_resource_id,
                    "principal_id": principal_id,
                    "principal_type": principal_type,
                },
            )
        except Exception as exc:
            if _is_existing_assignment(exc):
                roles.append({"name": role_name, "status": "already_assigned"})
                continue
            failed += 1
            roles.append(
                {
                    "name": role_name,
                    "status": "failed",
                    "error": sanitise(str(exc))[:300],
                }
            )
            continue
        created += 1
        roles.append({"name": role_name, "status": "assigned"})

    if failed == len(LOCAL_DEBUG_STORAGE_ROLES):
        action = "failed"
    elif failed:
        action = "partial"
    elif created:
        action = "assigned"
    else:
        action = "already_assigned"

    return {
        "action": action,
        "principal_type": principal_type,
        "roles": roles,
    }

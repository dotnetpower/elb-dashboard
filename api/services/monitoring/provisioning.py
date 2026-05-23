"""Idempotent ARM provisioning (storage + ACR) + role assignment.

Responsibility: Idempotent ARM provisioning (storage + ACR) + role assignment.
Edit boundaries: Keep reusable domain logic here; routes and tasks call this layer.
Key entry points: `ensure_storage_account`, `ensure_acr` (+ helpers).
Risky contracts: Keep Azure credentials centralized and sanitise data at HTTP/log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import os

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError

from api.services.azure_clients import acr_client, storage_client
from api.services.storage.network import ensure_workload_storage_private_endpoints

LOGGER = logging.getLogger(__name__)

STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
ACR_PULL_ROLE_ID = "8311e382-0749-4cb8-b61a-304f252e45ec"


def _current_managed_identity_principal_id() -> str:
    """Return the current app UAMI principal id when the deployment injected it."""

    return os.environ.get("SHARED_IDENTITY_PRINCIPAL_ID", "").strip()


def _auto_assign_role(
    credential: TokenCredential,
    subscription_id: str,
    principal_id: str,
    scope: str,
    role_definition_id: str,
    principal_type: str = "User",
) -> bool:
    """Assign a role to a principal. Idempotent — ignores conflict."""

    import uuid as _uuid

    from azure.mgmt.authorization import AuthorizationManagementClient

    auth_client = AuthorizationManagementClient(credential, subscription_id)
    role_def = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{role_definition_id}"
    )
    assignment_name = str(
        _uuid.uuid5(_uuid.NAMESPACE_URL, f"{scope}:{principal_id}:{role_definition_id}")
    )
    try:
        auth_client.role_assignments.create(
            scope,
            assignment_name,
            {
                "role_definition_id": role_def,
                "principal_id": principal_id,
                "principal_type": principal_type,
            },
        )
        LOGGER.info(
            "RBAC assigned role=%s principal=%s scope=%s",
            role_definition_id[:8],
            principal_id[:8],
            scope.split("/")[-1],
        )
        return True
    except Exception as exc:
        if "Conflict" in str(exc) or "RoleAssignmentExists" in str(exc):
            LOGGER.debug("Role already assigned, skipping")
            return True
        else:
            LOGGER.warning("RBAC assignment failed: %s", str(exc)[:200])
            return False


def ensure_storage_account(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    region: str,
    caller_oid: str = "",
    private_endpoint_subnet_id: str = "",
    private_dns_zone_resource_group: str = "",
) -> None:
    """Create a Standard_LRS HNS-enabled storage account and default containers."""

    client = storage_client(credential, subscription_id)
    LOGGER.info("ensure_storage_account account=%s rg=%s", account_name, resource_group)
    try:
        client.storage_accounts.get_properties(resource_group, account_name)
    except ResourceNotFoundError:
        poller = client.storage_accounts.begin_create(
            resource_group,
            account_name,
            {
                "location": region,
                "sku": {"name": "Standard_LRS"},
                "kind": "StorageV2",
                "is_hns_enabled": True,
                "public_network_access": "Disabled",
                "minimum_tls_version": "TLS1_2",
                "tags": {"managed-by": "elb-dashboard"},
            },
        )
        poller.result()

    for container_name in ("blast-db", "queries", "results"):
        try:
            client.blob_containers.create(resource_group, account_name, container_name, {})
        except Exception:  # noqa: S110 - container may already exist
            pass

    storage_scope = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{account_name}"
    )

    if caller_oid:
        _auto_assign_role(
            credential,
            subscription_id,
            caller_oid,
            storage_scope,
            STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID,
            principal_type="User",
        )

    uami_principal_id = _current_managed_identity_principal_id()
    if uami_principal_id:
        assigned = _auto_assign_role(
            credential,
            subscription_id,
            uami_principal_id,
            storage_scope,
            STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID,
            principal_type="ServicePrincipal",
        )
        if not assigned:
            raise RuntimeError(
                "failed to assign Storage Blob Data Contributor to the shared "
                "managed identity; grant the control-plane identity User Access "
                "Administrator on the Storage scope or assign the Blob role manually"
            )

    ensure_workload_storage_private_endpoints(
        credential,
        subscription_id,
        resource_group,
        account_name,
        region,
        private_endpoint_subnet_id,
        private_dns_zone_resource_group,
    )


def ensure_acr(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
    region: str,
    caller_oid: str = "",
) -> None:
    """Create a Standard SKU ACR and assign caller RBAC when requested."""

    client = acr_client(credential, subscription_id)
    LOGGER.info("ensure_acr registry=%s rg=%s", registry_name, resource_group)
    poller = client.registries.begin_create(
        resource_group,
        registry_name,
        {
            "location": region,
            "sku": {"name": "Standard"},
            "admin_user_enabled": False,
            "tags": {"managed-by": "elb-dashboard"},
        },
    )
    poller.result()

    if caller_oid:
        _auto_assign_role(
            credential,
            subscription_id,
            caller_oid,
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.ContainerRegistry/registries/{registry_name}",
            ACR_PULL_ROLE_ID,
        )

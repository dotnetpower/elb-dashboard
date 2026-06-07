"""Tests for local-debug Storage RBAC assignment.

Responsibility: Tests for local-debug Storage RBAC assignment
Edit boundaries: Keep assertions focused on RBAC request shaping and idempotent conflict
handling; do not call live Azure.
Key entry points: `test_grant_local_debug_storage_roles_creates_expected_assignments`,
`test_grant_local_debug_storage_roles_treats_conflict_as_already_assigned`,
`test_storage_local_debug_grant_rbac_rejects_dev_bypass`
Risky contracts: Never require network access or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_storage_local_rbac.py`.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from api.auth import CallerIdentity
from api.routes.storage import local_debug
from api.services.storage import local_rbac
from fastapi import HTTPException


class FakeAccessToken:
    def __init__(self, token: str) -> None:
        self.token = token


class FakeCredential:
    def __init__(self, claims: dict[str, Any]) -> None:
        self.claims = claims

    def get_token(self, _scope: str) -> FakeAccessToken:
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
        payload = (
            base64.urlsafe_b64encode(json.dumps(self.claims).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        return FakeAccessToken(f"{header}.{payload}.")


class FakeRoleAssignments:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    def create(
        self,
        *,
        scope: str,
        role_assignment_name: str,
        parameters: dict[str, Any],
    ) -> None:
        self.calls.append(
            {
                "scope": scope,
                "role_assignment_name": role_assignment_name,
                "parameters": parameters,
            }
        )
        if self.fail_with is not None:
            raise self.fail_with


def test_grant_local_debug_storage_roles_creates_expected_assignments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_assignments = FakeRoleAssignments()

    class Client:
        def __init__(self, _credential: object, _subscription_id: str) -> None:
            self.role_assignments = fake_assignments

    monkeypatch.setattr(
        "azure.mgmt.authorization.AuthorizationManagementClient",
        Client,
        raising=True,
    )

    result = local_rbac.grant_local_debug_storage_roles(
        object(),
        "00000000-0000-0000-0000-0000000000a1",
        "rg-elb-dashboard",
        "stelbdashboardtest01",
        "00000000-0000-0000-0000-0000000000b2",
    )

    assert result["action"] == "assigned"
    assert "scope" not in result
    assert "principal_id" not in result
    assert result["principal_type"] == "User"
    assert [role["name"] for role in result["roles"]] == [
        "Storage Blob Data Contributor",
        "Storage Table Data Contributor",
        "Storage Account Contributor",
    ]
    assert len(fake_assignments.calls) == 3
    for call in fake_assignments.calls:
        assert call["scope"].endswith(
            "/resourceGroups/rg-elb-dashboard/providers/Microsoft.Storage/"
            "storageAccounts/stelbdashboardtest01"
        )
        assert call["parameters"]["principal_id"] == (
            "00000000-0000-0000-0000-0000000000b2"
        )
        assert call["parameters"]["principal_type"] == "User"


def test_grant_local_debug_storage_roles_treats_conflict_as_already_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_assignments = FakeRoleAssignments()
    fake_assignments.fail_with = RuntimeError("RoleAssignmentExists")

    class Client:
        def __init__(self, _credential: object, _subscription_id: str) -> None:
            self.role_assignments = fake_assignments

    monkeypatch.setattr(
        "azure.mgmt.authorization.AuthorizationManagementClient",
        Client,
        raising=True,
    )

    result = local_rbac.grant_local_debug_storage_roles(
        object(),
        "00000000-0000-0000-0000-0000000000a1",
        "rg-elb-dashboard",
        "stelbdashboardtest01",
        "00000000-0000-0000-0000-0000000000b2",
    )

    assert result["action"] == "already_assigned"
    assert {role["status"] for role in result["roles"]} == {"already_assigned"}


def test_local_debug_credential_principal_reads_user_oid() -> None:
    principal_id, principal_type = local_rbac.local_debug_credential_principal(
        FakeCredential(
            {
                "oid": "00000000-0000-0000-0000-0000000000b2",
                "preferred_username": "admin@example.test",
            }
        )
    )

    assert principal_id == "00000000-0000-0000-0000-0000000000b2"
    assert principal_type == "User"


def test_local_debug_credential_principal_detects_service_principal() -> None:
    principal_id, principal_type = local_rbac.local_debug_credential_principal(
        FakeCredential(
            {
                "oid": "3f06c475-95ee-45f3-85e8-751f740e123f",
                "appid": "e4f4e63d-2fe7-4f48-99c1-3e1f762827b5",
            }
        )
    )

    assert principal_id == "3f06c475-95ee-45f3-85e8-751f740e123f"
    assert principal_type == "ServicePrincipal"


def test_local_debug_credential_principal_rejects_non_guid_oid() -> None:
    with pytest.raises(ValueError, match="not a GUID"):
        local_rbac.local_debug_credential_principal(FakeCredential({"oid": "not-guid"}))


def _caller(object_id: str) -> CallerIdentity:
    return CallerIdentity(
        object_id=object_id,
        tenant_id="tenant",
        upn="admin@example.test",
        raw_token="token",
        claims={},
    )


def _rbac_body() -> dict[str, str]:
    return {
        "subscription_id": "00000000-0000-0000-0000-0000000000a1",
        "resource_group": "rg-elb-dashboard",
        "account_name": "stelbdashboardtest01",
    }


def test_storage_local_debug_grant_rbac_rejects_dev_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "api.services.storage.public_access.is_running_locally",
        lambda: True,
        raising=True,
    )
    monkeypatch.setattr(local_debug, "get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.local_rbac.local_debug_credential_principal",
        lambda _credential: ("00000000-0000-0000-0000-0000000000b2", "User"),
        raising=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        local_debug.storage_local_debug_grant_rbac(
            _rbac_body(),
            caller=_caller("00000000-0000-0000-0000-000000000000"),
        )

    assert exc_info.value.status_code == 400
    assert "real MSAL auth" in exc_info.value.detail


def test_storage_local_debug_grant_rbac_rejects_mismatched_local_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "api.services.storage.public_access.is_running_locally",
        lambda: True,
        raising=True,
    )
    monkeypatch.setattr(local_debug, "get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.local_rbac.local_debug_credential_principal",
        lambda _credential: ("00000000-0000-0000-0000-0000000000b2", "User"),
        raising=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        local_debug.storage_local_debug_grant_rbac(
            _rbac_body(),
            caller=_caller("aaaaaaaa-2ef5-48f7-b57e-5e969b65f916"),
        )

    assert exc_info.value.status_code == 403
    assert "match" in exc_info.value.detail

"""Storage provisioning RBAC tests.

Responsibility: Cover role assignment side effects for Storage onboarding.
Edit boundaries: Keep these tests focused on api.services.monitoring.ensure_storage_account RBAC
behaviour; broader Azure SDK integration belongs in smoke tests.
Key entry points: `test_ensure_storage_assigns_blob_rbac_to_caller_and_uami`,
`test_ensure_storage_skips_uami_assignment_without_principal_env`,
`test_ensure_storage_fails_closed_when_uami_rbac_assignment_fails`
Risky contracts: The api/worker sidecars read workload Storage through the shared UAMI, not the
browser caller, so Storage onboarding must grant that UAMI data-plane RBAC.
Validation: `uv run pytest -q api/tests/test_monitoring_storage_rbac.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services import monitoring


class _StorageAccounts:
    def get_properties(self, resource_group: str, account_name: str) -> object:
        return object()


class _BlobContainers:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []

    def create(
        self,
        resource_group: str,
        account_name: str,
        container_name: str,
        parameters: dict[str, Any],
    ) -> None:
        self.created.append((resource_group, account_name, container_name))


class _StorageClient:
    def __init__(self) -> None:
        self.storage_accounts = _StorageAccounts()
        self.blob_containers = _BlobContainers()


def test_ensure_storage_assigns_blob_rbac_to_caller_and_uami(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    def fake_assign(
        credential: object,
        subscription_id: str,
        principal_id: str,
        scope: str,
        role_definition_id: str,
        principal_type: str = "User",
    ) -> bool:
        calls.append(
            {
                "subscription_id": subscription_id,
                "principal_id": principal_id,
                "scope": scope,
                "role_definition_id": role_definition_id,
                "principal_type": principal_type,
            }
        )
        return True

    monkeypatch.setattr(monitoring, "storage_client", lambda *_args: _StorageClient())
    monkeypatch.setattr(monitoring, "_auto_assign_role", fake_assign)
    monkeypatch.setattr(
        monitoring,
        "ensure_workload_storage_private_endpoints",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "uami-principal-id")

    monitoring.ensure_storage_account(
        object(),
        "sub-123",
        "rg-elb-dashboard",
        "stelbdashboard",
        "koreacentral",
        caller_oid="caller-object-id",
    )

    scope = (
        "/subscriptions/sub-123/resourceGroups/rg-elb-dashboard"
        "/providers/Microsoft.Storage/storageAccounts/stelbdashboard"
    )
    assert calls == [
        {
            "subscription_id": "sub-123",
            "principal_id": "caller-object-id",
            "scope": scope,
            "role_definition_id": monitoring.STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID,
            "principal_type": "User",
        },
        {
            "subscription_id": "sub-123",
            "principal_id": "uami-principal-id",
            "scope": scope,
            "role_definition_id": monitoring.STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID,
            "principal_type": "ServicePrincipal",
        },
    ]


def test_ensure_storage_skips_uami_assignment_without_principal_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_assign(
        credential: object,
        subscription_id: str,
        principal_id: str,
        scope: str,
        role_definition_id: str,
        principal_type: str = "User",
    ) -> bool:
        calls.append(principal_id)
        return True

    monkeypatch.setattr(monitoring, "storage_client", lambda *_args: _StorageClient())
    monkeypatch.setattr(monitoring, "_auto_assign_role", fake_assign)
    monkeypatch.setattr(
        monitoring,
        "ensure_workload_storage_private_endpoints",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.delenv("SHARED_IDENTITY_PRINCIPAL_ID", raising=False)

    monitoring.ensure_storage_account(
        object(),
        "sub-123",
        "rg-elb-dashboard",
        "stelbdashboard",
        "koreacentral",
        caller_oid="caller-object-id",
    )

    assert calls == ["caller-object-id"]


def test_ensure_storage_fails_closed_when_uami_rbac_assignment_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_assign(
        credential: object,
        subscription_id: str,
        principal_id: str,
        scope: str,
        role_definition_id: str,
        principal_type: str = "User",
    ) -> bool:
        return principal_type == "User"

    monkeypatch.setattr(monitoring, "storage_client", lambda *_args: _StorageClient())
    monkeypatch.setattr(monitoring, "_auto_assign_role", fake_assign)
    monkeypatch.setattr(
        monitoring,
        "ensure_workload_storage_private_endpoints",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "uami-principal-id")

    with pytest.raises(RuntimeError, match="shared managed identity"):
        monitoring.ensure_storage_account(
            object(),
            "sub-123",
            "rg-elb-dashboard",
            "stelbdashboard",
            "koreacentral",
            caller_oid="caller-object-id",
        )

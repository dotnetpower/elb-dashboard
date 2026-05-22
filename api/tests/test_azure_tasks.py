"""Tests for Azure Tasks behavior.

Responsibility: Tests for Azure Tasks behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_attach_acr_uses_subscription_scoped_role_definition`,
`test_grant_storage_blob_contributor_uses_storage_scope`,
`test_ensure_aks_runtime_rbac_grants_acr_and_storage`,
`test_ensure_aks_runtime_rbac_reports_nonfatal_failures`,
`test_start_aks_enqueues_openapi_after_cluster_start`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_azure_tasks.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from api.tasks import azure
from api.tests._fakes import AsyncResultStub


def test_attach_acr_uses_subscription_scoped_role_definition(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeAksClient:
        managed_clusters = SimpleNamespace(
            get=lambda _resource_group, _cluster_name: SimpleNamespace(
                identity_profile={"kubeletidentity": SimpleNamespace(object_id="kubelet-oid")}
            )
        )

    class FakeAcrClient:
        registries = SimpleNamespace(
            get=lambda _resource_group, _name: SimpleNamespace(
                id="/subscriptions/sub-1/resourceGroups/rg-acr/providers/Microsoft.ContainerRegistry/registries/acr1"
            )
        )

    class FakeRoleAssignments:
        def create(
            self, *, scope: str, role_assignment_name: str, parameters: dict[str, Any]
        ) -> None:
            captured["scope"] = scope
            captured["role_assignment_name"] = role_assignment_name
            captured["parameters"] = parameters

    class FakeAuthorizationClient:
        def __init__(self, _cred: object, _subscription_id: str) -> None:
            self.role_assignments = FakeRoleAssignments()

    import azure.mgmt.authorization as auth_mod

    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "acr_client", lambda _cred, _sub: FakeAcrClient())
    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", FakeAuthorizationClient)

    azure._attach_acr(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        "rg-acr",
        "acr1",
    )

    assert captured["scope"].endswith("/registries/acr1")
    params = captured["parameters"]
    assert params.principal_id == "kubelet-oid"
    assert params.role_definition_id == (
        "/subscriptions/sub-1/providers/Microsoft.Authorization/roleDefinitions/"
        "7f951dda-4ed3-4680-a7ca-43fe172d538d"
    )


def test_grant_storage_blob_contributor_uses_storage_scope(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeAksClient:
        managed_clusters = SimpleNamespace(
            get=lambda _resource_group, _cluster_name: SimpleNamespace(
                identity_profile={"kubeletidentity": SimpleNamespace(object_id="kubelet-oid")}
            )
        )

    class FakeStorageAccounts:
        def get_properties(self, _resource_group: str, _account_name: str) -> SimpleNamespace:
            return SimpleNamespace(
                id="/subscriptions/sub-1/resourceGroups/rg-storage/providers/Microsoft.Storage/storageAccounts/stg1"
            )

    class FakeStorageClient:
        storage_accounts = FakeStorageAccounts()

    class FakeRoleAssignments:
        def create(self, *, scope: str, role_assignment_name: str, parameters: object) -> None:
            captured["scope"] = scope
            captured["role_assignment_name"] = role_assignment_name
            captured["parameters"] = parameters

    class FakeAuthorizationClient:
        def __init__(self, _cred: object, _subscription_id: str) -> None:
            self.role_assignments = FakeRoleAssignments()

    import azure.mgmt.authorization as auth_mod

    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr(azure, "storage_client", lambda _cred, _sub: FakeStorageClient())
    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", FakeAuthorizationClient)

    azure._grant_storage_blob_contributor_to_aks(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        "rg-storage",
        "stg1",
    )

    assert captured["scope"].endswith("/storageAccounts/stg1")
    params = captured["parameters"]
    assert params.principal_id == "kubelet-oid"
    assert params.role_definition_id == (
        "/subscriptions/sub-1/providers/Microsoft.Authorization/roleDefinitions/"
        "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
    )


def test_ensure_aks_runtime_rbac_grants_acr_and_storage(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_attach_acr(
        _cred: object,
        _subscription_id: str,
        _resource_group: str,
        _cluster_name: str,
        acr_resource_group: str,
        acr_name: str,
    ) -> None:
        calls.append(("acr", acr_resource_group, acr_name))

    def fake_grant_storage(
        _cred: object,
        _subscription_id: str,
        _resource_group: str,
        _cluster_name: str,
        storage_resource_group: str,
        storage_account: str,
    ) -> None:
        calls.append(("storage", storage_resource_group, storage_account))

    monkeypatch.setattr(azure, "_attach_acr", fake_attach_acr)
    monkeypatch.setattr(azure, "_grant_storage_blob_contributor_to_aks", fake_grant_storage)

    summary = azure._ensure_aks_runtime_rbac(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        acr_resource_group="rg-acr",
        acr_name="acr1",
        storage_resource_group="rg-storage",
        storage_account="stg1",
    )

    assert calls == [("acr", "rg-acr", "acr1"), ("storage", "rg-storage", "stg1")]
    assert summary["roles_assigned"] == ["AcrPull", "Storage Blob Data Contributor"]
    assert summary["roles_failed"] == {}


def test_ensure_aks_runtime_rbac_reports_nonfatal_failures(monkeypatch) -> None:
    def fail_attach_acr(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("missing roleAssignments/write")

    monkeypatch.setattr(azure, "_attach_acr", fail_attach_acr)
    monkeypatch.setattr(
        azure, "_grant_storage_blob_contributor_to_aks", lambda *_args, **_kwargs: None
    )

    summary = azure._ensure_aks_runtime_rbac(
        object(),
        "sub-1",
        "rg-aks",
        "aks1",
        acr_resource_group="rg-acr",
        acr_name="acr1",
        storage_resource_group="rg-storage",
        storage_account="stg1",
    )

    assert summary["roles_assigned"] == ["Storage Blob Data Contributor"]
    assert "AcrPull" in summary["roles_failed"]


def test_start_aks_enqueues_openapi_after_cluster_start(monkeypatch) -> None:
    sent_tasks: list[dict[str, Any]] = []

    class FakePoller:
        def result(self) -> None:
            return None

    class FakeManagedClusters:
        def begin_start(self, resource_group: str, cluster_name: str) -> FakePoller:
            sent_tasks.append({"started": f"{resource_group}/{cluster_name}"})
            return FakePoller()

    class FakeAksClient:
        managed_clusters = FakeManagedClusters()

    def fake_send_task(
        task_name: str,
        *,
        kwargs: dict[str, Any],
        queue: str | None = None,
    ) -> AsyncResultStub:
        sent_tasks.append({"task_name": task_name, "kwargs": kwargs, "queue": queue})
        return AsyncResultStub("task-openapi-123")

    monkeypatch.setattr(azure, "get_credential", lambda: object())
    monkeypatch.setattr(azure, "aks_client", lambda _cred, _sub: FakeAksClient())
    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)

    result = azure.start_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        auto_openapi={
            "acr_name": "elbacr01",
            "acr_resource_group": "rg-elbacr",
            "storage_account": "elbstg01",
            "storage_resource_group": "rg-storage",
        },
    )

    assert result["openapi_task_id"] == "task-openapi-123"
    assert sent_tasks[0] == {"started": "rg-elb/elb-cluster"}
    assert sent_tasks[1]["task_name"] == "api.tasks.openapi.deploy_openapi_service"
    assert sent_tasks[1]["queue"] == "azure"
    assert sent_tasks[1]["kwargs"] == {
        "subscription_id": "sub-1",
        "resource_group": "rg-elb",
        "cluster_name": "elb-cluster",
        "acr_name": "elbacr01",
        "acr_resource_group": "rg-elbacr",
        "storage_account": "elbstg01",
        "storage_resource_group": "rg-storage",
    }

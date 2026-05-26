"""OpenAPI deploy route and Workload Identity contract tests.

Responsibility: Verify that OpenAPI deployment plumbing preserves the distinct
AKS resource group and Storage resource group through route enqueue and RBAC
scope construction.
Edit boundaries: Keep tests focused on request/task/RBAC contract shaping; use
fakes only and never call live Azure.
Key entry points: `test_openapi_deploy_route_forwards_storage_resource_group`,
`test_setup_workload_identity_uses_storage_resource_group_for_storage_role`,
`test_openapi_ready_failure_diagnostics_classifies_workload_identity_webhook`.
Risky contracts: OpenAPI deploy may target an AKS cluster in one resource group
while the workload Storage account lives in the dashboard anchor resource group.
Validation: `uv run pytest -q api/tests/test_openapi_deploy_contract.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from api.auth import CallerIdentity
from api.routes.aks import openapi as openapi_route
from api.tasks.openapi import deploy as openapi_deploy
from api.tasks.openapi import rbac as openapi_rbac
from api.tests._fakes import AsyncResultStub


def test_openapi_deploy_route_forwards_storage_resource_group(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_safe_delay(_task: object, **kwargs: Any) -> AsyncResultStub:
        captured.update(kwargs)
        return AsyncResultStub("task-openapi-1")

    monkeypatch.setattr(openapi_route, "_safe_delay", fake_safe_delay)

    response = openapi_route.aks_openapi_deploy(
        {
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "cluster_name": "elb-cluster-01",
            "acr_name": "elbacr",
            "storage_account": "stelbdashboardmul5oh5j44",
            "storage_resource_group": "rg-elb-dashboard",
        },
        CallerIdentity(
            object_id="caller-oid",
            tenant_id="tenant-id",
            upn="researcher@example.test",
            raw_token="token",
            claims={},
        ),
    )

    assert response["id"] == "task-openapi-1"
    assert captured["resource_group"] == "rg-elb-cluster"
    assert captured["storage_account"] == "stelbdashboardmul5oh5j44"
    assert captured["storage_resource_group"] == "rg-elb-dashboard"


def test_setup_workload_identity_uses_storage_resource_group_for_storage_role(
    monkeypatch,
) -> None:
    role_scopes: list[tuple[str, str]] = []

    class FakeManagedClusters:
        def get(self, resource_group: str, cluster_name: str) -> SimpleNamespace:
            assert resource_group == "rg-elb-cluster"
            assert cluster_name == "elb-cluster-01"
            return SimpleNamespace(
                oidc_issuer_profile=SimpleNamespace(
                    issuer_url="https://issuer.example.test/tenant/"
                )
            )

    class FakeUserAssignedIdentities:
        def create_or_update(
            self,
            resource_group: str,
            name: str,
            parameters: dict[str, Any],
        ) -> SimpleNamespace:
            assert resource_group == "rg-elb-cluster"
            assert parameters["location"] == "koreacentral"
            return SimpleNamespace(client_id="mi-client-id", principal_id="mi-principal-id")

    class FakeFederatedIdentityCredentials:
        def create_or_update(
            self,
            resource_group: str,
            identity_name: str,
            credential_name: str,
            parameters: dict[str, Any],
        ) -> None:
            assert resource_group == "rg-elb-cluster"
            assert parameters["subject"].startswith("system:serviceaccount:")

    class FakeMsiClient:
        def __init__(self, _credential: object, _subscription_id: str) -> None:
            self.user_assigned_identities = FakeUserAssignedIdentities()
            self.federated_identity_credentials = FakeFederatedIdentityCredentials()

    class FakeAuthorizationClient:
        def __init__(self, _credential: object, _subscription_id: str) -> None:
            self.role_assignments = object()

    def fake_assign_role_idempotent(
        _auth_client: object,
        scope: str,
        _principal_id: str,
        _role_definition_id: str,
        label: str,
    ) -> tuple[bool, str]:
        role_scopes.append((label, scope))
        return True, "created"

    import azure.mgmt.authorization as auth_mod
    import azure.mgmt.msi as msi_mod

    monkeypatch.setattr(
        openapi_rbac,
        "aks_client",
        lambda _credential, _subscription_id: SimpleNamespace(
            managed_clusters=FakeManagedClusters()
        ),
    )
    monkeypatch.setattr(auth_mod, "AuthorizationManagementClient", FakeAuthorizationClient)
    monkeypatch.setattr(msi_mod, "ManagedServiceIdentityClient", FakeMsiClient)
    monkeypatch.setattr(openapi_rbac, "assign_role_idempotent", fake_assign_role_idempotent)

    result = openapi_rbac.setup_workload_identity(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        cluster_name="elb-cluster-01",
        region="koreacentral",
        storage_account="stelbdashboardmul5oh5j44",
        storage_resource_group="rg-elb-dashboard",
    )

    assert result["mi_client_id"] == "mi-client-id"
    storage_scope = dict(role_scopes)["StorageBlobDataContributor"]
    assert "/resourceGroups/rg-elb-dashboard/" in storage_scope
    assert "/resourceGroups/rg-elb-cluster/providers/Microsoft.Storage/" not in storage_scope
    assert storage_scope.endswith(
        "/providers/Microsoft.Storage/storageAccounts/stelbdashboardmul5oh5j44"
    )


def test_openapi_ready_failure_diagnostics_classifies_workload_identity_webhook(
    monkeypatch,
) -> None:
    def fake_list_events(
        _cred: object,
        _subscription_id: str,
        _resource_group: str,
        _cluster_name: str,
        *,
        namespace: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        assert limit == 50
        if namespace == "default":
            return [
                {
                    "type": "Warning",
                    "namespace": "default",
                    "involved_kind": "ReplicaSet",
                    "involved_name": "elb-openapi-6495c5b67b",
                    "reason": "FailedCreate",
                    "message": (
                        "failed calling webhook \"mutation.azure-workload-identity.io\": "
                        "no endpoints available for service "
                        "\"azure-wi-webhook-webhook-service\""
                    ),
                    "last_timestamp": "2026-05-26T08:24:00Z",
                    "count": 17,
                }
            ]
        return [
            {
                "type": "Warning",
                "namespace": "kube-system",
                "involved_kind": "Pod",
                "involved_name": "azure-wi-webhook-controller-manager-abc",
                "reason": "FailedScheduling",
                "message": (
                    "0/11 nodes are available: 1 Insufficient cpu, "
                    "10 node(s) had untolerated taint(s)."
                ),
                "last_timestamp": "2026-05-26T08:23:00Z",
                "count": 3,
            }
        ]

    monkeypatch.setattr(openapi_deploy, "k8s_list_events", fake_list_events)

    diagnostics = openapi_deploy._openapi_ready_failure_diagnostics(
        object(),
        "sub-1",
        "rg-elb-cluster",
        "elb-cluster-01",
    )

    assert diagnostics["likely_cause"] == "workload_identity_webhook_unavailable"
    assert "systempool" in diagnostics["message"]
    assert len(diagnostics["events"]) == 2

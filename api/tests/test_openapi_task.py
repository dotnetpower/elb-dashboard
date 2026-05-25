"""Tests for OpenAPI Task behavior.

Responsibility: Tests for OpenAPI Task behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_build_manifests_sets_local_ssd_precise_openapi_env`,
`test_kubectl_apply_logs_in_with_managed_identity_when_needed`,
`test_kubectl_apply_reuses_existing_az_login`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_openapi_task.py`.
"""

from __future__ import annotations

import json
from typing import Any

from api.tasks import openapi


def test_build_manifests_sets_local_ssd_precise_openapi_env() -> None:
    manifest = openapi._build_manifests(
        image="elbacr.azurecr.io/elb-openapi:4.9",
        mi_client_id="mi-client-id",
        cluster_name="elb-cluster",
        resource_group="rg-elb",
        storage_account="elbstg",
        region="koreacentral",
        tenant_id="tenant-id",
        acr_name="elbacr",
        acr_resource_group="rg-acr",
        num_nodes=10,
    )
    docs = [json.loads(chunk) for chunk in manifest.split("\n---\n")]
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    env = {
        item["name"]: item["value"]
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert env["ELB_NUM_NODES"] == "10"
    assert env["ELB_CORE_NT_SHARDS"] == "10"
    assert "ELB_OPENAPI_API_TOKEN" not in env
    assert "PYTHONPATH" not in env


def test_build_manifests_preserves_openapi_api_token() -> None:
    manifest = openapi._build_manifests(
        image="elbacr.azurecr.io/elb-openapi:4.9",
        mi_client_id="mi-client-id",
        cluster_name="elb-cluster",
        resource_group="rg-elb",
        storage_account="elbstg",
        region="koreacentral",
        tenant_id="tenant-id",
        acr_name="elbacr",
        acr_resource_group="rg-acr",
        num_nodes=10,
        api_token="generated-token",
    )
    docs = [json.loads(chunk) for chunk in manifest.split("\n---\n")]
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    env = {
        item["name"]: item["value"]
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert env["ELB_OPENAPI_API_TOKEN"] == "generated-token"


def test_build_manifests_hardens_for_ha() -> None:
    """elb-openapi must roll out with replicas:2 + probes + PDB so a single
    node restart cannot take the BLAST submit path down."""
    manifest = openapi._build_manifests(
        image="elbacr.azurecr.io/elb-openapi:4.9",
        mi_client_id="mi-client-id",
        cluster_name="elb-cluster",
        resource_group="rg-elb",
        storage_account="elbstg",
        region="koreacentral",
        tenant_id="tenant-id",
        acr_name="elbacr",
        acr_resource_group="rg-acr",
        num_nodes=10,
    )
    docs = [json.loads(chunk) for chunk in manifest.split("\n---\n")]
    kinds = [doc["kind"] for doc in docs]
    assert "PodDisruptionBudget" in kinds, "PDB must ship with the deploy"

    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    spec = deployment["spec"]
    assert spec["replicas"] == 2

    container = spec["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["readinessProbe"]["httpGet"]["port"] == 8000
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"

    # Rolling update must not drop below the running count.
    assert spec["strategy"]["rollingUpdate"]["maxUnavailable"] == 0

    # topologySpread keeps the two replicas on different nodes when possible
    # but does not block scheduling on a single-node blast pool.
    spread = spec["template"]["spec"]["topologySpreadConstraints"][0]
    assert spread["topologyKey"] == "kubernetes.io/hostname"
    assert spread["whenUnsatisfiable"] == "ScheduleAnyway"

    pdb = next(doc for doc in docs if doc["kind"] == "PodDisruptionBudget")
    assert pdb["spec"]["minAvailable"] == 1
    assert pdb["spec"]["selector"]["matchLabels"] == {"app": "elb-openapi"}

    service = next(doc for doc in docs if doc["kind"] == "Service")
    assert service["spec"]["type"] == "LoadBalancer"
    assert service["metadata"]["annotations"] == {
        "service.beta.kubernetes.io/azure-load-balancer-internal": "true"
    }


def test_kubectl_apply_logs_in_with_managed_identity_when_needed(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout_seconds: int | float | None = None,
    ) -> dict[str, Any]:
        calls.append(argv)
        if argv[:3] == ["az", "account", "show"]:
            return {"exit_code": 1, "stderr": "Please run az login"}
        return {"exit_code": 0, "stdout": "ok"}

    monkeypatch.setenv("AZURE_CLIENT_ID", "mi-client-id")
    monkeypatch.setattr("api.services.terminal_exec.run", fake_run)

    result = openapi._kubectl_apply(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        manifest="apiVersion: v1\nkind: Service\nmetadata:\n  name: elb-openapi\n",
    )

    assert result == "ok"
    assert calls[0] == ["az", "account", "show", "--only-show-errors"]
    assert calls[1] == [
        "az",
        "login",
        "--identity",
        "--allow-no-subscriptions",
        "--only-show-errors",
        "--client-id",
        "mi-client-id",
    ]
    assert calls[2][:5] == ["az", "aks", "get-credentials", "--subscription", "sub-1"]
    assert calls[3][0] == "kubectl"


def test_kubectl_apply_reuses_existing_az_login(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout_seconds: int | float | None = None,
    ) -> dict[str, Any]:
        calls.append(argv)
        return {"exit_code": 0, "stdout": "ok"}

    monkeypatch.setattr("api.services.terminal_exec.run", fake_run)

    openapi._kubectl_apply(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        manifest="apiVersion: v1\nkind: Service\nmetadata:\n  name: elb-openapi\n",
    )

    assert not any(call[:3] == ["az", "login", "--identity"] for call in calls)
    assert calls[0] == ["az", "account", "show", "--only-show-errors"]
    assert calls[1][:5] == ["az", "aks", "get-credentials", "--subscription", "sub-1"]

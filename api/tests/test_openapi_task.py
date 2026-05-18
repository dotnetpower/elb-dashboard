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
    assert "PYTHONPATH" not in env


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

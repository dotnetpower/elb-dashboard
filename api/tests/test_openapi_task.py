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
        api_token="dummy-token-for-env-test",
    )
    docs = [json.loads(chunk) for chunk in manifest.split("\n---\n")]
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    env = {
        item["name"]: item["value"]
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert env["ELB_NUM_NODES"] == "10"
    assert env["ELB_CORE_NT_SHARDS"] == "10"
    # Concurrency cap defaults to 3 (matching the sibling OpenAPI's
    # BLAST_MAX_RUN_CONCURRENCY), with ELB_OPENAPI_NUM_CPUS=7 so 3 shard pods
    # (request=num-cpus-2=5) fit per E16 node (floor(15.74/5)=3, 0 Pending).
    assert env["ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS"] == "3"
    assert env["ELB_OPENAPI_NUM_CPUS"] == "7"
    # Escape hatch baked into the manifest so it survives cluster restarts /
    # redeploys: the split-versioned elb-openapi image emits a config key its
    # bundled elastic-blast CLI does not understand, so =0 forces the
    # historical init-ssd path and keeps `elastic-blast submit` from exiting 1.
    assert env["ELB_OPENAPI_SKIP_WARMED_SSD_INIT"] == "0"
    # Token entry is now mandatory — build_manifests refuses to emit a
    # deployment without it (see test_build_manifests_rejects_empty_token).
    assert env["ELB_OPENAPI_API_TOKEN"] == "dummy-token-for-env-test"
    assert "PYTHONPATH" not in env


def test_build_manifests_rejects_empty_token() -> None:
    """Refuse to emit a deployment without ELB_OPENAPI_API_TOKEN.

    Shipping a manifest without the token env entry is the root cause of
    the recurring "API token not visible" SPA bug. The guard must fail
    loudly at the manifest boundary, not silently produce a broken
    deployment.
    """
    import pytest

    base_kwargs = {
        "image": "elbacr.azurecr.io/elb-openapi:4.9",
        "mi_client_id": "mi-client-id",
        "cluster_name": "elb-cluster",
        "resource_group": "rg-elb",
        "storage_account": "elbstg",
        "region": "koreacentral",
        "tenant_id": "tenant-id",
        "acr_name": "elbacr",
        "acr_resource_group": "rg-acr",
        "num_nodes": 10,
    }
    for bad_token in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="api_token must be a non-empty string"):
            openapi._build_manifests(**base_kwargs, api_token=bad_token)
    # Default empty argument must also be rejected.
    with pytest.raises(ValueError, match="api_token must be a non-empty string"):
        openapi._build_manifests(**base_kwargs)


def test_build_manifests_grants_janitor_rbac_permissions() -> None:
    """elb-openapi ServiceAccount must hold cluster-admin so
    `elastic-blast submit` can apply its full manifest set.

    `elastic-blast submit` applies a janitor ClusterRoleBinding that binds
    the default ServiceAccount to `cluster-admin`, a `create-workspace`
    DaemonSet in `kube-system`, PersistentVolumes, a StorageClass, and the
    BLAST Jobs. A narrow custom ClusterRole made every openapi-driven
    core_nt submit fail mid-flight with a cascade of 403s. The pod's
    ServiceAccount is therefore bound directly to the built-in
    `cluster-admin` ClusterRole, and the redundant custom
    `elb-openapi-role` ClusterRole is no longer emitted.
    """
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
        api_token="dummy-token-for-rbac-test",
    )
    docs = [json.loads(chunk) for chunk in manifest.split("\n---\n")]

    # The ClusterRoleBinding must target the built-in cluster-admin role
    # and bind the elb-openapi ServiceAccount.
    binding = next(doc for doc in docs if doc["kind"] == "ClusterRoleBinding")
    assert binding["roleRef"]["name"] == "cluster-admin"
    assert binding["roleRef"]["kind"] == "ClusterRole"
    subject = binding["subjects"][0]
    assert subject["kind"] == "ServiceAccount"
    assert subject["name"] == "elb-openapi-sa"

    # The narrow custom ClusterRole must no longer be emitted — it was the
    # root cause of the cascading-403 submit failures.
    assert not any(
        doc["kind"] == "ClusterRole" and doc.get("metadata", {}).get("name") == "elb-openapi-role"
        for doc in docs
    )


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


def test_build_manifests_single_queue_owner() -> None:
    """elb-openapi must roll out with a SINGLE replica.

    The sibling OpenAPI service keeps its job queue in a process-local
    in-memory dict and enforces ``ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS`` against
    that local view only. A second replica would multiply the effective
    run-concurrency ceiling and strand queued jobs on whichever replica the
    LoadBalancer routed them to (the "/v1/jobs queueing doesn't work"
    symptom), so exactly one authoritative queue owner must ship.
    """
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
        api_token="dummy-token-for-ha-test",
    )
    docs = [json.loads(chunk) for chunk in manifest.split("\n---\n")]
    kinds = [doc["kind"] for doc in docs]
    assert "PodDisruptionBudget" in kinds, "PDB must ship with the deploy"

    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    spec = deployment["spec"]
    assert spec["replicas"] == 1

    # The manifest carries a revision annotation so the dashboard can detect a
    # live Deployment that predates a redeploy-only manifest change.
    from api.tasks.openapi.constants import (
        OPENAPI_MANIFEST_REVISION,
        OPENAPI_MANIFEST_REVISION_ANNOTATION,
    )

    annotations = deployment["metadata"].get("annotations", {})
    assert annotations.get(OPENAPI_MANIFEST_REVISION_ANNOTATION) == str(OPENAPI_MANIFEST_REVISION)
    container = spec["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["readinessProbe"]["httpGet"]["port"] == 8000
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"

    # Burst resilience (issue #54): the single-replica submit/dispatch path was
    # OOMKilled under a ~50-concurrent core_nt submit burst, then driven into a
    # liveness restart loop. Lock in the hardened resources + slack liveness so a
    # later edit cannot silently regress them.
    assert container["resources"]["limits"]["memory"] == "2Gi"
    assert container["resources"]["limits"]["cpu"] == "1"
    liveness = container["livenessProbe"]
    assert liveness["timeoutSeconds"] == 10
    assert liveness["failureThreshold"] == 6
    # Readiness stays strict so a transient spike still pulls the pod out of the
    # Service rotation quickly while liveness refrains from restarting it.
    assert container["readinessProbe"]["failureThreshold"] == 3

    # Rollout must never run two queue owners at once: the old pod terminates
    # before the new one starts (maxUnavailable:1, maxSurge:0).
    assert spec["strategy"]["rollingUpdate"]["maxUnavailable"] == 1
    assert spec["strategy"]["rollingUpdate"]["maxSurge"] == 0

    # topologySpread is retained (harmless on a single replica) and must keep
    # ScheduleAnyway so a single-node blast pool still schedules the pod.
    spread = spec["template"]["spec"]["topologySpreadConstraints"][0]
    assert spread["topologyKey"] == "kubernetes.io/hostname"
    assert spread["whenUnsatisfiable"] == "ScheduleAnyway"

    # PDB uses maxUnavailable:1 so a single-replica deployment does not block
    # voluntary node drains / AKS upgrades (minAvailable:1 would hang them).
    pdb = next(doc for doc in docs if doc["kind"] == "PodDisruptionBudget")
    assert pdb["spec"]["maxUnavailable"] == 1
    assert "minAvailable" not in pdb["spec"]
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


# ---------- PLS (Private Link Service) — PR2 ---------------------------------


def _base_build_kwargs() -> dict[str, Any]:
    return {
        "image": "elbacr.azurecr.io/elb-openapi:4.15",
        "mi_client_id": "mi-client-id",
        "cluster_name": "elb-cluster",
        "resource_group": "rg-elb",
        "storage_account": "elbstg",
        "region": "koreacentral",
        "tenant_id": "tenant-id",
        "acr_name": "elbacr",
        "acr_resource_group": "rg-acr",
        "num_nodes": 10,
        "api_token": "dummy-token",
    }


def _service_annotations(manifest_str: str) -> dict[str, str]:
    docs = [json.loads(chunk) for chunk in manifest_str.split("\n---\n")]
    svc = next(doc for doc in docs if doc["kind"] == "Service")
    return svc["metadata"]["annotations"]


def test_build_manifests_pls_disabled_omits_annotations() -> None:
    """Default (PLS disabled) → only ILB annotation present."""
    from api.tasks.openapi.constants import PlsConfig

    manifest = openapi._build_manifests(
        **_base_build_kwargs(),
        pls=PlsConfig(enabled=False, name="", lb_subnet="", visibility="*", auto_approval=""),
    )
    annotations = _service_annotations(manifest)
    assert annotations.get("service.beta.kubernetes.io/azure-load-balancer-internal") == "true"
    assert "service.beta.kubernetes.io/azure-pls-create" not in annotations


def test_build_manifests_pls_enabled_injects_annotations() -> None:
    """PLS enabled → all required PLS annotations injected."""
    from api.tasks.openapi.constants import PlsConfig

    manifest = openapi._build_manifests(
        **_base_build_kwargs(),
        pls=PlsConfig(
            enabled=True,
            name="pls-elb-openapi",
            lb_subnet="snet-elb-lb",
            visibility="sub-aaaa,sub-bbbb",
            auto_approval="sub-aaaa",
        ),
    )
    annotations = _service_annotations(manifest)
    assert annotations["service.beta.kubernetes.io/azure-load-balancer-internal"] == "true"
    assert annotations["service.beta.kubernetes.io/azure-pls-create"] == "true"
    assert annotations["service.beta.kubernetes.io/azure-pls-name"] == "pls-elb-openapi"
    assert (
        annotations["service.beta.kubernetes.io/azure-pls-ip-configuration-subnet"] == "snet-elb-lb"
    )
    assert annotations["service.beta.kubernetes.io/azure-pls-visibility"] == "sub-aaaa,sub-bbbb"
    assert annotations["service.beta.kubernetes.io/azure-pls-auto-approval"] == "sub-aaaa"


def test_build_manifests_pls_enabled_without_auto_approval_skips_annotation() -> None:
    """Empty auto_approval → annotation omitted entirely."""
    from api.tasks.openapi.constants import PlsConfig

    manifest = openapi._build_manifests(
        **_base_build_kwargs(),
        pls=PlsConfig(
            enabled=True,
            name="pls-elb-openapi",
            lb_subnet="snet-elb-lb",
            visibility="*",
            auto_approval="",
        ),
    )
    annotations = _service_annotations(manifest)
    assert annotations["service.beta.kubernetes.io/azure-pls-create"] == "true"
    assert "service.beta.kubernetes.io/azure-pls-auto-approval" not in annotations


def test_pls_config_from_env_rejects_enabled_without_subnet(monkeypatch) -> None:
    """OPENAPI_PLS_ENABLED=true without LB_SUBNET → ValueError."""
    import pytest
    from api.tasks.openapi.constants import pls_config_from_env

    monkeypatch.setenv("OPENAPI_PLS_ENABLED", "true")
    monkeypatch.delenv("OPENAPI_PLS_LB_SUBNET", raising=False)
    with pytest.raises(ValueError, match="OPENAPI_PLS_LB_SUBNET"):
        pls_config_from_env()


def test_pls_config_from_env_disabled_by_default(monkeypatch) -> None:
    """No env vars → enabled=False, defaults sane."""
    from api.tasks.openapi.constants import pls_config_from_env

    for var in (
        "OPENAPI_PLS_ENABLED",
        "OPENAPI_PLS_NAME",
        "OPENAPI_PLS_LB_SUBNET",
        "OPENAPI_PLS_VISIBILITY",
        "OPENAPI_PLS_AUTO_APPROVAL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = pls_config_from_env()
    assert cfg.enabled is False
    assert cfg.name == "pls-elb-openapi"
    assert cfg.visibility == "*"


def test_pls_config_from_env_reads_all_fields(monkeypatch) -> None:
    """All env vars set → PlsConfig populated."""
    from api.tasks.openapi.constants import pls_config_from_env

    monkeypatch.setenv("OPENAPI_PLS_ENABLED", "1")
    monkeypatch.setenv("OPENAPI_PLS_NAME", "pls-custom")
    monkeypatch.setenv("OPENAPI_PLS_LB_SUBNET", "snet-lb")
    monkeypatch.setenv("OPENAPI_PLS_VISIBILITY", "sub-a")
    monkeypatch.setenv("OPENAPI_PLS_AUTO_APPROVAL", "sub-a")
    cfg = pls_config_from_env()
    assert cfg.enabled is True
    assert cfg.name == "pls-custom"
    assert cfg.lb_subnet == "snet-lb"
    assert cfg.visibility == "sub-a"
    assert cfg.auto_approval == "sub-a"


def _baseline_manifest_kwargs() -> dict[str, object]:
    return dict(
        image="elbacr.azurecr.io/elb-openapi:4.24",
        mi_client_id="mi-client-id",
        cluster_name="elb-cluster",
        resource_group="rg-elb",
        storage_account="elbstg",
        region="koreacentral",
        tenant_id="tenant-id",
        acr_name="elbacr",
        acr_resource_group="rg-acr",
        num_nodes=10,
        api_token="shared-secret",
    )


def _env_map(manifest: str) -> dict[str, str]:
    docs = [json.loads(chunk) for chunk in manifest.split("\n---\n")]
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    return {
        item["name"]: item["value"]
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def test_build_manifests_omits_webhook_env_without_control_plane_url() -> None:
    manifest = openapi._build_manifests(**_baseline_manifest_kwargs())
    env = _env_map(manifest)
    # Backward-compat: deploys without a known control-plane URL must not emit
    # webhook env vars so the sibling's _webhook_notify stays a no-op (preserves
    # local-dev / azd-less deploy behaviour).
    assert "CONTROL_PLANE_URL" not in env
    assert "ELB_OPENAPI_INTERNAL_TOKEN" not in env
    # ELB_OPENAPI_API_TOKEN still required regardless.
    assert env["ELB_OPENAPI_API_TOKEN"] == "shared-secret"


def test_build_manifests_emits_webhook_env_when_control_plane_url_provided() -> None:
    manifest = openapi._build_manifests(
        **_baseline_manifest_kwargs(),
        control_plane_url="https://ca-elb-dashboard.example.azurecontainerapps.io",
    )
    env = _env_map(manifest)
    assert env["CONTROL_PLANE_URL"] == "https://ca-elb-dashboard.example.azurecontainerapps.io"
    # Shared secret: the dashboard webhook receiver accepts either
    # ELB_OPENAPI_INTERNAL_TOKEN or ELB_OPENAPI_API_TOKEN, but they must carry
    # the same value (single secret per cluster). The manifest builder forwards
    # api_token to both env entries to keep the contract obvious.
    assert env["ELB_OPENAPI_INTERNAL_TOKEN"] == env["ELB_OPENAPI_API_TOKEN"] == "shared-secret"


def test_resolve_control_plane_url_prefers_explicit_override(monkeypatch) -> None:
    from api.tasks.openapi import deploy as deploy_mod

    monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://override.example.com/")
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setenv("CONTAINER_APP_ENV_DNS_SUFFIX", "env.koreacentral.azurecontainerapps.io")
    assert deploy_mod._resolve_control_plane_url() == "https://override.example.com"


def test_resolve_control_plane_url_composes_from_aca_env(monkeypatch) -> None:
    from api.tasks.openapi import deploy as deploy_mod

    monkeypatch.delenv("DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setattr(
        "api.services.control_plane_url.get_control_plane_url", lambda: ""
    )
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setenv("CONTAINER_APP_ENV_DNS_SUFFIX", "env.koreacentral.azurecontainerapps.io")
    assert (
        deploy_mod._resolve_control_plane_url()
        == "https://ca-elb-dashboard.env.koreacentral.azurecontainerapps.io"
    )


def test_resolve_control_plane_url_empty_when_aca_env_missing(monkeypatch) -> None:
    from api.tasks.openapi import deploy as deploy_mod

    monkeypatch.delenv("DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setattr(
        "api.services.control_plane_url.get_control_plane_url", lambda: ""
    )
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("CONTAINER_APP_ENV_DNS_SUFFIX", raising=False)
    assert deploy_mod._resolve_control_plane_url() == ""

"""Tests for the AKS power-state gate on `/api/blast/databases/{db}/oracle`.

Responsibility: Pin the explicit 409 `aks_unavailable` response the order
    oracle build returns when the target AKS cluster is not Running, and that
    the gate degrades open (does not 409) when the ARM health probe raises.
    Verify every cluster call uses the AKS RG rather than the Storage RG.
Edit boundaries: Stubs the credential, the local-storage-access helper, and
    `get_cluster_health`; the unhealthy case returns before any Storage or
    K8s call so nothing else needs mocking.
Key entry points: `test_oracle_returns_409_when_cluster_stopped`,
    `test_oracle_defaults_aks_resource_group_for_legacy_callers`,
    `test_oracle_degrades_open_when_health_probe_raises`,
    `test_oracle_uses_aks_resource_group_for_every_cluster_call`.
Risky contracts: The 409 detail object's `code: aks_unavailable` is the SPA
    hook for the actionable "start the cluster" hint — renaming it breaks the
    Build Oracle error toast.
Validation: `uv run pytest -q api/tests/test_blast_oracle_aks_route.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


_BODY = {
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "resource_group": "rg-elb",
    "aks_resource_group": "rg-aks",
    "account_name": "stelbtest",
    "cluster_name": "elb-cluster",
    "acr_name": "acrelbtest",
}


def _patch_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.get_credential",
        lambda *_a, **_kw: object(),
        raising=True,
    )
    monkeypatch.setattr(
        "api.routes.blast.databases._maybe_open_local_storage_access",
        lambda *_a, **_kw: None,
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch._recover_terminal_active_claim",
        lambda *_args, **_kwargs: "none",
        raising=True,
    )


def test_oracle_returns_409_when_cluster_stopped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_credential(monkeypatch)

    def _stopped_health(
        _credential: object,
        _subscription_id: str,
        resource_group: str,
        _cluster_name: str,
    ) -> dict[str, Any]:
        assert resource_group == "rg-aks"
        return {
            "healthy": False,
            "exists": True,
            "power_state": "Stopped",
            "reason": "cluster_stopped",
        }

    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        _stopped_health,
        raising=True,
    )

    resp = client.post("/api/blast/databases/core_nt/oracle", json=_BODY)

    assert resp.status_code == 409, resp.text
    detail = resp.json()
    assert detail["code"] == "aks_unavailable"
    assert detail["cluster_power_state"] == "Stopped"
    assert detail["cluster_reason"] == "cluster_stopped"


def test_oracle_defaults_aks_resource_group_for_legacy_callers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_credential(monkeypatch)

    def _missing_health(
        _credential: object,
        _subscription_id: str,
        resource_group: str,
        _cluster_name: str,
    ) -> dict[str, Any]:
        assert resource_group == "rg-elb"
        return {
            "healthy": False,
            "exists": False,
            "power_state": None,
            "reason": "cluster_not_found",
        }

    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        _missing_health,
        raising=True,
    )
    legacy_body = dict(_BODY)
    legacy_body.pop("aks_resource_group")

    resp = client.post("/api/blast/databases/core_nt/oracle", json=legacy_body)

    assert resp.status_code == 409, resp.text
    assert resp.json()["cluster_reason"] == "cluster_not_found"


def test_oracle_degrades_open_when_health_probe_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_credential(monkeypatch)

    def _boom(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("ARM unreachable")

    monkeypatch.setattr("api.services.cluster_health.get_cluster_health", _boom, raising=True)
    # Storage listing returns no match → the route 404s the DB rather than
    # 409-ing on the cluster. The point is that the health probe raising does
    # NOT short-circuit into a 409.
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda *_a, **_kw: [],
        raising=True,
    )

    resp = client.post("/api/blast/databases/core_nt/oracle", json=_BODY)

    assert resp.status_code != 409
    if resp.status_code >= 400:
        body = resp.json()
        if isinstance(body, dict):
            assert body.get("code") != "aks_unavailable"


def test_oracle_uses_aks_resource_group_for_every_cluster_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_credential(monkeypatch)
    cluster_resource_groups: list[str] = []

    def _record_cluster_call(*args: Any, **_kwargs: Any) -> None:
        cluster_resource_groups.append(str(args[2]))

    def _healthy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _record_cluster_call(*args, **kwargs)
        return {
            "healthy": True,
            "exists": True,
            "power_state": "Running",
            "reason": None,
        }

    def _warmup(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _record_cluster_call(*args, **kwargs)
        return {
            "databases": [
                {
                    "name": "core_nt",
                    "status": "Ready",
                    "source_version": "v1",
                    "source_versions": ["v1"],
                    "shards": ["00"],
                    "pod_statuses": [
                        {"shard": "00", "node": "node-1"},
                    ],
                }
            ]
        }

    def _ready_nodes(*args: Any, **kwargs: Any) -> list[str]:
        _record_cluster_call(*args, **kwargs)
        return ["node-1"]

    def _apply_jobs(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _record_cluster_call(*args, **kwargs)
        return {"created": ["oracle-00"], "existing": [], "error_count": 0}

    monkeypatch.setattr("api.services.cluster_health.get_cluster_health", _healthy, raising=True)
    monkeypatch.setattr(
        "api.services.storage.data.list_databases",
        lambda *_a, **_kw: [
            {
                "name": "core_nt",
                "source_version": "v1",
                "copy_status": {"phase": "completed"},
                "sharded": True,
                "shard_sets": [1],
            }
        ],
        raising=True,
    )
    monkeypatch.setattr("api.services.k8s.monitoring.k8s_warmup_status", _warmup, raising=True)
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ready_warmup_node_names",
        _ready_nodes,
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ensure_job_manifests",
        _apply_jobs,
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.storage.data.upload_blob_text",
        lambda *_a, **_kw: None,
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.db.ops_audit.record_db_op",
        lambda **_kw: None,
        raising=True,
    )
    from api.services.db.oracle_state import OracleClaimResult

    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.oracle_container",
        lambda *_a, **_kw: object(),
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_dispatch.claim_oracle_build",
        lambda _container, *, db_name, document: OracleClaimResult(
            "ready", {**document, "status": "ready"}
        ),
        raising=True,
    )

    resp = client.post("/api/blast/databases/core_nt/oracle", json=_BODY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"
    assert cluster_resource_groups == ["rg-aks"] * 3


def test_oracle_build_denies_direct_write_when_rbac_gate_rejects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_credential(monkeypatch)
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.oracle_caller_write_authorized",
        lambda *_args, **_kwargs: (False, "storage_write_denied"),
    )

    response = client.post("/api/blast/databases/core_nt/oracle", json=_BODY)

    assert response.status_code == 403
    assert response.json()["code"] == "oracle_build_permission_denied"

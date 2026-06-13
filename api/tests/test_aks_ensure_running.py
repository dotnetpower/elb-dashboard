"""Tests for the AKS ensure-running readiness gate (service + route).

Responsibility: Lock the ensure-running state machine
(`api.services.aks.ensure_running.evaluate_ensure_running`) across every phase
and the route's start side-effect / Retry-After contract
(`api.routes.aks.ensure_running`).
Edit boundaries: Test-only. Patch the service's collaborators at their source
modules (cluster_health / auto_warmup / auto_warmup_reconcile / monitoring).
Key entry points: the test functions below.
Risky contracts: Asserts the external ``status`` vocabulary and that a start is
enqueued ONLY for a stopped + recommended cluster.
Validation: `uv run pytest -q api/tests/test_aks_ensure_running.py`.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from api.services.aks import ensure_running as svc
from fastapi.testclient import TestClient


def _health(
    *,
    exists: bool = True,
    power_state: str | None = "Running",
    provisioning_state: str | None = "Succeeded",
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "healthy": power_state == "Running",
        "exists": exists,
        "power_state": power_state,
        "provisioning_state": provisioning_state,
        "reason": reason,
    }


@pytest.fixture()
def patch_health(monkeypatch: pytest.MonkeyPatch):
    def _apply(health: dict[str, Any]) -> None:
        monkeypatch.setattr(
            "api.services.cluster_health.get_cluster_health",
            lambda *a, **k: health,
        )

    return _apply


def test_not_found(patch_health) -> None:
    patch_health(_health(exists=False, power_state=None, provisioning_state=None))
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "not_found"
    assert out["exists"] is False
    assert out["start_recommended"] is False
    assert out["retry_after_seconds"] is None


def test_stopped_recommends_start(patch_health) -> None:
    patch_health(_health(power_state="Stopped", provisioning_state="Succeeded"))
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "stopped"
    assert out["start_recommended"] is True
    assert out["retry_after_seconds"] == svc._RETRY_TRANSITION


def test_stopping_does_not_recommend_start(patch_health) -> None:
    patch_health(_health(power_state="Stopped", provisioning_state="Stopping"))
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "stopped"
    assert out["start_recommended"] is False


def test_starting(patch_health) -> None:
    patch_health(_health(power_state="Running", provisioning_state="Starting"))
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "starting"
    assert out["start_recommended"] is False


def test_unknown_when_arm_unreachable(patch_health) -> None:
    patch_health(_health(power_state=None, provisioning_state=None))
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "unknown"
    assert out["start_recommended"] is False


def test_running_without_warmup_is_ready(patch_health, monkeypatch) -> None:
    patch_health(_health(power_state="Running", provisioning_state="Succeeded"))
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *a, **k: None,
    )
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "ready"
    assert out["warmup"] is None
    assert out["retry_after_seconds"] is None


class _Pref:
    enabled = True
    databases: ClassVar[list[str]] = ["16S_ribosomal_RNA"]
    num_nodes = 2


def test_running_with_warmup_not_ready_is_warming(patch_health, monkeypatch) -> None:
    patch_health(_health(power_state="Running", provisioning_state="Succeeded"))
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *a, **k: _Pref(),
    )
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: {
            "provisioning_state": "Succeeded",
            "power_state": "Running",
            "node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.auto_warmup_reconcile.auto_warmup_ready_gate",
        lambda *a, **k: {
            "ready": False,
            "phase": "waiting_for_warmup_nodes",
            "expected_node_count": 2,
            "ready_node_count": 1,
            "reason": "waiting for all warmup nodes",
        },
    )
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "warming"
    assert out["warmup"] == {
        "ready": False,
        "phase": "waiting_for_warmup_nodes",
        "expected_node_count": 2,
        "ready_node_count": 1,
    }
    assert out["retry_after_seconds"] == svc._RETRY_WARMING


def test_running_with_warmup_ready_is_ready(patch_health, monkeypatch) -> None:
    patch_health(_health(power_state="Running", provisioning_state="Succeeded"))
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *a, **k: _Pref(),
    )
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: {
            "provisioning_state": "Succeeded",
            "power_state": "Running",
            "node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.auto_warmup_reconcile.auto_warmup_ready_gate",
        lambda *a, **k: {
            "ready": True,
            "phase": "ready",
            "expected_node_count": 2,
            "ready_node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda *a, **k: {
            "databases": [{"name": "16S_ribosomal_RNA", "status": "Ready"}],
        },
    )
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "ready"
    assert out["warmup"]["ready"] is True
    assert out["warmup"]["databases_total"] == 1
    assert out["warmup"]["databases_ready"] == 1
    assert out["warmup"]["pending_databases"] == []
    assert out["warmup"]["failed_databases"] == []
    assert out["retry_after_seconds"] is None


class _MultiDbPref:
    enabled = True
    databases: ClassVar[list[str]] = ["core_nt", "16S_ribosomal_RNA"]
    num_nodes = 2


def test_running_nodes_ready_but_database_still_loading_is_warming(
    patch_health, monkeypatch
) -> None:
    # Regression: warmup nodes are all K8s-Ready but ``core_nt`` is still
    # ``Loading`` (only ``16S_ribosomal_RNA`` warmed). The cluster must report
    # ``warming``, NOT ``ready`` — submitting now would fall back to the slow
    # on-node DB init.
    patch_health(_health(power_state="Running", provisioning_state="Succeeded"))
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *a, **k: _MultiDbPref(),
    )
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: {
            "provisioning_state": "Succeeded",
            "power_state": "Running",
            "node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.auto_warmup_reconcile.auto_warmup_ready_gate",
        lambda *a, **k: {
            "ready": True,
            "phase": "ready",
            "expected_node_count": 2,
            "ready_node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda *a, **k: {
            "databases": [
                {"name": "16S_ribosomal_RNA", "status": "Ready"},
                {"name": "core_nt", "status": "Loading"},
            ],
        },
    )
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "warming"
    assert out["warmup"]["ready"] is False
    assert out["warmup"]["phase"] == "warming_databases"
    assert out["warmup"]["databases_total"] == 2
    assert out["warmup"]["databases_ready"] == 1
    assert out["warmup"]["pending_databases"] == [{"db": "core_nt", "status": "Loading"}]
    assert "core_nt" in out["reason"]
    assert out["retry_after_seconds"] == svc._RETRY_WARMING


def test_running_nodes_ready_but_database_missing_is_warming(
    patch_health, monkeypatch
) -> None:
    # The warmup Job for a configured database has not appeared yet (empty
    # warmup status). Node readiness alone must not report ``ready``.
    patch_health(_health(power_state="Running", provisioning_state="Succeeded"))
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *a, **k: _Pref(),
    )
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: {
            "provisioning_state": "Succeeded",
            "power_state": "Running",
            "node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.auto_warmup_reconcile.auto_warmup_ready_gate",
        lambda *a, **k: {
            "ready": True,
            "phase": "ready",
            "expected_node_count": 2,
            "ready_node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda *a, **k: {"databases": []},
    )
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "warming"
    assert out["warmup"]["databases_ready"] == 0
    assert out["warmup"]["pending_databases"] == [
        {"db": "16S_ribosomal_RNA", "status": "pending"}
    ]
    assert out["retry_after_seconds"] == svc._RETRY_WARMING


def test_running_database_terminally_failed_is_ready_degraded(
    patch_health, monkeypatch
) -> None:
    # A configured database whose warmup is terminally ``Failed`` (no active
    # node) must NOT block ``ready`` forever — warmup is best-effort, so the
    # cluster reports ``ready`` (degraded) with the failed set surfaced. The
    # search falls back to the slow on-node init for that DB only.
    patch_health(_health(power_state="Running", provisioning_state="Succeeded"))
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *a, **k: _MultiDbPref(),
    )
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: {
            "provisioning_state": "Succeeded",
            "power_state": "Running",
            "node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.auto_warmup_reconcile.auto_warmup_ready_gate",
        lambda *a, **k: {
            "ready": True,
            "phase": "ready",
            "expected_node_count": 2,
            "ready_node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda *a, **k: {
            "databases": [
                {"name": "16S_ribosomal_RNA", "status": "Ready"},
                {"name": "core_nt", "status": "Failed", "nodes_active": 0},
            ],
        },
    )
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "ready"
    assert out["warmup"]["phase"] == "ready_degraded"
    assert out["warmup"]["databases_ready"] == 1
    assert out["warmup"]["pending_databases"] == []
    assert out["warmup"]["failed_databases"] == [{"db": "core_nt", "status": "Failed"}]
    assert "core_nt" in out["reason"]
    assert out["retry_after_seconds"] is None


def test_running_failed_database_with_active_node_keeps_warming(
    patch_health, monkeypatch
) -> None:
    # A ``Failed`` status that still has an active node is a retry in progress,
    # not a terminal failure — keep ``warming`` rather than degrading to ready.
    patch_health(_health(power_state="Running", provisioning_state="Succeeded"))
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *a, **k: _MultiDbPref(),
    )
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: {
            "provisioning_state": "Succeeded",
            "power_state": "Running",
            "node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.auto_warmup_reconcile.auto_warmup_ready_gate",
        lambda *a, **k: {
            "ready": True,
            "phase": "ready",
            "expected_node_count": 2,
            "ready_node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda *a, **k: {
            "databases": [
                {"name": "16S_ribosomal_RNA", "status": "Ready"},
                {"name": "core_nt", "status": "Failed", "nodes_active": 1},
            ],
        },
    )
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "warming"
    assert out["warmup"]["phase"] == "warming_databases"
    assert out["warmup"]["pending_databases"] == [{"db": "core_nt", "status": "Failed"}]
    assert out["warmup"]["failed_databases"] == []
    assert out["retry_after_seconds"] == svc._RETRY_WARMING


def test_running_stale_database_keeps_warming(patch_health, monkeypatch) -> None:
    # ``Stale`` (mixed DB generations) is recoverable — the reconcile re-warms
    # it — so it keeps ``warming``, not the terminal degraded path.
    patch_health(_health(power_state="Running", provisioning_state="Succeeded"))
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *a, **k: _Pref(),
    )
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: {
            "provisioning_state": "Succeeded",
            "power_state": "Running",
            "node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.auto_warmup_reconcile.auto_warmup_ready_gate",
        lambda *a, **k: {
            "ready": True,
            "phase": "ready",
            "expected_node_count": 2,
            "ready_node_count": 2,
        },
    )
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda *a, **k: {
            "databases": [{"name": "16S_ribosomal_RNA", "status": "Stale"}],
        },
    )
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "warming"
    assert out["warmup"]["pending_databases"] == [
        {"db": "16S_ribosomal_RNA", "status": "Stale"}
    ]
    assert out["warmup"]["failed_databases"] == []
    assert out["retry_after_seconds"] == svc._RETRY_WARMING



def test_running_warmup_snapshot_missing_degrades_to_warming(
    patch_health, monkeypatch
) -> None:
    patch_health(_health(power_state="Running", provisioning_state="Succeeded"))
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *a, **k: _Pref(),
    )
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *a, **k: None,
    )
    out = svc.evaluate_ensure_running(
        object(), subscription_id="s", resource_group="rg", cluster_name="c"
    )
    assert out["status"] == "warming"
    assert out["retry_after_seconds"] == svc._RETRY_WARMING


# --------------------------------------------------------------------------- #
# Route tests
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    from api.main import app

    return TestClient(app)


def _patch_eval(monkeypatch, result: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "api.services.aks.ensure_running.evaluate_ensure_running",
        lambda *a, **k: result,
    )


def _eval_result(status: str, *, start_recommended: bool = False, retry: int | None = None):
    return svc.EnsureRunningResult(
        status=status,
        power_state="Stopped" if status == "stopped" else "Running",
        provisioning_state="Succeeded",
        exists=True,
        start_recommended=start_recommended,
        warmup=None,
        retry_after_seconds=retry,
        reason=status,
    )


def test_route_missing_params_returns_400(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    resp = client.post("/api/aks/openapi/ensure-running", json={"cluster_name": "c"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "missing_parameters"


def test_route_stopped_enqueues_start(client: TestClient, monkeypatch) -> None:
    _patch_eval(
        monkeypatch,
        _eval_result("stopped", start_recommended=True, retry=30),
    )
    captured: dict[str, Any] = {}

    class _AsyncResult:
        id = "task-123"

    def _fake_delay(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _AsyncResult()

    import api.tasks.azure as azure_tasks

    monkeypatch.setattr(azure_tasks.start_aks, "delay", _fake_delay)
    resp = client.post(
        "/api/aks/openapi/ensure-running",
        json={"subscription_id": "s", "resource_group": "rg", "cluster_name": "c"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "stopped"
    assert body["start_triggered"] is True
    assert body["start_task_id"] == "task-123"
    assert resp.headers["Retry-After"] == "30"
    assert captured["cluster_name"] == "c"


def test_route_stopped_with_start_false_does_not_enqueue(
    client: TestClient, monkeypatch
) -> None:
    _patch_eval(
        monkeypatch,
        _eval_result("stopped", start_recommended=True, retry=30),
    )

    def _boom(**_kwargs: Any) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("start_aks.delay should not be called when start=false")

    import api.tasks.azure as azure_tasks

    monkeypatch.setattr(azure_tasks.start_aks, "delay", _boom)
    resp = client.post(
        "/api/aks/openapi/ensure-running",
        json={
            "subscription_id": "s",
            "resource_group": "rg",
            "cluster_name": "c",
            "start": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "stopped"
    assert body["start_triggered"] is False
    assert body["start_task_id"] is None


def test_route_ready_has_no_retry_header(client: TestClient, monkeypatch) -> None:
    _patch_eval(monkeypatch, _eval_result("ready"))
    resp = client.post(
        "/api/aks/openapi/ensure-running",
        json={"subscription_id": "s", "resource_group": "rg", "cluster_name": "c"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert "Retry-After" not in resp.headers


def test_route_auto_start_disabled_env(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("ENSURE_RUNNING_AUTO_START", "false")
    _patch_eval(
        monkeypatch,
        _eval_result("stopped", start_recommended=True, retry=30),
    )

    def _boom(**_kwargs: Any) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("start must not be enqueued when auto-start disabled")

    import api.tasks.azure as azure_tasks

    monkeypatch.setattr(azure_tasks.start_aks, "delay", _boom)
    resp = client.post(
        "/api/aks/openapi/ensure-running",
        json={"subscription_id": "s", "resource_group": "rg", "cluster_name": "c"},
    )
    assert resp.status_code == 200
    assert resp.json()["start_triggered"] is False

"""Tests for the synchronous BLAST submit preflight gates.

Responsibility: Verify each individual gate function and the aggregated
``evaluate_submit_gates`` decision under healthy + degraded conditions, and
confirm that the submit route fails closed with a structured 409 when any
critical gate blocks.
Edit boundaries: Test-only module; uses monkeypatch to stub the cloud / sidecar
collaborators each gate depends on. Mirror new gate additions here with a
"healthy + degraded" pair.
Key entry points: ``test_exec_token_gate_*``, ``test_terminal_sidecar_gate_*``,
``test_broker_gate_*``, ``test_aks_cluster_gate_*``, ``test_blast_database_gate_*``,
``test_evaluate_*``, ``test_submit_route_*``.
Risky contracts: ``conftest._stub_blast_submit_gates`` autoinstalls a default-pass
``evaluate_submit_gates``; route-level tests below re-patch the same symbol on
``api.services.blast.submit_gates`` so the request body actually exercises the
HTTPException 409 path.
Validation: ``uv run pytest -q api/tests/test_blast_submit_gates.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from api.services.blast import submit_gates
from fastapi.testclient import TestClient

# Capture real implementations at import time so the tests below can call them
# even though ``conftest._stub_blast_submit_gates`` patches the module-level
# symbols to a permissive default.
_REAL_EVALUATE = submit_gates.evaluate_submit_gates
_REAL_EXEC_TOKEN = submit_gates._gate_exec_token
_REAL_TERMINAL_SIDECAR = submit_gates._gate_terminal_sidecar


def _allow_all(**_kwargs: object) -> submit_gates.SubmitGatesReport:
    return submit_gates.SubmitGatesReport(ok=True, gates=[], blocking=[])


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_gates_cache() -> None:
    submit_gates.reset_submit_gates_cache()


# --------------------------- individual gates --------------------------------


def test_exec_token_gate_ok_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXEC_TOKEN", "abcdef")
    result = _REAL_EXEC_TOKEN()
    assert result.status == "ok"
    assert result.id == "exec_token"


def test_exec_token_gate_fails_when_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXEC_TOKEN", raising=False)
    result = _REAL_EXEC_TOKEN()
    assert result.status == "fail"
    assert result.error_code == "exec_token_missing"


def test_terminal_sidecar_gate_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.terminal_exec.healthz",
        lambda: {"status": "ok"},
    )
    result = _REAL_TERMINAL_SIDECAR()
    assert result.status == "ok"


def test_terminal_sidecar_gate_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict[str, Any]:
        raise RuntimeError("exec server unreachable: connect refused")

    monkeypatch.setattr("api.services.terminal_exec.healthz", _boom)
    result = _REAL_TERMINAL_SIDECAR()
    assert result.status == "fail"
    assert result.error_code == "terminal_sidecar_unavailable"


def test_broker_gate_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BadConn:
        def ensure_connection(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("Connection refused")

        def close(self) -> None:
            return None

    class _BadApp:
        def connection(self) -> _BadConn:
            return _BadConn()

    monkeypatch.setattr("api.celery_app.celery_app", _BadApp())
    result = submit_gates._gate_broker()
    assert result.status == "fail"
    assert result.error_code == "broker_unavailable"


def test_aks_cluster_gate_fail_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_args, **_kwargs: [
            {"name": "elb-cluster", "power_state": "Stopped"}
        ],
    )
    result = submit_gates._gate_aks_cluster(
        subscription_id="sub", resource_group="rg", cluster_name="elb-cluster"
    )
    assert result.status == "fail"
    assert result.error_code == "cluster_not_ready"


def test_aks_cluster_gate_ok_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    calls: list[tuple[Any, ...]] = []

    def _list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append((args, kwargs))
        return [{"name": "elb-cluster", "power_state": "Running"}]

    monkeypatch.setattr("api.services.monitoring.list_aks_clusters", _list)
    first = submit_gates._gate_aks_cluster(
        subscription_id="sub", resource_group="rg", cluster_name="elb-cluster"
    )
    second = submit_gates._gate_aks_cluster(
        subscription_id="sub", resource_group="rg", cluster_name="elb-cluster"
    )
    assert first.status == "ok"
    assert second.status == "ok"
    assert len(calls) == 1  # second call served from cache


def test_aks_cluster_gate_unknown_when_arm_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def _boom(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ARM throttled")

    monkeypatch.setattr("api.services.monitoring.list_aks_clusters", _boom)
    result = submit_gates._gate_aks_cluster(
        subscription_id="sub", resource_group="rg", cluster_name="elb-cluster"
    )
    assert result.status == "unknown"
    assert result.error_code == "cluster_check_unavailable"


def test_blast_database_gate_fail_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.blast.task_config import BlastDatabaseAvailabilityError

    def _boom(**_kwargs: Any) -> None:
        raise BlastDatabaseAvailabilityError(
            "BLAST database 'core_nt' is not available in Storage.",
            code="database_not_found",
        )

    monkeypatch.setattr(
        "api.services.blast.task_config.validate_blast_database_available", _boom
    )
    result = submit_gates._gate_blast_database(
        storage_account="elbstg01", database="core_nt"
    )
    assert result.status == "fail"
    assert result.error_code == "database_not_found"


def test_blast_database_gate_unknown_when_storage_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.blast.task_config import BlastDatabaseAvailabilityError

    def _boom(**_kwargs: Any) -> None:
        raise BlastDatabaseAvailabilityError(
            "Could not verify BLAST database 'core_nt' in Storage: ServiceRequestError.",
            code="database_check_unavailable",
        )

    monkeypatch.setattr(
        "api.services.blast.task_config.validate_blast_database_available", _boom
    )
    result = submit_gates._gate_blast_database(
        storage_account="elbstg01", database="core_nt"
    )
    assert result.status == "unknown"


# --------------------------- aggregate evaluator -----------------------------


def _stub_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXEC_TOKEN", "tok-123")
    monkeypatch.setattr("api.services.terminal_exec.healthz", lambda: {"status": "ok"})

    class _OkConn:
        def ensure_connection(self, *_args: object, **_kwargs: object) -> None:
            return None

        def close(self) -> None:
            return None

    class _OkApp:
        def connection(self) -> _OkConn:
            return _OkConn()

    monkeypatch.setattr("api.celery_app.celery_app", _OkApp())
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_a, **_k: [{"name": "elb-cluster", "power_state": "Running"}],
    )
    monkeypatch.setattr(
        "api.services.blast.task_config.validate_blast_database_available",
        lambda **_k: {
            "container": "blast-db",
            "blob_prefix": "core_nt/core_nt",
            "marker_blob": "core_nt/core_nt.nsq",
        },
    )


def test_evaluate_ok_when_all_gates_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_all_ok(monkeypatch)
    report = _REAL_EVALUATE(
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        database="core_nt",
    )
    assert report.ok is True
    assert report.blocking == []
    assert {g.id for g in report.gates} == {
        "exec_token",
        "terminal_sidecar",
        "broker",
        "aks_cluster",
        "blast_database",
    }


def test_evaluate_blocks_when_cluster_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_all_ok(monkeypatch)
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_a, **_k: [{"name": "elb-cluster", "power_state": "Stopped"}],
    )
    report = _REAL_EVALUATE(
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        database="core_nt",
    )
    assert report.ok is False
    assert [g.error_code for g in report.blocking] == ["cluster_not_ready"]


def test_evaluate_allow_unverified_downgrades_unknowns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_all_ok(monkeypatch)

    def _boom(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ARM throttled")

    monkeypatch.setattr("api.services.monitoring.list_aks_clusters", _boom)

    blocked = _REAL_EVALUATE(
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        database="core_nt",
    )
    assert blocked.ok is False
    assert [g.id for g in blocked.blocking] == ["aks_cluster"]

    submit_gates.reset_submit_gates_cache()
    permitted = _REAL_EVALUATE(
        subscription_id="sub",
        resource_group="rg",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        database="core_nt",
        allow_unverified=True,
    )
    assert permitted.ok is True
    aks = next(g for g in permitted.gates if g.id == "aks_cluster")
    assert aks.severity == "warning"
    assert aks.status == "unknown"


# --------------------------- HTTP integration --------------------------------


def _submit_payload() -> dict[str, Any]:
    return {
        "resource_group": "rg-elb",
        "cluster_name": "elb-cluster",
        "storage_account": "elbstg01",
        "program": "blastn",
        "database": "core_nt",
        "query_file": "queries/original/input.fa",
        "options": {"sharding_mode": "off"},
    }


def test_submit_route_returns_409_when_gate_blocks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    def _block(**_kwargs: object) -> submit_gates.SubmitGatesReport:
        blocked = submit_gates.GateResult(
            id="aks_cluster",
            status="fail",
            severity="critical",
            error_code="cluster_not_ready",
            message="AKS cluster 'elb-cluster' is Stopped. Start it first.",
            action="Start cluster",
            action_type="start_cluster",
        )
        return submit_gates.SubmitGatesReport(
            ok=False, gates=[blocked], blocking=[blocked]
        )

    monkeypatch.setattr(submit_gates, "evaluate_submit_gates", _block)

    response = client.post("/api/blast/submit", json=_submit_payload())
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "blocked_by_preflight"
    assert body["blocking_gates"][0]["error_code"] == "cluster_not_ready"
    assert body["blocking_gates"][0]["action_type"] == "start_cluster"


def test_submit_route_passes_when_gates_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr(submit_gates, "evaluate_submit_gates", _allow_all)

    class _AsyncResult:
        def __init__(self, task_id: str) -> None:
            self.id = task_id

    class FakeRepository:
        def get(self, _job_id: str) -> object | None:
            return None

        def create(self, _state: object) -> None:
            return None

        def update(self, job_id: str, **kwargs: object) -> object:
            return SimpleNamespace(job_id=job_id, **kwargs)

    monkeypatch.setattr(
        "api.services.state.repository.JobStateRepository", FakeRepository
    )
    monkeypatch.setattr(
        "api.tasks.blast.submit.delay", lambda **_k: _AsyncResult("task-ok")
    )

    response = client.post("/api/blast/submit", json=_submit_payload())
    assert response.status_code == 200
    assert response.json()["task_id"] == "task-ok"


def test_submit_route_respects_allow_unverified_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    seen_allow: dict[str, bool] = {}

    def _record(**kwargs: object) -> submit_gates.SubmitGatesReport:
        seen_allow["allow_unverified"] = bool(kwargs.get("allow_unverified"))
        return submit_gates.SubmitGatesReport(ok=True, gates=[], blocking=[])

    monkeypatch.setattr(submit_gates, "evaluate_submit_gates", _record)

    class _AsyncResult:
        id = "task-ok"

    monkeypatch.setattr("api.tasks.blast.submit.delay", lambda **_k: _AsyncResult())

    response = client.post(
        "/api/blast/submit",
        json=_submit_payload(),
        headers={"X-Submit-Allow-Unverified": "true"},
    )
    assert response.status_code == 200
    assert seen_allow["allow_unverified"] is True

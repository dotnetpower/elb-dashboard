"""Tests for the DB order-oracle Celery task state machine.

Responsibility: Verify successful publication, terminal Job failure, and
    bounded timeout behavior while all cloud/Kubernetes dependencies are
    mocked.
Edit boundaries: Task orchestration only; readiness, Blob CAS, and runtime
    classification have dedicated unit suites.
Key entry points: `test_oracle_task_publishes_only_after_complete_parts`,
    `test_published_redelivery_does_not_overwrite_new_automation_run`,
    `test_terminal_failure_sanitises_durable_error`.
Risky contracts: Domain failures must raise so Celery records FAILURE, delayed
    deliveries cannot overwrite a newer run, and durable errors are sanitised.
Validation: `uv run pytest -q api/tests/test_oracle_task.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.db.oracle_build import OracleBuildContext
from api.tasks.storage import oracle as oracle_task


class _Task:
    def __init__(self) -> None:
        self.progress: list[tuple[str, dict[str, Any]]] = []

    def update_state(self, *, state: str, meta: dict[str, Any]) -> None:
        self.progress.append((state, meta))


def _context() -> OracleBuildContext:
    return OracleBuildContext(
        db_name="core_nt",
        source_version="v1",
        layout_schema=1,
        layout_fingerprint="layout-1",
        identity="oracle-v1:layout-1",
        shards=("00", "01"),
        shard_nodes=(("00", "node-a"), ("01", "node-b")),
    )


def _kwargs() -> dict[str, str]:
    return {
        "job_id": "dbops:oracle:run-1",
        "run_id": "run-1",
        "owner_operation_id": "owner-1",
        "subscription_id": "sub-1",
        "storage_resource_group": "rg-storage",
        "storage_account": "stelbtest",
        "cluster_resource_group": "rg-aks",
        "cluster_name": "aks-1",
        "db_name": "core_nt",
        "image": "acr.azurecr.io/ncbi/elb:1",
        "identity": "oracle-v1:layout-1",
        "requested_source_version": "v1",
        "automatic": False,
        "dispatch_token": "dispatch-1",
    }


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    jobs: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    container = object()
    active = {
        "run_id": "run-1",
        "owner_operation_id": "owner-1",
        "part_prefix": "metadata/oracles/core_nt/parts/run-1/",
    }
    observations: dict[str, Any] = {
        "updates": [],
        "failures": [],
        "promotions": [],
        "cleanup": [],
        "job_logs": [],
        "order": [],
        "state": [],
    }
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.db.oracle_state.oracle_container",
        lambda *_args, **_kwargs: container,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_current",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_active",
        lambda *_args, **_kwargs: dict(active),
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.release_oracle_active",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.claim_oracle_execution",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.update_oracle_run",
        lambda *_args, **kwargs: observations["updates"].append(kwargs) or kwargs["updates"],
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.update_oracle_active",
        lambda *_args, **kwargs: observations["updates"].append(kwargs) or kwargs["updates"],
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.fail_oracle_run",
        lambda *_args, **kwargs: observations["failures"].append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.promote_oracle_run",
        lambda *_args, **kwargs: (
            observations["promotions"].append(kwargs)
            or {**kwargs["ready_document"], "status": "ready"}
        ),
    )
    monkeypatch.setattr(
        "api.services.db.oracle_build.resolve_oracle_build_context",
        lambda *_args, **_kwargs: _context(),
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ensure_job_manifests",
        lambda *_args, **_kwargs: {"created": ["oracle-00", "oracle-01"], "errors": []},
    )
    queue = list(jobs)
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_get_jobs",
        lambda *_args, **_kwargs: queue.pop(0) if len(queue) > 1 else queue[0],
    )
    monkeypatch.setattr(
        "api.services.db.oracle_runtime.validate_oracle_parts",
        lambda *_args, **_kwargs: {
            "ready": True,
            "ready_parts": 2,
            "expected_parts": 2,
            "missing": [],
            "unexpected": [],
            "empty": [],
        },
    )
    monkeypatch.setattr(
        "api.services.db.oracle_runtime.cleanup_oracle_jobs",
        lambda *_args, **kwargs: (
            observations["cleanup"].append(kwargs)
            or observations["order"].append("cleanup")
            or {"deleted": [], "errors": []}
        ),
    )

    def job_logs(*_args: Any, **kwargs: Any) -> str:
        observations["job_logs"].append(kwargs.get("job_name") or _args[5])
        observations["order"].append("logs")
        return "Error: [blastdbcmd] byte 178: Frame type=eFrameClassMember"

    monkeypatch.setattr("api.services.k8s.workload_ops.k8s_job_logs", job_logs)
    monkeypatch.setattr(
        "api.tasks.storage._update_state",
        lambda job_id, phase, **kwargs: observations["state"].append(
            {"job_id": job_id, "phase": phase, **kwargs}
        ),
    )
    monkeypatch.setattr("api.tasks.storage._record_task_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(
        oracle_task.build_db_order_oracle,
        "update_state",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(oracle_task.time, "sleep", lambda _seconds: None)
    return observations


def test_oracle_task_publishes_only_after_complete_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(
        monkeypatch,
        jobs=[
            [
                {"name": "oracle-core-nt-00-run-1", "status": "Running"},
                {"name": "oracle-core-nt-01-run-1", "status": "Pending"},
            ],
            [
                {"name": "oracle-core-nt-00-run-1", "status": "Complete"},
                {"name": "oracle-core-nt-01-run-1", "status": "Complete"},
            ],
        ],
    )

    result = oracle_task.build_db_order_oracle.run(**_kwargs())

    assert result["status"] == "completed"
    assert result["ready_parts"] == 2
    assert len(observations["promotions"]) == 1
    assert observations["state"][-1]["status"] == "completed"
    assert observations["failures"] == []


def test_oracle_task_raises_after_job_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(
        monkeypatch,
        jobs=[
            [
                {"name": "oracle-core-nt-00-run-1", "status": "Complete"},
                {"name": "oracle-core-nt-01-run-1", "status": "Failed"},
            ]
        ],
    )

    with pytest.raises(oracle_task.OracleTaskFailed, match="oracle_job_failed"):
        oracle_task.build_db_order_oracle.run(**_kwargs())

    assert observations["failures"][0]["error_code"] == "oracle_job_failed"
    assert observations["state"][-1]["status"] == "failed"
    assert len(observations["cleanup"]) == 1
    assert observations["job_logs"] == ["oracle-core-nt-01-run-1"]
    assert observations["order"] == ["logs", "cleanup"]
    assert "Frame type=eFrameClassMember" in observations["failures"][0]["error"]
    assert observations["promotions"] == []


def test_oracle_job_log_failure_does_not_block_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(
        monkeypatch,
        jobs=[[{"name": "oracle-core-nt-01-run-1", "status": "Failed"}]],
    )
    monkeypatch.setattr(
        "api.services.k8s.workload_ops.k8s_job_logs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("logs unavailable")),
    )

    with pytest.raises(oracle_task.OracleTaskFailed, match="oracle_job_failed"):
        oracle_task.build_db_order_oracle.run(**_kwargs())

    assert len(observations["cleanup"]) == 1
    assert observations["failures"][0]["error"] == (
        "failed Jobs: oracle-core-nt-01-run-1"
    )


def test_oracle_task_times_out_and_cleans_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(
        monkeypatch,
        jobs=[[{"name": "oracle-core-nt-00-run-1", "status": "Running"}]],
    )
    clock = iter([0.0, 0.0, float(oracle_task._BUILD_TIMEOUT_SECONDS + 1)])
    monkeypatch.setattr(oracle_task.time, "monotonic", lambda: next(clock))

    with pytest.raises(oracle_task.OracleTaskFailed, match="oracle_timeout"):
        oracle_task.build_db_order_oracle.run(**_kwargs())

    assert observations["failures"][0]["error_code"] == "oracle_timeout"
    assert observations["state"][-1]["phase"] == "timeout"
    assert len(observations["cleanup"]) == 1


def test_automatic_task_success_resets_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(
        monkeypatch,
        jobs=[
            [
                {"name": "oracle-core-nt-00-run-1", "status": "Complete"},
                {"name": "oracle-core-nt-01-run-1", "status": "Complete"},
            ]
        ],
    )
    successes = []
    monkeypatch.setattr(
        "api.services.db.oracle_retry.record_automation_success",
        lambda *_args, **kwargs: successes.append(kwargs) or kwargs,
    )
    kwargs = _kwargs()
    kwargs["automatic"] = True

    result = oracle_task.build_db_order_oracle.run(**kwargs)

    assert result["status"] == "completed"
    assert successes == [
        {
            "db_name": "core_nt",
            "run_id": "run-1",
            "require_current_run": True,
        }
    ]
    assert observations["failures"] == []


def test_published_redelivery_does_not_overwrite_new_automation_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(monkeypatch, jobs=[[]])
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_current",
        lambda *_args, **_kwargs: {"status": "ready", "run_id": "run-1"},
    )
    monkeypatch.setattr(
        "api.services.db.oracle_state.read_oracle_active",
        lambda *_args, **_kwargs: {
            "run_id": "run-2",
            "owner_operation_id": "owner-2",
        },
    )
    successes = []
    monkeypatch.setattr(
        "api.services.db.oracle_retry.record_automation_success",
        lambda *_args, **kwargs: successes.append(kwargs) or kwargs,
    )
    kwargs = _kwargs()
    kwargs["automatic"] = True

    result = oracle_task.build_db_order_oracle.run(**kwargs)

    assert result == {"status": "completed", "run_id": "run-1", "adopted": True}
    assert successes == []
    assert observations["state"][-1]["status"] == "completed"


def test_terminal_failure_sanitises_durable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(
        monkeypatch,
        jobs=[[{"name": "oracle-core-nt-00-run-1", "status": "Failed"}]],
    )
    secret = (
        "https://example.blob.core.windows.net/path?sv=1&se=2&sig=secret "
        "d0747f40-7ea8-4b08-8740-311fe516946c"
    )
    monkeypatch.setattr(
        "api.services.db.oracle_runtime.classify_oracle_jobs",
        lambda *_args, **_kwargs: type(
            "Progress",
            (),
            {
                "signature": "failed",
                "complete": (),
                "failed": (secret,),
                "missing": (),
                "status": "failed",
            },
        )(),
    )

    with pytest.raises(oracle_task.OracleTaskFailed, match="oracle_job_failed"):
        oracle_task.build_db_order_oracle.run(**_kwargs())

    durable_error = observations["failures"][0]["error"]
    state_error = observations["state"][-1]["error"]
    assert "sig=secret" not in durable_error
    assert "d0747f40-7ea8-4b08-8740-311fe516946c" not in durable_error
    assert "<sas-redacted>" in durable_error or "sig=<redacted>" in durable_error
    assert "d0747f40…" in durable_error
    assert state_error == durable_error


def test_automatic_task_failure_consumes_retry_budget_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(
        monkeypatch,
        jobs=[
            [
                {"name": "oracle-core-nt-00-run-1", "status": "Complete"},
                {"name": "oracle-core-nt-01-run-1", "status": "Failed"},
            ]
        ],
    )
    failures = []
    monkeypatch.setattr(
        "api.services.db.oracle_retry.record_automation_failure",
        lambda *_args, **kwargs: failures.append(kwargs) or kwargs,
    )
    kwargs = _kwargs()
    kwargs["automatic"] = True

    with pytest.raises(oracle_task.OracleTaskFailed, match="oracle_job_failed"):
        oracle_task.build_db_order_oracle.run(**kwargs)

    assert failures == [
        {
            "db_name": "core_nt",
            "run_id": "run-1",
            "error_code": "oracle_job_failed",
        }
    ]


def test_k8s_dispatch_exception_cleans_and_terminalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(monkeypatch, jobs=[[]])
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ensure_job_manifests",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("k8s down")),
    )

    with pytest.raises(oracle_task.OracleTaskFailed, match="oracle_k8s_dispatch_unreachable"):
        oracle_task.build_db_order_oracle.run(**_kwargs())

    assert observations["failures"][0]["error_code"] == ("oracle_k8s_dispatch_unreachable")
    assert len(observations["cleanup"]) == 1


def test_part_listing_exception_cleans_and_terminalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(
        monkeypatch,
        jobs=[
            [
                {"name": "oracle-core-nt-00-run-1", "status": "Complete"},
                {"name": "oracle-core-nt-01-run-1", "status": "Complete"},
            ]
        ],
    )
    monkeypatch.setattr(
        "api.services.db.oracle_runtime.validate_oracle_parts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("storage unavailable")),
    )

    with pytest.raises(oracle_task.OracleTaskFailed, match="oracle_parts_validation_failed"):
        oracle_task.build_db_order_oracle.run(**_kwargs())

    assert observations["failures"][0]["error_code"] == ("oracle_parts_validation_failed")
    assert len(observations["cleanup"]) == 1


def test_k8s_status_outage_waits_for_grace_then_terminalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(monkeypatch, jobs=[[]])
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_get_jobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("k8s down")),
    )
    monkeypatch.setattr(oracle_task, "_K8S_ERROR_LIMIT", 2)
    monkeypatch.setattr(oracle_task, "_K8S_ERROR_GRACE_SECONDS", 10)
    clock = iter([0.0, 0.0, 0.0, 5.0, 5.0, 11.0, 11.0])
    monkeypatch.setattr(oracle_task.time, "monotonic", lambda: next(clock))

    with pytest.raises(oracle_task.OracleTaskFailed, match="oracle_k8s_unreachable"):
        oracle_task.build_db_order_oracle.run(**_kwargs())

    assert observations["failures"][0]["error_code"] == "oracle_k8s_unreachable"
    assert len(observations["cleanup"]) == 1


def test_automatic_failure_state_write_error_retains_run_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _patch_common(
        monkeypatch,
        jobs=[
            [
                {"name": "oracle-core-nt-00-run-1", "status": "Complete"},
                {"name": "oracle-core-nt-01-run-1", "status": "Failed"},
            ]
        ],
    )
    monkeypatch.setattr(
        "api.services.db.oracle_retry.record_automation_failure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("automation state unavailable")
        ),
    )
    kwargs = _kwargs()
    kwargs["automatic"] = True

    with pytest.raises(
        oracle_task.OracleTaskFailed,
        match="automation failure recovery pending",
    ):
        oracle_task.build_db_order_oracle.run(**kwargs)

    assert observations["failures"] == []
    assert observations["state"][-1]["status"] == "running"
    assert observations["state"][-1]["phase"] == "failure_state_pending"

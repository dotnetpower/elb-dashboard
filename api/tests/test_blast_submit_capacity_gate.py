"""Integration tests for the BLAST submit capacity gate (issue #23 Stage 3b).

Responsibility: Exercise the ``BLAST_GATE_ENABLED`` branch in
``api.tasks.blast.submit_task.submit`` end-to-end so a default-OFF deploy stays
byte-equivalent to the legacy submit-lock path (Charter §12a Rule 4) and the
opt-in gate path emits the documented admit / deny / reserve-lost / release
behaviour.
Edit boundaries: Mock the upstream pipeline (DB availability, warmup oracle,
config build/upload, terminal warmup, oracle uploads, stream submit) so the
test only asserts the gate branch. Do NOT exercise the real terminal sidecar
or storage / ARM clients.
Key entry points: ``_install_pipeline_stubs``, ``test_*`` cases below.
Risky contracts: ``blast.submit.run`` is the canonical celery-task entry; the
helper-rewiring pattern through ``_blast.X`` (where ``_blast`` is
``api.tasks.blast`` re-exporting submit_task internals) must continue to work
or every monkeypatch here silently no-ops.
Validation: ``uv run pytest -q api/tests/test_blast_submit_capacity_gate.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from api.services.blast import capacity_gate, capacity_signals
from api.tasks import blast as _blast
from api.tasks.blast import submit_task


def test_capacity_gate_enabled_parses_truthy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "true", "TRUE", "Yes", "on"):
        monkeypatch.setenv("BLAST_GATE_ENABLED", value)
        assert submit_task._capacity_gate_enabled() is True


def test_capacity_gate_disabled_when_env_unset_or_falsey(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BLAST_GATE_ENABLED", raising=False)
    assert submit_task._capacity_gate_enabled() is False
    for value in ("", "0", "false", "no", "off", "random"):
        monkeypatch.setenv("BLAST_GATE_ENABLED", value)
        assert submit_task._capacity_gate_enabled() is False


# ---------------------------------------------------------------------------
# Pipeline stubs — install the minimum set of patches so blast.submit.run()
# reaches the critical try-block where the gate decision is taken.
# ---------------------------------------------------------------------------


@dataclass
class _Tracker:
    updates: list[tuple[str, str, dict[str, Any]]]
    requeues: list[dict[str, Any]]
    stream_calls: int = 0


def _install_pipeline_stubs(monkeypatch: pytest.MonkeyPatch) -> _Tracker:
    tracker = _Tracker(updates=[], requeues=[])

    def _update_state(job_id: str, phase: str, status: str = "running", **details: Any) -> None:
        tracker.updates.append((job_id, phase, {"status": status, **details}))

    monkeypatch.setattr(_blast, "_update_state", _update_state)
    monkeypatch.setattr(_blast, "_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(
        _blast,
        "_suppress_sharding_for_unsharded_database",
        lambda **kwargs: kwargs.get("options"),
    )
    monkeypatch.setattr(
        _blast, "_expand_strict_tie_order_candidate_pool", lambda options: options
    )
    monkeypatch.setattr(_blast, "_validate_blast_database_available", lambda **_k: None)
    monkeypatch.setattr(_blast, "_validate_blast_database_ready", lambda **_k: None)
    monkeypatch.setattr(_blast, "_ensure_node_warmup_ready_for_submit", lambda **_k: None)
    monkeypatch.setattr(_blast, "_ensure_terminal_azure_cli_login", lambda *_a, **_k: None)
    monkeypatch.setattr(_blast, "_ensure_terminal_kubeconfig_context", lambda *_a, **_k: None)
    monkeypatch.setattr(_blast, "_requires_split_parent_submission", lambda *_a, **_k: False)
    monkeypatch.setattr(_blast, "_submit_requires_node_warmup", lambda *_a, **_k: False)
    monkeypatch.setattr(
        _blast, "_build_config_content", lambda **_kwargs: "[elastic-blast]\n"
    )

    # Skip the best-effort storage config preview upload.
    def _fake_upload(_credential, _account, _container, _path, _content):
        return "https://example/queries/config.cfg"

    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr("api.services.storage.data.upload_blob_text", _fake_upload)

    # Best-effort oracle uploads return ``None`` so the success path is
    # exercised without hitting Storage.
    monkeypatch.setattr(
        "api.tasks.blast.submit_task.upload_tie_order_oracle_if_present",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "api.tasks.blast.submit_task.upload_db_order_oracle_pointer_if_available",
        lambda **_k: None,
    )

    def _stream_submit_command(**_kwargs: Any) -> dict[str, Any]:
        tracker.stream_calls += 1
        return {
            "exit_code": 0,
            "stdout": '{"decision": "accepted"}',
            "stderr": "",
            "duration_ms": 1,
            "timed_out": False,
            "log_line_count": 0,
            "_log_events": [],
        }

    monkeypatch.setattr(_blast, "_stream_submit_command", _stream_submit_command)
    monkeypatch.setattr(_blast, "_extract_elastic_blast_job_id", lambda *_a, **_k: None)
    monkeypatch.setattr(_blast, "_discover_elastic_blast_job_id", lambda *_a, **_k: None)
    monkeypatch.setattr(_blast, "_last_json", lambda *_a, **_k: {"decision": "accepted"})
    monkeypatch.setattr(
        _blast,
        "_submit_success_status",
        lambda *_a, **_k: ("completed", "completed"),
    )
    monkeypatch.setattr(
        _blast,
        "_gate_completed_submit_on_results",
        lambda **kwargs: (kwargs["phase"], kwargs["status"]),
    )
    monkeypatch.setattr(_blast, "_snippet", lambda value, *_a, **_k: str(value)[:200])
    monkeypatch.setattr(_blast, "_tail_text", lambda lines, *_a, **_k: "\n".join(lines))

    # Apply-async requeues — capture so tests can assert countdown / queue.
    def _capture_requeue(*_args: Any, **kwargs: Any) -> Any:
        tracker.requeues.append(kwargs)
        return None

    monkeypatch.setattr(submit_task.submit, "apply_async", _capture_requeue)
    monkeypatch.setattr(
        "api.tasks.blast.submit_task.poll_running_status",
        type("_P", (), {"apply_async": staticmethod(lambda **_k: None)})(),
    )
    monkeypatch.setattr(
        "api.tasks.blast.submit_task.persist_submit_log_events",
        lambda **_k: None,
    )

    return tracker


_SUBMIT_KWARGS = dict(
    job_id="job-cap-1",
    subscription_id="sub-1",
    resource_group="rg-elb",
    cluster_name="aks-elb",
    storage_account="elbstg01",
    program="blastn",
    database="16S_ribosomal_RNA",
    query_file="queries/q.fa",
    options={"sharding_mode": "off", "disable_sharding": True},
)


# ---------------------------------------------------------------------------
# Gate-disabled path (default) — must stay byte-equivalent.
# ---------------------------------------------------------------------------


def test_submit_gate_disabled_uses_submit_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLAST_GATE_ENABLED", raising=False)
    tracker = _install_pipeline_stubs(monkeypatch)

    lock_acquire_calls: list[tuple[str, str]] = []
    lock_release_calls: list[tuple[str, str]] = []

    def _fake_acquire(job_id: str, *, lock_key: str) -> tuple[object, str]:
        lock_acquire_calls.append((job_id, lock_key))
        return (object(), "token-A")

    def _fake_release(_client: object, token: str, *, lock_key: str) -> None:
        lock_release_calls.append((token, lock_key))

    monkeypatch.setattr(submit_task, "acquire_submit_lock", _fake_acquire)
    monkeypatch.setattr(submit_task, "release_submit_lock", _fake_release)

    capacity_touched: list[str] = []
    for attr in ("evaluate_capacity_gate", "reserve_slot", "release_slot"):
        monkeypatch.setattr(
            capacity_gate, attr, lambda *_a, _attr=attr, **_k: capacity_touched.append(_attr)
        )

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result["status"] == "completed"
    assert lock_acquire_calls == [("job-cap-1", submit_task.submit_lock_key("aks-elb", "default"))]
    assert len(lock_release_calls) == 1
    assert tracker.stream_calls == 1
    assert capacity_touched == []  # gate path must not be entered when disabled


def test_submit_failed_persists_full_console_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-retryable submit failure must persist the full stdout/stderr console
    # output on the ``submit_failed`` step (not just the short error_code tail)
    # so the Run details page shows the detailed error the operator needs.
    monkeypatch.delenv("BLAST_GATE_ENABLED", raising=False)
    tracker = _install_pipeline_stubs(monkeypatch)

    monkeypatch.setattr(submit_task, "acquire_submit_lock", lambda *_a, **_k: (object(), "tok"))
    monkeypatch.setattr(submit_task, "release_submit_lock", lambda *_a, **_k: None)

    failure_stderr = "INFO: starting submit\nERROR: AKS cluster elb-cluster-02 not found\n"

    def _failing_stream(**_kwargs: Any) -> dict[str, Any]:
        tracker.stream_calls += 1
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": failure_stderr,
            "duration_ms": 42,
            "timed_out": False,
            "log_line_count": 2,
            "_log_events": [],
        }

    monkeypatch.setattr(_blast, "_stream_submit_command", _failing_stream)
    # A non-error JSON payload so _result_error digs into the raw stderr.
    monkeypatch.setattr(_blast, "_last_json", lambda *_a, **_k: {"decision": "accepted"})
    monkeypatch.setattr(_blast, "_is_retryable_result", lambda *_a, **_k: False)

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result["status"] == "failed"
    assert result["phase"] == "submit_failed"
    assert "cluster elb-cluster-02 not found" in result["error"]

    failed_rows = [u for u in tracker.updates if u[1] == "submit_failed"]
    assert failed_rows, "expected a submit_failed state update"
    details = failed_rows[-1][2]
    assert details["status"] == "failed"
    # Full console output is persisted on the step, not just error_code.
    assert "cluster elb-cluster-02 not found" in details["output"]
    assert "cluster elb-cluster-02 not found" in details["last_output"]
    assert details["exit_code"] == 1
    assert details["log_line_count"] == 2
    assert details["error_code"]  # non-empty diagnostic always recorded


def test_submit_failed_logs_diagnostic_for_missing_elastic_blast(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Regression: when the terminal sidecar cannot spawn `elastic-blast` the
    # exec server now streams a stderr diagnostic + exit code 127 instead of an
    # empty body. The submit task must (a) surface that diagnostic as the job
    # error and (b) emit an explicit ``blast_submit_failed`` worker-log record
    # so the failure is greppable by job_id (previously the branch was silent,
    # producing the opaque "no output captured" with nothing in Log Analytics).
    monkeypatch.delenv("BLAST_GATE_ENABLED", raising=False)
    tracker = _install_pipeline_stubs(monkeypatch)

    monkeypatch.setattr(submit_task, "acquire_submit_lock", lambda *_a, **_k: (object(), "tok"))
    monkeypatch.setattr(submit_task, "release_submit_lock", lambda *_a, **_k: None)

    exec_diag = (
        "exec: cannot start 'elastic-blast': "
        "[Errno 2] No such file or directory: 'elastic-blast'"
    )

    def _failing_stream(**_kwargs: Any) -> dict[str, Any]:
        tracker.stream_calls += 1
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": exec_diag,
            "duration_ms": 31,
            "timed_out": False,
            "log_line_count": 1,
            "_log_events": [],
            "error": exec_diag,
        }

    monkeypatch.setattr(_blast, "_stream_submit_command", _failing_stream)
    monkeypatch.setattr(_blast, "_last_json", lambda *_a, **_k: None)
    monkeypatch.setattr(_blast, "_is_retryable_result", lambda *_a, **_k: False)

    with caplog.at_level("ERROR", logger="api.tasks.blast.submit_task"):
        result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result["status"] == "failed"
    assert result["phase"] == "submit_failed"
    assert "cannot start 'elastic-blast'" in result["error"]

    # An explicit worker-log record names the job + the actionable signals.
    failure_logs = [
        rec for rec in caplog.records if rec.message.startswith("blast_submit_failed")
    ]
    assert failure_logs, "expected a blast_submit_failed ERROR log record"
    log_text = failure_logs[-1].getMessage()
    assert "job-cap-1" in log_text
    assert "exit_code=127" in log_text
    assert "cannot start 'elastic-blast'" in log_text


# ---------------------------------------------------------------------------
# Gate-enabled path — admit, retryable deny, hard reject, reserve race.
# ---------------------------------------------------------------------------


def _stub_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        capacity_signals,
        "resolve_capacity_signals",
        lambda *_a, **_k: capacity_signals.CapacitySignals(
            pressure={"reachable": True, "pools": {}}, top_nodes=[], pending_pods=0
        ),
    )


def _force_decision(
    monkeypatch: pytest.MonkeyPatch, *, admit: bool, retryable: bool = False, reason: str = "ok"
) -> None:
    decision = capacity_gate.GateDecision(
        admit=admit,
        reason=None if admit else reason,
        retryable=retryable,
        slots_in_use=0,
        measured_pct=None,
    )
    monkeypatch.setattr(capacity_gate, "evaluate_capacity_gate", lambda **_k: decision)
    monkeypatch.setattr(capacity_gate, "list_active_reservations", lambda *_a, **_k: [])
    monkeypatch.setattr(
        capacity_gate,
        "predict_demand",
        lambda **_k: capacity_gate.ResourceDemand(cpu_m=1000, mem_mib=2048),
    )


def test_submit_gate_enabled_admit_reserves_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")
    tracker = _install_pipeline_stubs(monkeypatch)
    _stub_signals(monkeypatch)
    _force_decision(monkeypatch, admit=True)

    reservations: list[tuple[str, str]] = []
    releases: list[tuple[str, str]] = []

    def _reserve(cluster: str, job_id: str, demand: capacity_gate.ResourceDemand):
        reservations.append((cluster, job_id))
        return capacity_gate.Reservation(
            job_id=job_id,
            cpu_m=demand.cpu_m,
            mem_mib=demand.mem_mib,
            reserved_at="2026-05-31T00:00:00Z",
        )

    def _release(cluster: str, job_id: str) -> None:
        releases.append((cluster, job_id))

    monkeypatch.setattr(capacity_gate, "reserve_slot", _reserve)
    monkeypatch.setattr(capacity_gate, "release_slot", _release)

    # Lock acquire must NOT be invoked on the gate path.
    monkeypatch.setattr(
        submit_task,
        "acquire_submit_lock",
        lambda *_a, **_k: pytest.fail("acquire_submit_lock must not run when gate is enabled"),
    )

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result["status"] == "completed"
    assert reservations == [("aks-elb", "job-cap-1")]
    assert releases == [("aks-elb", "job-cap-1")]
    assert tracker.stream_calls == 1
    assert tracker.requeues == []


def test_submit_gate_enabled_retryable_deny_requeues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")
    tracker = _install_pipeline_stubs(monkeypatch)
    _stub_signals(monkeypatch)
    _force_decision(monkeypatch, admit=False, retryable=True, reason="cpu_watermark")

    reservations: list[Any] = []
    monkeypatch.setattr(
        capacity_gate,
        "reserve_slot",
        lambda *_a, **_k: reservations.append("nope") or pytest.fail(
            "reserve_slot must not run on deny"
        ),
    )

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result == {
        "job_id": "job-cap-1",
        "status": "running",
        "phase": "waiting_for_capacity",
        "requeued": True,
    }
    assert tracker.stream_calls == 0
    assert reservations == []
    assert len(tracker.requeues) == 1
    requeue = tracker.requeues[0]
    assert requeue["countdown"] == 30
    assert requeue["queue"] == "blast"
    # The deny state row carries the retryable phase + the deny error_code.
    deny_rows = [u for u in tracker.updates if u[1] == "waiting_for_capacity"]
    assert deny_rows
    assert deny_rows[-1][2]["status"] == "running"
    assert deny_rows[-1][2]["error_code"] == "capacity_gate_cpu_watermark"
    assert deny_rows[-1][2]["retry_after_seconds"] == 30


def test_submit_gate_enabled_hard_reject_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")
    tracker = _install_pipeline_stubs(monkeypatch)
    _stub_signals(monkeypatch)
    _force_decision(monkeypatch, admit=False, retryable=False, reason="oversize_request")

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result == {
        "job_id": "job-cap-1",
        "status": "failed",
        "phase": "rejected_capacity",
        "error_code": "capacity_gate_oversize_request",
    }
    assert tracker.requeues == []
    assert tracker.stream_calls == 0
    deny_rows = [u for u in tracker.updates if u[1] == "rejected_capacity"]
    assert deny_rows
    assert deny_rows[-1][2]["status"] == "failed"
    assert deny_rows[-1][2]["retry_after_seconds"] == 600


def test_submit_gate_enabled_reserve_lost_race_requeues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")
    tracker = _install_pipeline_stubs(monkeypatch)
    _stub_signals(monkeypatch)
    _force_decision(monkeypatch, admit=True)
    # evaluate admits, but reserve_slot returns None — another worker took
    # the last slot atomically. Treat the same as a retryable deny.
    monkeypatch.setattr(capacity_gate, "reserve_slot", lambda *_a, **_k: None)

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result == {
        "job_id": "job-cap-1",
        "status": "running",
        "phase": "waiting_for_capacity",
        "requeued": True,
    }
    assert tracker.stream_calls == 0
    assert len(tracker.requeues) == 1
    assert tracker.requeues[0]["countdown"] == 30
    deny_rows = [u for u in tracker.updates if u[1] == "waiting_for_capacity"]
    assert deny_rows
    assert deny_rows[-1][2]["error_code"] == "capacity_reserve_lost"


# ---------------------------------------------------------------------------
# BLAST_COORD_BACKEND=k8s precedence (§2a) — the cluster-backed Lease + count
# gate wins over BLAST_GATE_ENABLED; the Redis capacity gate / submit lock are
# bypassed entirely (reserve_slot / acquire_submit_lock never called).
# ---------------------------------------------------------------------------


def test_submit_k8s_backend_admit_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.blast import k8s_gate
    from api.services.k8s.submit_lease import SubmitLeaseHandle

    monkeypatch.setenv("BLAST_COORD_BACKEND", "k8s")
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")  # k8s must still win (§2a)
    tracker = _install_pipeline_stubs(monkeypatch)

    handle = SubmitLeaseHandle(
        name="elb-blast-submit-default", namespace="default", holder="dashboard-z"
    )
    released: list[object] = []
    monkeypatch.setattr(
        k8s_gate,
        "acquire_k8s_admission",
        lambda *_a, **_k: k8s_gate.K8sAdmission(admitted=True, lease=handle),
    )
    monkeypatch.setattr(
        k8s_gate, "release_k8s_admission", lambda *a, **_k: released.append(a[-1])
    )
    # Neither the Redis gate nor the submit lock may be touched in k8s mode.
    monkeypatch.setattr(
        capacity_gate,
        "reserve_slot",
        lambda *_a, **_k: pytest.fail("reserve_slot must not run in k8s mode"),
    )
    monkeypatch.setattr(
        submit_task,
        "acquire_submit_lock",
        lambda *_a, **_k: pytest.fail("acquire_submit_lock must not run in k8s mode"),
    )

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result["status"] == "completed"
    assert tracker.stream_calls == 1
    assert released == [handle]  # Lease released after the submit completes


def test_submit_k8s_backend_capacity_full_requeues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.blast import k8s_gate

    monkeypatch.setenv("BLAST_COORD_BACKEND", "k8s")
    tracker = _install_pipeline_stubs(monkeypatch)

    monkeypatch.setattr(
        k8s_gate,
        "acquire_k8s_admission",
        lambda *_a, **_k: k8s_gate.K8sAdmission(
            admitted=False,
            reason=k8s_gate.REASON_CAPACITY_FULL,
            retryable=True,
            active_count=3,
        ),
    )

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result == {
        "job_id": "job-cap-1",
        "status": "running",
        "phase": "waiting_for_capacity",
        "requeued": True,
    }
    assert tracker.stream_calls == 0
    assert len(tracker.requeues) == 1
    # countdown carries +0..10s de-sync jitter around the 30s base (H6/L26).
    assert 30 <= tracker.requeues[0]["countdown"] <= 40
    deny_rows = [u for u in tracker.updates if u[1] == "waiting_for_capacity"]
    assert deny_rows
    assert deny_rows[-1][2]["error_code"] == "blast_capacity_full"


def test_submit_k8s_backend_submit_slot_busy_requeues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.blast import k8s_gate

    monkeypatch.setenv("BLAST_COORD_BACKEND", "k8s")
    tracker = _install_pipeline_stubs(monkeypatch)

    monkeypatch.setattr(
        k8s_gate,
        "acquire_k8s_admission",
        lambda *_a, **_k: k8s_gate.K8sAdmission(
            admitted=False, reason=k8s_gate.REASON_SUBMIT_SLOT_BUSY, retryable=True
        ),
    )

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result == {
        "job_id": "job-cap-1",
        "status": "running",
        "phase": "waiting_for_submit_slot",
        "requeued": True,
    }
    assert tracker.stream_calls == 0
    deny_rows = [u for u in tracker.updates if u[1] == "waiting_for_submit_slot"]
    assert deny_rows
    assert deny_rows[-1][2]["error_code"] == "blast_submit_slot_busy"


def test_submit_k8s_backend_lease_api_error_uses_retry_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.blast import k8s_gate

    monkeypatch.setenv("BLAST_COORD_BACKEND", "k8s")
    _install_pipeline_stubs(monkeypatch)

    monkeypatch.setattr(
        k8s_gate,
        "acquire_k8s_admission",
        lambda *_a, **_k: k8s_gate.K8sAdmission(
            admitted=False, reason=k8s_gate.REASON_LEASE_API_ERROR, error=True
        ),
    )
    retry_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _blast,
        "_retry_or_fail",
        lambda _self, **kwargs: retry_calls.append(kwargs)
        or {"job_id": kwargs["job_id"], "status": "retrying"},
    )

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result["status"] == "retrying"
    assert retry_calls
    assert retry_calls[-1]["error_code"] == "blast_submit_lease_api_error"
    assert retry_calls[-1]["phase"] == "submit_coordination_unavailable"


# ---------------------------------------------------------------------------
# Stage 5 — telemetry counter bumps (admit / deny / release / reserve_lost).
# Each test isolates the counter store via _reset_counters_for_tests so the
# in-process dict can't bleed across tests when the module is reused.
# ---------------------------------------------------------------------------


def test_submit_gate_admit_bumps_admit_and_release_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")
    capacity_gate._reset_counters_for_tests()
    _install_pipeline_stubs(monkeypatch)
    _stub_signals(monkeypatch)
    _force_decision(monkeypatch, admit=True)

    def _reserve(cluster: str, job_id: str, demand: capacity_gate.ResourceDemand):
        return capacity_gate.Reservation(
            job_id=job_id,
            cpu_m=demand.cpu_m,
            mem_mib=demand.mem_mib,
            reserved_at="2026-05-31T00:00:00Z",
        )

    monkeypatch.setattr(capacity_gate, "reserve_slot", _reserve)
    monkeypatch.setattr(capacity_gate, "release_slot", lambda *_a, **_k: None)

    _blast.submit.run(**_SUBMIT_KWARGS)

    snap = capacity_gate.gate_counters_snapshot("aks-elb")
    assert snap["admit_total"] == 1
    assert snap["release_total"] == 1
    assert snap["deny_total"] == 0
    assert snap["reserve_lost_total"] == 0
    assert snap["last_event_at"] is not None


def test_submit_gate_retryable_deny_bumps_deny_counter_with_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")
    capacity_gate._reset_counters_for_tests()
    _install_pipeline_stubs(monkeypatch)
    _stub_signals(monkeypatch)
    _force_decision(monkeypatch, admit=False, retryable=True, reason="cpu_watermark")

    monkeypatch.setattr(
        capacity_gate,
        "reserve_slot",
        lambda *_a, **_k: pytest.fail("reserve must not run on deny"),
    )

    _blast.submit.run(**_SUBMIT_KWARGS)

    snap = capacity_gate.gate_counters_snapshot("aks-elb")
    assert snap["deny_total"] == 1
    assert snap["deny_by_reason"] == {"cpu_watermark": 1}
    assert snap["admit_total"] == 0
    assert snap["release_total"] == 0


def test_submit_gate_hard_reject_bumps_deny_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")
    capacity_gate._reset_counters_for_tests()
    _install_pipeline_stubs(monkeypatch)
    _stub_signals(monkeypatch)
    _force_decision(monkeypatch, admit=False, retryable=False, reason="oversize_request")

    _blast.submit.run(**_SUBMIT_KWARGS)

    snap = capacity_gate.gate_counters_snapshot("aks-elb")
    assert snap["deny_total"] == 1
    assert snap["deny_by_reason"] == {"oversize_request": 1}


def test_submit_gate_reserve_lost_bumps_reserve_lost_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")
    capacity_gate._reset_counters_for_tests()
    _install_pipeline_stubs(monkeypatch)
    _stub_signals(monkeypatch)
    _force_decision(monkeypatch, admit=True)
    monkeypatch.setattr(capacity_gate, "reserve_slot", lambda *_a, **_k: None)

    _blast.submit.run(**_SUBMIT_KWARGS)

    snap = capacity_gate.gate_counters_snapshot("aks-elb")
    assert snap["reserve_lost_total"] == 1
    assert snap["admit_total"] == 0
    assert snap["release_total"] == 0
    assert snap["deny_total"] == 0


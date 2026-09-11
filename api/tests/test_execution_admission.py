"""Tests for lifecycle-aware Service Bus execution admission.

Responsibility: Verify durable lifecycle barriers keep request messages queued until target
    nodes and correlated post-lifecycle database warmups are complete, including strict live
    reconciliation of a terminal failed-start generation.
Edit boundaries: Azure ARM, Kubernetes, Redis, Table Storage, and JobState are faked; no live
    cloud or broker access is allowed.
Key entry points: `_ready_dependencies`, `test_*`.
Risky contracts: Stop/delete barriers always block; start/scale barriers require exact target
    node convergence and completed correlated warmup jobs; newer lifecycle generations alone may
    terminalise an otherwise orphaned external job. Failed-start recovery requires authoritative
    current-node warmup and one source generation. Celery soft deadlines must propagate.
Validation: `uv run pytest -q api/tests/test_execution_admission.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api.services.aks import execution_admission as admission
from api.services.aks import execution_admission_state as admission_state
from billiard.exceptions import SoftTimeLimitExceeded


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch: pytest.MonkeyPatch) -> None:
    admission.reset_execution_admission_for_tests()
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setattr(admission_state, "_redis_client", lambda: None)
    monkeypatch.setattr(admission_state, "save_singleton", lambda _key, _payload: False)
    monkeypatch.setattr(admission_state, "load_singleton", lambda _key: None)
    monkeypatch.setattr(admission_state, "clear_singleton", lambda _key: True)
    monkeypatch.setattr(admission, "_CACHE_SECONDS", 0)
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: SimpleNamespace(get_many=lambda _ids, **_kwargs: {}),
    )


def _ready_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    live_nodes: int = 4,
    ready_nodes: int = 4,
    warmup_phase: str = "ready",
) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.aks.ensure_running.evaluate_ensure_running",
        lambda *_args, **_kwargs: {
            "status": "ready",
            "reason": "ready",
            "warmup": {"phase": warmup_phase},
        },
    )
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *_args, **_kwargs: {
            "node_count": live_nodes,
            "power_state": "Running",
            "provisioning_state": "Succeeded",
        },
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_ready_warmup_node_names",
        lambda *_args, **_kwargs: [f"node-{index}" for index in range(ready_nodes)],
    )


def _barrier(*, action: str = "scale", target: int = 4, databases=None, complete: bool = True):
    barrier = admission.create_lifecycle_barrier(
        action=action,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        target_node_count=target,
        databases=databases or [],
    )
    if complete and action in {"start", "scale"}:
        admission.record_lifecycle_completed(
            token=barrier.token,
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="aks-elb",
        )
    return barrier


def _decision() -> admission.AdmissionDecision:
    return admission.evaluate_execution_admission(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )


def test_stop_barrier_keeps_queue_closed_without_readiness_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _barrier(action="stop", target=0)
    called: list[int] = []
    monkeypatch.setattr(
        "api.services.aks.ensure_running.evaluate_ensure_running",
        lambda *_a, **_k: called.append(1),
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "aks_stop_in_progress"
    assert called == []


def test_deployed_persistence_failure_does_not_leave_ghost_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setattr(admission_state, "load_singleton_strict", lambda _key: None)

    with pytest.raises(admission.ExecutionAdmissionPersistenceError):
        _barrier(action="scale", target=4)

    assert admission_state._MEMORY == {}


def test_admission_state_read_failure_denies_queue_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        admission,
        "get_lifecycle_barrier",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("table unavailable")),
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "execution_admission_state_unavailable"


def test_readiness_soft_deadline_is_not_converted_to_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.aks.ensure_running.evaluate_ensure_running",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    with pytest.raises(SoftTimeLimitExceeded):
        _decision()


def test_caller_deadline_stops_before_next_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    now = [0.0]
    monkeypatch.setattr(
        admission,
        "get_lifecycle_barrier",
        lambda *_args: now.__setitem__(0, 10.0),
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.aks.ensure_running.evaluate_ensure_running",
        lambda *_args, **_kwargs: (
            calls.append("readiness")
            or {"status": "ready", "reason": "ready", "warmup": {"phase": "ready"}}
        ),
    )
    monkeypatch.setattr(
        admission,
        "_active_cluster_warmup_jobs",
        lambda *_args: calls.append("warmup-markers") or [],
    )
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *_args: pytest.fail("snapshot must not start after deadline"),
    )

    decision = admission.evaluate_execution_admission(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        deadline_monotonic=5.0,
        clock=lambda: now[0],
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "admission_budget_exhausted"
    assert calls == []


def test_failed_start_budget_exhaustion_keeps_budget_reason() -> None:
    barrier = admission.LifecycleBarrier(
        token="failed-start",
        action="start",
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        target_node_count=4,
        databases=(),
        created_at="2026-01-01T00:00:00Z",
    )

    decision = admission._evaluate_uncached(
        "sub-1",
        "rg-elb",
        "aks-elb",
        barrier,
        {"error_code": "start_failed"},
        deadline_monotonic=1.0,
        clock=lambda: 2.0,
    )

    assert decision["reason"] == "admission_budget_exhausted"
    assert "recovery_blocker" not in decision


def test_budget_denial_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission, "_CACHE_SECONDS", 60)
    decisions = iter(
        (
            admission._denied("admission_budget_exhausted", barrier=None),
            {"allowed": True, "reason": "ready", "retry_after_seconds": 0},
        )
    )
    monkeypatch.setattr(admission, "_evaluate_uncached", lambda *_args, **_kwargs: next(decisions))

    first = _decision()
    second = _decision()

    assert first["reason"] == "admission_budget_exhausted"
    assert second["allowed"] is True


def test_scale_barrier_waits_for_exact_target_node_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _barrier(target=4)
    _ready_dependencies(monkeypatch, live_nodes=3, ready_nodes=3)

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "aks_scaling"


def test_scale_barrier_waits_for_arm_lifecycle_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _barrier(target=4, complete=False)
    _ready_dependencies(monkeypatch)

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "aks_scale_in_progress"


def test_scale_barrier_surfaces_terminal_lifecycle_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _barrier(target=4, complete=False)
    _ready_dependencies(monkeypatch)
    assert admission.record_lifecycle_failed(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        error_code="HttpResponseError",
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "aks_scale_failed"


def test_start_failure_recovers_only_after_live_execution_state_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _barrier(action="start", target=4, complete=False)
    _ready_dependencies(monkeypatch, live_nodes=4, ready_nodes=4)
    assert admission.record_lifecycle_failed(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        error_code="ResourceExistsError",
    )

    decision = _decision()

    assert decision["allowed"] is True
    assert decision["reason"] == "ready"
    assert decision["recovered_lifecycle_failure"] is True


def test_start_failure_keeps_original_reason_until_target_nodes_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _barrier(action="start", target=4, complete=False)
    _ready_dependencies(monkeypatch, live_nodes=3, ready_nodes=3)
    assert admission.record_lifecycle_failed(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        error_code="ResourceExistsError",
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "aks_start_failed"
    assert decision["recovery_blocker"] == "aks_scaling"


def test_start_failure_with_database_requires_current_per_node_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _barrier(
        action="start",
        target=4,
        databases=["core_nt"],
        complete=False,
    )
    _ready_dependencies(monkeypatch, live_nodes=4, ready_nodes=4)
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda *_args, **_kwargs: {
            "databases": [
                {
                    "name": "core_nt",
                    "status": "Ready",
                    "sources": ["warmup"],
                    "nodes_ready": 4,
                    "nodes_active": 0,
                    "nodes_failed": 0,
                    "total_jobs": 4,
                    "source_version": "2026-08-01-01-05-01",
                }
            ]
        },
    )
    assert admission.record_lifecycle_failed(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        error_code="ResourceExistsError",
    )

    decision = _decision()

    assert decision["allowed"] is True
    assert decision["recovered_lifecycle_failure"] is True


@pytest.mark.parametrize(
    ("sources", "nodes_ready", "source_version", "expected_detail"),
    [
        (["setup"], 4, "2026-08-01-01-05-01", "4/4 Ready nodes"),
        (["warmup"], 3, "2026-08-01-01-05-01", "3/4 Ready nodes"),
        (["warmup"], 4, "", "4/4 Ready nodes"),
    ],
)
def test_start_failure_rejects_non_authoritative_or_partial_warmup(
    monkeypatch: pytest.MonkeyPatch,
    sources: list[str],
    nodes_ready: int,
    source_version: str,
    expected_detail: str,
) -> None:
    barrier = _barrier(
        action="start",
        target=4,
        databases=["core_nt"],
        complete=False,
    )
    _ready_dependencies(monkeypatch, live_nodes=4, ready_nodes=4)
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda *_args, **_kwargs: {
            "databases": [
                {
                    "name": "core_nt",
                    "status": "Ready",
                    "sources": sources,
                    "nodes_ready": nodes_ready,
                    "nodes_active": 0,
                    "nodes_failed": 0,
                    "total_jobs": nodes_ready,
                    "source_version": source_version,
                }
            ]
        },
    )
    assert admission.record_lifecycle_failed(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        error_code="ResourceExistsError",
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "aks_start_failed"
    assert decision["recovery_blocker"] == "database_warmup_pending"
    assert expected_detail in decision["detail"]


def test_scale_barrier_waits_for_all_kubernetes_ready_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _barrier(target=4)
    _ready_dependencies(monkeypatch, live_nodes=4, ready_nodes=3)

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "waiting_for_target_nodes"
    assert decision["ready_node_count"] == 3


def test_scale_barrier_requires_correlated_warmup_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _barrier(target=4, databases=["core_nt"])
    _ready_dependencies(monkeypatch)

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "database_warmup_pending"


def test_warmup_correlations_do_not_lose_parallel_database_updates() -> None:
    barrier = _barrier(target=4, databases=["core_nt", "nr"])

    admission.record_barrier_warmup_jobs(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        jobs={"core_nt": "warm-core"},
    )
    admission.record_barrier_warmup_jobs(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        jobs={"nr": "warm-nr"},
    )

    assert admission.get_barrier_warmup_jobs(barrier.token, barrier.databases) == {
        "core_nt": "warm-core",
        "nr": "warm-nr",
    }


def test_current_generation_can_clear_failed_warmup_enqueue_correlation() -> None:
    barrier = _barrier(target=4, databases=["core_nt"])
    assert admission.record_barrier_warmup_jobs(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        jobs={"core_nt": "warm-core"},
    )

    assert admission.clear_barrier_warmup_job(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        database="core_nt",
    )
    assert admission.get_barrier_warmup_jobs(barrier.token, barrier.databases) == {}


def test_superseded_generation_cannot_clear_current_warmup_correlation() -> None:
    old = _barrier(target=4, databases=["core_nt"])
    current = _barrier(target=4, databases=["core_nt"])
    assert admission.record_barrier_warmup_jobs(
        token=current.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        jobs={"core_nt": "warm-current"},
    )

    assert not admission.clear_barrier_warmup_job(
        token=old.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        database="core_nt",
    )
    assert admission.get_barrier_warmup_jobs(current.token, current.databases) == {
        "core_nt": "warm-current"
    }


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("queued", "database_warmup_in_progress"),
        ("running", "database_warmup_in_progress"),
        ("failed", "database_warmup_failed"),
    ],
)
def test_scale_barrier_classifies_correlated_warmup_state(
    monkeypatch: pytest.MonkeyPatch, status: str, reason: str
) -> None:
    barrier = _barrier(target=4, databases=["core_nt"])
    _ready_dependencies(monkeypatch)
    assert admission.record_barrier_warmup_jobs(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        jobs={"core_nt": "warm-1"},
    )
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: SimpleNamespace(
            list_active=lambda **_kwargs: [],
            get_many=lambda _ids, **_kwargs: {"warm-1": SimpleNamespace(status=status)},
        ),
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == reason


def test_scale_barrier_opens_only_after_warmup_job_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _barrier(target=4, databases=["core_nt"])
    _ready_dependencies(monkeypatch)
    admission.record_barrier_warmup_jobs(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        jobs={"core_nt": "warm-1"},
    )
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: SimpleNamespace(
            list_active=lambda **_kwargs: [],
            get_many=lambda _ids, **_kwargs: {"warm-1": SimpleNamespace(status="completed")},
        ),
    )

    decision = _decision()

    assert decision["allowed"] is True
    assert decision["reason"] == "ready"


def test_degraded_warmup_never_opens_request_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_dependencies(monkeypatch, warmup_phase="ready_degraded")

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "database_warmup_failed"


def test_manual_active_warmup_keeps_queue_closed_without_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_dependencies(monkeypatch, warmup_phase="ready")
    admission.record_active_warmup_job(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        job_id="manual-warmup-1",
        database="core_nt",
    )
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: SimpleNamespace(
            get_many=lambda _ids, **_kwargs: {
                "manual-warmup-1": SimpleNamespace(job_id="manual-warmup-1", status="running")
            }
        ),
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "database_warmup_in_progress"


def test_terminal_warmup_marker_does_not_block_and_is_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_dependencies(monkeypatch, warmup_phase="ready")
    admission.record_active_warmup_job(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        job_id="warmup-done",
    )
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: SimpleNamespace(
            get_many=lambda _ids, **_kwargs: {
                "warmup-done": SimpleNamespace(job_id="warmup-done", status="completed")
            }
        ),
    )

    decision = _decision()

    assert decision["allowed"] is True
    assert admission_state.list_active_warmup_markers("sub-1", "rg-elb", "aks-elb") == {}


def test_fresh_marker_without_jobstate_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_dependencies(monkeypatch, warmup_phase="ready")
    admission.record_active_warmup_job(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        job_id="warmup-missing",
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "database_warmup_in_progress"


def test_admission_never_calls_full_active_job_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_dependencies(monkeypatch, warmup_phase="ready")

    class _Repo:
        def list_active(self, **_kwargs: object) -> list[object]:
            raise AssertionError("full active JobState scan is forbidden")

        def get_many(self, _ids: list[str], **_kwargs: object) -> dict[str, object]:
            return {}

    monkeypatch.setattr("api.services.state_repo.get_state_repo", _Repo)

    assert _decision()["allowed"] is True


def test_active_marker_jobstate_lookup_projects_status_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_dependencies(monkeypatch, warmup_phase="ready")
    admission.record_active_warmup_job(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        job_id="warmup-projected",
    )
    captured: dict[str, object] = {}

    class _Repo:
        def get_many(self, job_ids: list[str], **kwargs: object) -> dict[str, object]:
            captured["job_ids"] = job_ids
            captured.update(kwargs)
            return {"warmup-projected": SimpleNamespace(status="running")}

    monkeypatch.setattr("api.services.state_repo.get_state_repo", _Repo)

    decision = _decision()

    assert decision["reason"] == "database_warmup_in_progress"
    assert captured == {
        "job_ids": ["warmup-projected"],
        "select": ["PartitionKey", "RowKey", "status"],
    }


def test_concurrent_warmup_markers_are_all_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_dependencies(monkeypatch, warmup_phase="ready")
    for job_id in ("warmup-a", "warmup-b"):
        admission.record_active_warmup_job(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="aks-elb",
            job_id=job_id,
        )
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: SimpleNamespace(
            get_many=lambda ids, **_kwargs: {
                job_id: SimpleNamespace(job_id=job_id, status="running") for job_id in ids
            }
        ),
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert set(decision["warmup_jobs"]) == {"warmup-a", "warmup-b"}


def test_local_marker_is_read_from_cross_process_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Redis:
        def scan_iter(self, **_kwargs: object) -> list[bytes]:
            return [marker_key.encode("utf-8")]

        def get(self, _key: object) -> bytes:
            return marker_payload

    marker_key = admission_state._active_warmup_key(
        "sub-1", "rg-elb", "aks-elb", "warmup-cross-process"
    )
    marker_payload = (
        b'{"subscription_id":"sub-1","resource_group":"rg-elb",'
        b'"cluster_name":"aks-elb","job_id":"warmup-cross-process",'
        b'"database":"core_nt","registered_at":"2026-01-01T00:00:00Z"}'
    )
    monkeypatch.setattr(admission_state, "_redis_client", lambda: _Redis())

    markers = admission_state.list_active_warmup_markers("sub-1", "rg-elb", "aks-elb")

    assert set(markers) == {"warmup-cross-process"}


def test_stale_orphan_marker_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_dependencies(monkeypatch, warmup_phase="ready")
    admission.record_active_warmup_job(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        job_id="orphan",
    )
    marker_key = next(key for key in admission_state._MEMORY if "active-warmup" in key)
    admission_state._MEMORY[marker_key]["registered_at"] = "2000-01-01T00:00:00Z"

    decision = _decision()

    assert decision["allowed"] is True
    assert admission_state.list_active_warmup_markers("sub-1", "rg-elb", "aks-elb") == {}


def test_marker_store_failure_keeps_admission_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_dependencies(monkeypatch, warmup_phase="ready")
    monkeypatch.setattr(
        admission,
        "list_active_warmup_markers",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("table unavailable")),
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "readiness_check_failed"


def test_allow_decision_is_not_cached_across_manual_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admission, "_CACHE_SECONDS", 60)
    _ready_dependencies(monkeypatch, warmup_phase="ready")
    assert _decision()["allowed"] is True
    monkeypatch.setattr(
        "api.services.aks.ensure_running.evaluate_ensure_running",
        lambda *_args, **_kwargs: {
            "status": "warming",
            "reason": "database warmup is active",
            "warmup": {"phase": "warming"},
        },
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "cluster_warming"


def test_recovered_start_allow_is_not_cached_across_node_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admission, "_CACHE_SECONDS", 60)
    barrier = _barrier(action="start", target=4, complete=False)
    _ready_dependencies(monkeypatch, live_nodes=4, ready_nodes=4)
    assert admission.record_lifecycle_failed(
        token=barrier.token,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        error_code="ResourceExistsError",
    )
    assert _decision()["allowed"] is True
    monkeypatch.setattr(
        "api.services.monitoring.get_aks_cluster_snapshot",
        lambda *_args, **_kwargs: {
            "node_count": 3,
            "power_state": "Running",
            "provisioning_state": "Succeeded",
        },
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "aks_start_failed"
    assert decision["recovery_blocker"] == "aks_scaling"


def test_cancelled_enqueue_generation_no_longer_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _barrier(action="stop", target=0)
    admission.cancel_lifecycle_barrier(barrier.token, reason="broker_unavailable")
    _ready_dependencies(monkeypatch)

    decision = _decision()

    assert decision["allowed"] is True


def test_cancelling_old_token_cannot_open_superseding_stop_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _barrier(action="start", target=4, complete=False)
    _barrier(action="stop", target=0, complete=False)
    admission.cancel_lifecycle_barrier(old.token, reason="start_failure_live_reconciled")
    _ready_dependencies(monkeypatch)

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "aks_stop_in_progress"


def test_only_newer_lifecycle_generation_interrupts_existing_job() -> None:
    barrier = _barrier(action="scale", target=4)

    interrupted = admission.lifecycle_barrier_interrupts_job(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        job_created_at="2020-01-01T00:00:00Z",
    )
    newer_job = admission.lifecycle_barrier_interrupts_job(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        job_created_at="2099-01-01T00:00:00Z",
    )

    assert interrupted == barrier
    assert newer_job is None

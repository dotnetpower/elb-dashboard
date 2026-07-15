"""Tests for lifecycle-aware Service Bus execution admission.

Responsibility: Verify durable lifecycle barriers keep request messages queued until target
    nodes and correlated post-lifecycle database warmups are complete.
Edit boundaries: Azure ARM, Kubernetes, Redis, Table Storage, and JobState are faked; no live
    cloud or broker access is allowed.
Key entry points: `_ready_dependencies`, `test_*`.
Risky contracts: Stop/delete barriers always block; start/scale barriers require exact target
    node convergence and completed correlated warmup jobs; newer lifecycle generations alone may
    terminalise an otherwise orphaned external job.
Validation: `uv run pytest -q api/tests/test_execution_admission.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api.services.aks import execution_admission as admission
from api.services.aks import execution_admission_state as admission_state


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
        lambda: SimpleNamespace(list_active=lambda **_kwargs: []),
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
            get_many=lambda _ids: {"warm-1": SimpleNamespace(status=status)},
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
            get_many=lambda _ids: {"warm-1": SimpleNamespace(status="completed")},
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
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: SimpleNamespace(
            list_active=lambda **_kwargs: [
                SimpleNamespace(
                    job_id="manual-warmup-1",
                    subscription_id="sub-1",
                    resource_group="rg-elb",
                    cluster_name="aks-elb",
                    payload={},
                )
            ]
        ),
    )

    decision = _decision()

    assert decision["allowed"] is False
    assert decision["reason"] == "database_warmup_in_progress"


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


def test_cancelled_enqueue_generation_no_longer_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _barrier(action="stop", target=0)
    admission.cancel_lifecycle_barrier(barrier.token, reason="broker_unavailable")
    _ready_dependencies(monkeypatch)

    decision = _decision()

    assert decision["allowed"] is True


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

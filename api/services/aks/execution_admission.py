"""Strict execution-readiness decisions for the Service Bus request queue.

Responsibility: Combine lifecycle barrier state, ARM/Kubernetes node convergence, configured
    database warmup state, and correlated warmup JobState into one fail-closed admission decision.
Edit boundaries: Durable record I/O belongs in `execution_admission_state.py`; queue receive,
    settlement, and lifecycle side effects remain in their callers.
Key entry points: `evaluate_execution_admission`; state primitives are re-exported for callers
    that create lifecycle generations or correlate warmup jobs.
Risky contracts: Stop/delete always deny. Start/scale require ARM completion, exact target node
    convergence, strict (not degraded) DB readiness, and completed token-correlated warmup jobs.
    The short process cache includes lifecycle and warmup fingerprints so a new barrier invalidates
    an earlier allow decision before another queue message is submitted.
Validation: `uv run pytest -q api/tests/test_execution_admission.py
    api/tests/test_servicebus_tasks.py api/tests/test_resident_consumer.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, TypedDict, cast

from api.services.aks.execution_admission_state import (
    ExecutionAdmissionPersistenceError,
    LifecycleBarrier,
    barrier_cancelled,
    cancel_lifecycle_barrier,
    clear_barrier_warmup_job,
    create_lifecycle_barrier,
    get_barrier_warmup_jobs,
    get_lifecycle_barrier,
    lifecycle_barrier_interrupts_job,
    lifecycle_completed,
    lifecycle_failure,
    record_barrier_warmup_jobs,
    record_lifecycle_completed,
    record_lifecycle_failed,
    reset_execution_admission_state_for_tests,
)

LOGGER = logging.getLogger(__name__)

_CACHE_SECONDS = max(0.0, float(os.environ.get("SERVICEBUS_ADMISSION_CACHE_SECONDS", "2")))
_RETRY_SECONDS = max(1, int(os.environ.get("SERVICEBUS_ADMISSION_RETRY_SECONDS", "10")))


class AdmissionDecision(TypedDict, total=False):
    allowed: bool
    reason: str
    retry_after_seconds: int
    lifecycle_action: str
    barrier_token: str
    target_node_count: int
    ready_node_count: int
    warmup_jobs: dict[str, str]
    failed_warmup_jobs: dict[str, str]
    detail: str


_DECISION_CACHE: dict[tuple[str, str, str, str, str], tuple[float, AdmissionDecision]] = {}
_DECISION_CACHE_LOCK = threading.Lock()


def _invalidate_decisions() -> None:
    with _DECISION_CACHE_LOCK:
        _DECISION_CACHE.clear()


def _denied(
    reason: str,
    *,
    barrier: LifecycleBarrier | None,
    detail: str = "",
    **extra: Any,
) -> AdmissionDecision:
    decision: AdmissionDecision = {
        "allowed": False,
        "reason": reason,
        "retry_after_seconds": _RETRY_SECONDS,
    }
    if barrier is not None:
        decision.update(
            {
                "lifecycle_action": barrier.action,
                "barrier_token": barrier.token,
                "target_node_count": barrier.target_node_count,
            }
        )
    if detail:
        decision["detail"] = detail[:300]
    return cast(AdmissionDecision, {**decision, **extra})


def _active_cluster_warmup_jobs(
    subscription_id: str, resource_group: str, cluster_name: str
) -> list[str]:
    """Return active warmup JobState IDs scoped to the target cluster."""
    from api.services.state_repo import get_state_repo

    rows = get_state_repo().list_active(job_type="warmup", limit=200)
    active: list[str] = []
    for row in rows:
        payload = getattr(row, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        row_subscription = str(
            getattr(row, "subscription_id", "") or payload.get("subscription_id") or ""
        )
        row_resource_group = str(
            getattr(row, "resource_group", "") or payload.get("resource_group") or ""
        )
        row_cluster = str(
            getattr(row, "cluster_name", "") or payload.get("cluster_name") or ""
        )
        if (
            row_subscription == subscription_id
            and row_resource_group == resource_group
            and row_cluster == cluster_name
        ):
            active.append(str(getattr(row, "job_id", "") or ""))
    return [job_id for job_id in active if job_id]


def _evaluate_uncached(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    barrier: LifecycleBarrier | None,
    lifecycle_failure_state: dict[str, Any] | None,
) -> AdmissionDecision:
    if not all((subscription_id, resource_group, cluster_name)):
        return _denied(
            "cluster_context_unavailable",
            barrier=barrier,
            detail="Service Bus routing does not identify exactly one AKS cluster",
        )
    if barrier is not None:
        if barrier.action in {"stop", "delete"}:
            return _denied(
                f"aks_{barrier.action}_in_progress",
                barrier=barrier,
                detail="cluster lifecycle keeps request messages queued",
            )
        if lifecycle_failure_state is not None:
            return _denied(
                f"aks_{barrier.action}_failed",
                barrier=barrier,
                detail=(
                    "AKS lifecycle task failed; retry the lifecycle action before "
                    "queued requests can run"
                ),
            )
        if not lifecycle_completed(barrier.token):
            return _denied(
                f"aks_{barrier.action}_in_progress",
                barrier=barrier,
                detail="AKS lifecycle operation has not reported ARM convergence",
            )
    try:
        from api.services import get_credential
        from api.services.aks.ensure_running import evaluate_ensure_running
        from api.services.k8s.monitoring import k8s_ready_warmup_node_names
        from api.services.monitoring import get_aks_cluster_snapshot

        credential = get_credential()
        readiness = evaluate_ensure_running(
            credential,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
        if readiness["status"] != "ready":
            return _denied(
                f"cluster_{readiness['status']}",
                barrier=barrier,
                detail=readiness.get("reason") or "cluster is not execution-ready",
            )
        warmup = readiness.get("warmup") or {}
        if str(warmup.get("phase") or "") == "ready_degraded":
            return _denied(
                "database_warmup_failed",
                barrier=barrier,
                detail=readiness.get("reason") or "database warmup failed",
            )

        active_warmups = _active_cluster_warmup_jobs(
            subscription_id, resource_group, cluster_name
        )
        if active_warmups:
            return _denied(
                "database_warmup_in_progress",
                barrier=barrier,
                detail=(
                    f"{len(active_warmups)} database warmup task(s) are queued or running"
                ),
                warmup_jobs={job_id: job_id for job_id in active_warmups},
            )

        snapshot = get_aks_cluster_snapshot(
            credential, subscription_id, resource_group, cluster_name
        )
        if snapshot is None:
            return _denied("cluster_snapshot_unavailable", barrier=barrier)
        target = (
            barrier.target_node_count
            if barrier is not None
            else int(snapshot.get("node_count") or 0)
        )
        live_count = int(snapshot.get("node_count") or 0)
        if target > 0 and live_count != target:
            return _denied(
                "aks_scaling",
                barrier=barrier,
                detail=f"workload pool reports {live_count}/{target} target nodes",
            )
        ready_nodes = k8s_ready_warmup_node_names(
            credential, subscription_id, resource_group, cluster_name
        )
        if target > 0 and len(ready_nodes) < target:
            return _denied(
                "waiting_for_target_nodes",
                barrier=barrier,
                detail=f"Kubernetes reports {len(ready_nodes)}/{target} Ready workload nodes",
                ready_node_count=len(ready_nodes),
            )

        if barrier is not None and barrier.databases:
            jobs = get_barrier_warmup_jobs(barrier.token, barrier.databases)
            missing = [name for name in barrier.databases if not jobs.get(name)]
            if missing:
                return _denied(
                    "database_warmup_pending",
                    barrier=barrier,
                    detail="post-lifecycle warmup has not been enqueued for: " + ", ".join(missing),
                    warmup_jobs=jobs,
                )
            from api.services.state_repo import get_state_repo

            rows = get_state_repo().get_many([jobs[name] for name in barrier.databases])
            failed: dict[str, str] = {}
            active: dict[str, str] = {}
            for name in barrier.databases:
                job_id = jobs[name]
                row = rows.get(job_id)
                status = str(getattr(row, "status", "") or "missing").lower()
                if status == "failed":
                    failed[name] = job_id
                elif status != "completed":
                    active[name] = job_id
            if failed:
                return _denied(
                    "database_warmup_failed",
                    barrier=barrier,
                    detail="post-lifecycle warmup failed; requests remain queued",
                    warmup_jobs=jobs,
                    failed_warmup_jobs=failed,
                )
            if active:
                return _denied(
                    "database_warmup_in_progress",
                    barrier=barrier,
                    detail="post-lifecycle warmup jobs are still active",
                    warmup_jobs=jobs,
                )
        return {
            "allowed": True,
            "reason": "ready",
            "retry_after_seconds": 0,
            **(
                {
                    "lifecycle_action": barrier.action,
                    "barrier_token": barrier.token,
                    "target_node_count": barrier.target_node_count,
                }
                if barrier is not None
                else {}
            ),
        }
    except Exception as exc:
        LOGGER.warning(
            "execution admission evaluation failed cluster=%s error=%s",
            cluster_name,
            type(exc).__name__,
        )
        return _denied(
            "readiness_check_failed",
            barrier=barrier,
            detail=type(exc).__name__,
        )


def evaluate_execution_admission(
    *, subscription_id: str, resource_group: str, cluster_name: str
) -> AdmissionDecision:
    """Return a fail-closed decision for Service Bus queue consumption."""
    try:
        barrier = get_lifecycle_barrier(subscription_id, resource_group, cluster_name)
        if barrier is not None and barrier_cancelled(barrier.token):
            barrier = None
        token = barrier.token if barrier is not None else ""
        warmup_jobs = (
            get_barrier_warmup_jobs(token, barrier.databases)
            if token and barrier is not None
            else {}
        )
        completed = lifecycle_completed(token) if token else False
        failure_state = lifecycle_failure(token) if token else None
    except Exception as exc:
        LOGGER.warning(
            "execution admission state read failed cluster=%s error=%s",
            cluster_name,
            type(exc).__name__,
        )
        return _denied(
            "execution_admission_state_unavailable",
            barrier=None,
            detail=type(exc).__name__,
        )
    warmup_fingerprint = hashlib.sha256(
        json.dumps(warmup_jobs, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    cache_key = (
        subscription_id,
        resource_group,
        cluster_name,
        token,
        f"{int(completed)}:{int(failure_state is not None)}:{warmup_fingerprint}",
    )
    now = time.monotonic()
    if _CACHE_SECONDS > 0:
        with _DECISION_CACHE_LOCK:
            cached = _DECISION_CACHE.get(cache_key)
        if cached is not None and now - cached[0] < _CACHE_SECONDS:
            return dict(cached[1])  # type: ignore[return-value]
    decision = _evaluate_uncached(
        subscription_id,
        resource_group,
        cluster_name,
        barrier,
        failure_state,
    )
    # Never cache an allow decision. A manual DB re-warm can begin without a
    # new lifecycle token, and even a two-second stale allow would let the
    # resident consumer remove messages during that transition. Short-lived
    # deny caching is safe: it only keeps work queued slightly longer.
    if _CACHE_SECONDS > 0 and not decision.get("allowed"):
        with _DECISION_CACHE_LOCK:
            _DECISION_CACHE[cache_key] = (now, decision)
    return decision


def reset_execution_admission_for_tests() -> None:
    """Clear evaluator and process-local state caches for isolated tests."""
    reset_execution_admission_state_for_tests()
    _invalidate_decisions()


__all__ = [
    "AdmissionDecision",
    "ExecutionAdmissionPersistenceError",
    "LifecycleBarrier",
    "cancel_lifecycle_barrier",
    "clear_barrier_warmup_job",
    "create_lifecycle_barrier",
    "evaluate_execution_admission",
    "get_barrier_warmup_jobs",
    "get_lifecycle_barrier",
    "lifecycle_barrier_interrupts_job",
    "record_barrier_warmup_jobs",
    "record_lifecycle_completed",
    "record_lifecycle_failed",
    "reset_execution_admission_for_tests",
]

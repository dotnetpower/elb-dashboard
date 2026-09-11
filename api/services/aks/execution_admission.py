"""Strict execution-readiness decisions for the Service Bus request queue.

Responsibility: Combine lifecycle barrier state, ARM/Kubernetes node convergence, configured
    database warmup state, and correlated warmup JobState into one fail-closed admission decision.
Edit boundaries: Durable record I/O belongs in `execution_admission_state.py`; queue receive,
    settlement, and lifecycle side effects remain in their callers.
Key entry points: `evaluate_execution_admission`; state primitives are re-exported for callers
    that create lifecycle generations or correlate warmup jobs.
Risky contracts: Stop/delete always deny. Start/scale require ARM completion, exact target node
    convergence, strict (not degraded) DB readiness, and completed token-correlated warmup jobs.
    A terminal start failure may reconcile without its failed token only when the same exact node
    convergence plus authoritative current-node warmup Jobs and one DB generation are live-proven.
    The short process cache includes lifecycle and warmup fingerprints so a new barrier invalidates
    an earlier allow decision before another queue message is submitted. Celery soft deadlines are
    process-control signals and must propagate rather than becoming a cached deny decision.
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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypedDict, cast

from billiard.exceptions import SoftTimeLimitExceeded

from api.services.aks.execution_admission_state import (
    ExecutionAdmissionPersistenceError,
    LifecycleBarrier,
    barrier_cancelled,
    cancel_lifecycle_barrier,
    clear_active_warmup_job,
    clear_barrier_warmup_job,
    create_lifecycle_barrier,
    get_barrier_warmup_jobs,
    get_lifecycle_barrier,
    lifecycle_barrier_interrupts_job,
    lifecycle_completed,
    lifecycle_failure,
    list_active_warmup_markers,
    record_active_warmup_job,
    record_barrier_warmup_jobs,
    record_lifecycle_completed,
    record_lifecycle_failed,
    reset_execution_admission_state_for_tests,
)

LOGGER = logging.getLogger(__name__)

_CACHE_SECONDS = max(0.0, float(os.environ.get("SERVICEBUS_ADMISSION_CACHE_SECONDS", "2")))
_RETRY_SECONDS = max(1, int(os.environ.get("SERVICEBUS_ADMISSION_RETRY_SECONDS", "10")))
_ACTIVE_WARMUP_MARKER_STALE_SECONDS = max(
    300,
    int(os.environ.get("EXECUTION_ADMISSION_WARMUP_MARKER_STALE_SECONDS", "7200")),
)


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
    recovered_lifecycle_failure: bool
    recovery_blocker: str


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
    """Return marker-correlated active warmups without scanning JobState."""
    from api.services.state_repo import get_state_repo

    markers = list_active_warmup_markers(subscription_id, resource_group, cluster_name)
    if not markers:
        return []
    rows = get_state_repo().get_many(
        list(markers),
        select=["PartitionKey", "RowKey", "status"],
    )
    active: list[str] = []
    now = datetime.now(UTC)
    terminal_statuses = {"completed", "failed", "cancelled", "deleted", "succeeded"}
    for job_id, marker in markers.items():
        row = rows.get(job_id)
        if row is None:
            registered_at = str(marker.get("registered_at") or "")
            try:
                registered = datetime.fromisoformat(registered_at.replace("Z", "+00:00"))
                if registered.tzinfo is None:
                    registered = registered.replace(tzinfo=UTC)
                stale = (now - registered).total_seconds() >= _ACTIVE_WARMUP_MARKER_STALE_SECONDS
            except (TypeError, ValueError):
                stale = False
            if stale:
                clear_active_warmup_job(
                    subscription_id=subscription_id,
                    resource_group=resource_group,
                    cluster_name=cluster_name,
                    job_id=job_id,
                )
                LOGGER.warning(
                    "execution admission cleared stale orphan warmup marker job_id=%s",
                    job_id,
                )
                continue
            active.append(job_id)
            continue
        status = str(getattr(row, "status", "") or "").strip().lower()
        if status in terminal_statuses:
            clear_active_warmup_job(
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
                job_id=job_id,
            )
            continue
        active.append(job_id)
    return active


def _failed_start_warmup_recovered(
    credential: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    databases: tuple[str, ...],
    expected_node_count: int,
) -> tuple[bool, str]:
    """Prove failed-start databases are warm on the current Ready node set."""
    if not databases:
        return True, ""
    from api.services.auto_warmup_reconcile import warmup_status_by_db
    from api.services.monitoring import k8s_warmup_status

    status = k8s_warmup_status(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
    )
    by_database = warmup_status_by_db(status.get("databases", []) or [])
    required_nodes = max(1, expected_node_count)
    for database in databases:
        item = by_database.get(database) or {}
        sources = {str(value) for value in item.get("sources", []) or []}
        source_versions = {
            str(value) for value in item.get("source_versions", []) or [] if str(value)
        }
        source_version = str(item.get("source_version") or "").strip()
        if source_version:
            source_versions.add(source_version)
        ready_nodes = int(item.get("nodes_ready") or 0)
        total_jobs = int(item.get("total_jobs") or 0)
        if (
            str(item.get("status") or "") != "Ready"
            or "warmup" not in sources
            or len(source_versions) != 1
            or ready_nodes < required_nodes
            or total_jobs < required_nodes
            or int(item.get("nodes_active") or 0) > 0
            or int(item.get("nodes_failed") or 0) > 0
        ):
            return (
                False,
                f"current warmup proof is incomplete for {database} "
                f"({ready_nodes}/{required_nodes} Ready nodes)",
            )
    return True, ""


def _evaluate_uncached(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    barrier: LifecycleBarrier | None,
    lifecycle_failure_state: dict[str, Any] | None,
    *,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AdmissionDecision:
    if not all((subscription_id, resource_group, cluster_name)):
        return _denied(
            "cluster_context_unavailable",
            barrier=barrier,
            detail="Service Bus routing does not identify exactly one AKS cluster",
        )
    recovering_start_failure = False
    if barrier is not None:
        if barrier.action in {"stop", "delete"}:
            return _denied(
                f"aks_{barrier.action}_in_progress",
                barrier=barrier,
                detail="cluster lifecycle keeps request messages queued",
            )
        if lifecycle_failure_state is not None:
            if barrier.action != "start":
                return _denied(
                    f"aks_{barrier.action}_failed",
                    barrier=barrier,
                    detail=(
                        "AKS lifecycle task failed; retry the lifecycle action before "
                        "queued requests can run"
                    ),
                )
            recovering_start_failure = True
        elif not lifecycle_completed(barrier.token):
            return _denied(
                f"aks_{barrier.action}_in_progress",
                barrier=barrier,
                detail="AKS lifecycle operation has not reported ARM convergence",
            )

    def runtime_denied(
        reason: str,
        *,
        detail: str = "",
        **extra: Any,
    ) -> AdmissionDecision:
        if recovering_start_failure:
            recovery_detail = "AKS start failed; live recovery is not yet proven"
            if detail:
                recovery_detail += f": {detail}"
            return _denied(
                "aks_start_failed",
                barrier=barrier,
                detail=recovery_detail,
                recovery_blocker=reason,
                **extra,
            )
        return _denied(reason, barrier=barrier, detail=detail, **extra)

    def budget_exhausted() -> bool:
        return deadline_monotonic is not None and clock() >= deadline_monotonic

    def budget_denied() -> AdmissionDecision:
        return _denied(
            "admission_budget_exhausted",
            barrier=barrier,
            detail="execution admission deferred before the task settlement reserve",
        )

    try:
        if budget_exhausted():
            return budget_denied()
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
        if budget_exhausted():
            return budget_denied()
        if readiness["status"] != "ready":
            return runtime_denied(
                f"cluster_{readiness['status']}",
                detail=readiness.get("reason") or "cluster is not execution-ready",
            )
        warmup = readiness.get("warmup") or {}
        if str(warmup.get("phase") or "") == "ready_degraded":
            return runtime_denied(
                "database_warmup_failed",
                detail=readiness.get("reason") or "database warmup failed",
            )

        active_warmups = _active_cluster_warmup_jobs(subscription_id, resource_group, cluster_name)
        if budget_exhausted():
            return budget_denied()
        if active_warmups:
            return runtime_denied(
                "database_warmup_in_progress",
                detail=(f"{len(active_warmups)} database warmup task(s) are queued or running"),
                warmup_jobs={job_id: job_id for job_id in active_warmups},
            )

        snapshot = get_aks_cluster_snapshot(
            credential, subscription_id, resource_group, cluster_name
        )
        if budget_exhausted():
            return budget_denied()
        if snapshot is None:
            return runtime_denied("cluster_snapshot_unavailable")
        target = (
            barrier.target_node_count
            if barrier is not None
            else int(snapshot.get("node_count") or 0)
        )
        live_count = int(snapshot.get("node_count") or 0)
        if target > 0 and live_count != target:
            return runtime_denied(
                "aks_scaling",
                detail=f"workload pool reports {live_count}/{target} target nodes",
            )
        ready_nodes = k8s_ready_warmup_node_names(
            credential, subscription_id, resource_group, cluster_name
        )
        if budget_exhausted():
            return budget_denied()
        if target > 0 and len(ready_nodes) < target:
            return runtime_denied(
                "waiting_for_target_nodes",
                detail=f"Kubernetes reports {len(ready_nodes)}/{target} Ready workload nodes",
                ready_node_count=len(ready_nodes),
            )

        if barrier is not None and barrier.databases:
            if recovering_start_failure:
                if budget_exhausted():
                    return budget_denied()
                warmup_recovered, recovery_detail = _failed_start_warmup_recovered(
                    credential,
                    subscription_id=subscription_id,
                    resource_group=resource_group,
                    cluster_name=cluster_name,
                    databases=barrier.databases,
                    expected_node_count=target or len(ready_nodes),
                )
                if budget_exhausted():
                    return budget_denied()
                if not warmup_recovered:
                    return runtime_denied(
                        "database_warmup_pending",
                        detail=recovery_detail,
                    )
                return {
                    "allowed": True,
                    "reason": "ready",
                    "retry_after_seconds": 0,
                    "lifecycle_action": barrier.action,
                    "barrier_token": barrier.token,
                    "target_node_count": barrier.target_node_count,
                    "recovered_lifecycle_failure": True,
                }
            jobs = get_barrier_warmup_jobs(barrier.token, barrier.databases)
            missing = [name for name in barrier.databases if not jobs.get(name)]
            if missing:
                return runtime_denied(
                    "database_warmup_pending",
                    detail="post-lifecycle warmup has not been enqueued for: " + ", ".join(missing),
                    warmup_jobs=jobs,
                )
            from api.services.state_repo import get_state_repo

            if budget_exhausted():
                return budget_denied()
            rows = get_state_repo().get_many([jobs[name] for name in barrier.databases])
            if budget_exhausted():
                return budget_denied()
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
                return runtime_denied(
                    "database_warmup_failed",
                    detail="post-lifecycle warmup failed; requests remain queued",
                    warmup_jobs=jobs,
                    failed_warmup_jobs=failed,
                )
            if active:
                return runtime_denied(
                    "database_warmup_in_progress",
                    detail="post-lifecycle warmup jobs are still active",
                    warmup_jobs=jobs,
                )
        decision: AdmissionDecision = {
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
        if recovering_start_failure:
            decision["recovered_lifecycle_failure"] = True
        return decision
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        LOGGER.warning(
            "execution admission evaluation failed cluster=%s error=%s",
            cluster_name,
            type(exc).__name__,
        )
        return runtime_denied(
            "readiness_check_failed",
            detail=type(exc).__name__,
        )


def evaluate_execution_admission(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    deadline_monotonic: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AdmissionDecision:
    """Return a fail-closed decision for Service Bus queue consumption."""

    def budget_exhausted() -> bool:
        return deadline_monotonic is not None and clock() >= deadline_monotonic

    def budget_denied(barrier: LifecycleBarrier | None = None) -> AdmissionDecision:
        return _denied(
            "admission_budget_exhausted",
            barrier=barrier,
            detail="execution admission had no remaining caller budget",
        )

    if budget_exhausted():
        return budget_denied()
    try:
        barrier = get_lifecycle_barrier(subscription_id, resource_group, cluster_name)
        if budget_exhausted():
            return budget_denied(barrier)
        if barrier is not None and barrier_cancelled(barrier.token):
            barrier = None
        if budget_exhausted():
            return budget_denied(barrier)
        token = barrier.token if barrier is not None else ""
        warmup_jobs = (
            get_barrier_warmup_jobs(token, barrier.databases)
            if token and barrier is not None
            else {}
        )
        if budget_exhausted():
            return budget_denied(barrier)
        completed = lifecycle_completed(token) if token else False
        if budget_exhausted():
            return budget_denied(barrier)
        failure_state = lifecycle_failure(token) if token else None
        if budget_exhausted():
            return budget_denied(barrier)
    except SoftTimeLimitExceeded:
        raise
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
        deadline_monotonic=deadline_monotonic,
        clock=clock,
    )
    # Never cache an allow decision. A manual DB re-warm can begin without a
    # new lifecycle token, and even a two-second stale allow would let the
    # resident consumer remove messages during that transition. Short-lived
    # deny caching is safe: it only keeps work queued slightly longer.
    if (
        _CACHE_SECONDS > 0
        and not decision.get("allowed")
        and decision.get("reason") != "admission_budget_exhausted"
    ):
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
    "clear_active_warmup_job",
    "clear_barrier_warmup_job",
    "create_lifecycle_barrier",
    "evaluate_execution_admission",
    "get_barrier_warmup_jobs",
    "get_lifecycle_barrier",
    "lifecycle_barrier_interrupts_job",
    "record_active_warmup_job",
    "record_barrier_warmup_jobs",
    "record_lifecycle_completed",
    "record_lifecycle_failed",
    "reset_execution_admission_for_tests",
]

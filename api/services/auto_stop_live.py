"""Live Kubernetes workload activity probe for AKS auto-stop.

Responsibility: Best-effort, read-only probe that reports whether a Running AKS
    cluster has nonterminal BLAST, DB warmup, or prepare-db Kubernetes work.
Edit boundaries: Direct, filtered Kubernetes list calls only. No ARM stop calls,
    no Celery, no state writes. Returns ``None`` on ANY failure
    so callers degrade to the state_repo-only decision — a permanently
    unreachable cluster must still be stoppable. This is an *additive*
    protection signal only, never a hard "keep forever".
Key entry points: `probe_live_cluster_activity`, `probe_live_blast_activity`.
Risky contracts: The ``(live_active_jobs, live_latest_activity)`` tuple is
    injected into `auto_stop_evaluator.evaluate_cluster` as
    ``live_active_jobs`` / ``live_latest_activity``. Over-reporting activity
    would strand a cluster running forever, so only Jobs without a terminal
    Complete/Failed condition (or nonterminal Pods in a narrow race) count;
    completed/failed objects that linger do NOT.
Validation: `uv run pytest -q api/tests/test_auto_stop_live.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from api.services.auto_stop import AutoStopPreference

LOGGER = logging.getLogger(__name__)

_WORKLOAD_JOB_SELECTORS = (
    "app=blast",
    "app=elb-db-warmup",
    "app=elb-prepare-db",
)


def _parse_k8s_ts(value: object) -> datetime | None:
    """Best-effort K8s ISO 8601 timestamp → aware UTC datetime, else None."""
    if not value:
        return None
    text = str(value)
    try:
        text = text.replace("Z", "+00:00") if text.endswith("Z") else text
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except (TypeError, ValueError):
        return None


def _job_is_terminal(job: dict[str, Any]) -> bool:
    status = job.get("status", {}) or {}
    try:
        if int(status.get("succeeded") or 0) > 0:
            return True
    except (TypeError, ValueError):
        return False
    if status.get("completionTime"):
        return True
    return any(
        isinstance(condition, dict)
        and condition.get("type") in {"Complete", "Failed"}
        and str(condition.get("status") or "").lower() == "true"
        for condition in status.get("conditions", []) or []
    )


def _latest_object_timestamp(items: list[dict[str, Any]]) -> datetime | None:
    latest: datetime | None = None
    for item in items:
        metadata = item.get("metadata", {}) or {}
        status = item.get("status", {}) or {}
        values = [
            metadata.get("creationTimestamp"),
            status.get("startTime"),
            status.get("completionTime"),
        ]
        values.extend(
            condition.get("lastTransitionTime")
            for condition in status.get("conditions", []) or []
            if isinstance(condition, dict)
        )
        for value in values:
            parsed = _parse_k8s_ts(value)
            if parsed is not None and (latest is None or parsed > latest):
                latest = parsed
    return latest


def _probe_cluster_workload_status(
    pref: AutoStopPreference,
    namespace: str,
) -> dict[str, Any] | None:
    """Fetch only nonterminal workload objects from Kubernetes.

    The cluster can retain tens of thousands of completed ``app=blast`` Jobs.
    Listing all of them made one auto-stop status probe download nearly 1 GB
    and take 36-88 seconds. Kubernetes supports ``status.successful=0`` for
    Jobs and phase inequality selectors for Pods, reducing the live read to the
    small set that can still be running or failed-without-success.
    """

    from api.services import get_credential
    from api.services.k8s.fanout import _k8s_fanout_pool
    from api.services.k8s.monitoring import _get_k8s_session, _namespace_or_default

    session, server = _get_k8s_session(
        get_credential(),
        pref.subscription_id,
        pref.resource_group,
        pref.cluster_name,
    )
    try:
        target_ns = _namespace_or_default(session, server, namespace)

        def _get(path: str, params: dict[str, str]) -> Any:
            return session.get(f"{server}{path}", params=params, timeout=10)

        pool = _k8s_fanout_pool()
        jobs_path = f"/apis/batch/v1/namespaces/{target_ns}/jobs"
        job_futures = [
            pool.submit(
                _get,
                jobs_path,
                {
                    "labelSelector": selector,
                    "fieldSelector": "status.successful=0",
                },
            )
            for selector in _WORKLOAD_JOB_SELECTORS
        ]
        pods_path = f"/api/v1/namespaces/{target_ns}/pods"
        pod_futures = [
            pool.submit(
                _get,
                pods_path,
                {
                    "labelSelector": selector,
                    "fieldSelector": "status.phase!=Succeeded,status.phase!=Failed",
                },
            )
            for selector in _WORKLOAD_JOB_SELECTORS
        ]

        jobs: list[dict[str, Any]] = []
        for future in job_futures:
            response = future.result()
            if response.status_code != 200:
                return None
            jobs.extend(response.json().get("items", []))
        live_pods: list[dict[str, Any]] = []
        for future in pod_futures:
            pod_response = future.result()
            if pod_response.status_code != 200:
                return None
            live_pods.extend(pod_response.json().get("items", []))

        active_jobs = [job for job in jobs if not _job_is_terminal(job)]
        active = len(active_jobs)
        if live_pods and active == 0:
            active = 1

        latest = _latest_object_timestamp(jobs + live_pods)
        if active > 0:
            # Observation heartbeat: a long job that completes between ticks
            # retains almost the full idle grace from its last observed work.
            latest = datetime.now(UTC)
        return {
            "status": "running" if active > 0 else ("completed" if latest else "creating"),
            "active": active,
            "pods": len(live_pods),
            "jobs": len(jobs),
            "completed_at": latest.isoformat() if latest and active == 0 else None,
            "started_at": latest.isoformat() if latest and active > 0 else None,
        }
    finally:
        session.close()


def probe_live_cluster_activity(
    pref: AutoStopPreference,
    *,
    namespace: str = "",
) -> tuple[int, datetime | None] | None:
    """Probe live BLAST / warmup / prepare-db activity for one cluster.

    Returns ``(live_active_jobs, live_latest_activity)`` or ``None``.

    ``None`` means "could not determine" (K8s unreachable, kubeconfig fetch
    failed, or the helper returned ``status == 'unknown'``). The caller MUST
    fall back to the state_repo-only decision so an unreachable cluster is
    not stranded running forever.

    ``live_active_jobs > 0`` means the cluster has in-flight BLAST, warmup, or
    prepare-db work and must be kept alive. ``live_latest_activity`` is the
    most recent observed workload timestamp used to reset the idle clock.

    Only call this for a cluster whose ARM ``power_state == 'Running'`` — a
    stopped cluster has no API server to query.
    """
    try:
        status = _probe_cluster_workload_status(pref, namespace)
    except Exception as exc:
        LOGGER.debug(
            "auto_stop live workload probe failed cluster=%s: %s",
            pref.cluster_name,
            exc,
        )
        return None

    if not isinstance(status, dict):
        return None

    state = str(status.get("status") or "")
    if state == "unknown":
        # Treat unknown as "could not determine" → fall back to durable state
        # rather than blocking forever on a transient probe failure.
        return None

    active = int(status.get("active") or 0)
    pods = int(status.get("pods") or 0)
    jobs = int(status.get("jobs") or 0)

    # In-use predicate. ``active`` (sum of K8s job ``status.active``) is the
    # primary signal. A run in the ``creating`` / ``running`` phase that has
    # a Job or Pod object but no started pod yet (active == 0) is ALSO in
    # use — it is a just-submitted BLAST about to start. A ``completed`` /
    # ``failed`` run is intentionally NOT counted: its pods may linger until
    # the user deletes the run, so blocking on their mere presence would
    # strand the cluster forever. Those runs instead seed ``latest`` below
    # so the cluster gets the normal idle grace after the burst finishes.
    in_use = active > 0 or (state in {"running", "creating"} and (pods > 0 or jobs > 0))
    live_active = active if active > 0 else (1 if in_use else 0)

    latest: datetime | None = None
    for key in ("started_at", "completed_at"):
        ts = _parse_k8s_ts(status.get(key))
        if ts is not None and (latest is None or ts > latest):
            latest = ts

    return live_active, latest


def probe_live_blast_activity(
    pref: AutoStopPreference,
    *,
    namespace: str = "",
) -> tuple[int, datetime | None] | None:
    """Backward-compatible alias for the broadened cluster workload probe."""

    return probe_live_cluster_activity(pref, namespace=namespace)


__all__ = ["probe_live_blast_activity", "probe_live_cluster_activity"]

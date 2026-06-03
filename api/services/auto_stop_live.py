"""Live Kubernetes BLAST activity probe for AKS auto-stop.

Responsibility: Best-effort, read-only probe that reports whether a Running
    AKS cluster currently has live ElasticBLAST (``app=blast``) activity —
    including OpenAPI-submitted runs that never touch the dashboard's
    jobstate Table — so the auto-stop evaluator can keep a busy cluster
    alive and reset its idle clock.
Edit boundaries: Read-only K8s probe via `k8s_check_blast_status`. No ARM
    stop calls, no Celery, no state writes. Returns ``None`` on ANY failure
    so callers degrade to the state_repo-only decision — a permanently
    unreachable cluster must still be stoppable. This is an *additive*
    protection signal only, never a hard "keep forever".
Key entry points: `probe_live_blast_activity`.
Risky contracts: The ``(live_active_jobs, live_latest_activity)`` tuple is
    injected into `auto_stop_evaluator.evaluate_cluster` as
    ``live_active_jobs`` / ``live_latest_activity``. Over-reporting activity
    would strand a cluster running forever, so only genuinely in-flight
    runs (K8s ``status.active`` > 0, or a scheduled-but-unstarted job in the
    ``creating``/``running`` phase) count as active; ``completed``/``failed``
    runs whose pods merely linger do NOT.
Validation: `uv run pytest -q api/tests/test_auto_stop_live.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from api.services.auto_stop import AutoStopPreference

LOGGER = logging.getLogger(__name__)


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


def probe_live_blast_activity(
    pref: AutoStopPreference,
    *,
    namespace: str = "",
) -> tuple[int, datetime | None] | None:
    """Probe live ``app=blast`` activity for one cluster.

    Returns ``(live_active_jobs, live_latest_activity)`` or ``None``.

    ``None`` means "could not determine" (K8s unreachable, kubeconfig fetch
    failed, or the helper returned ``status == 'unknown'``). The caller MUST
    fall back to the state_repo-only decision so an unreachable cluster is
    not stranded running forever.

    ``live_active_jobs > 0`` means the cluster has an in-flight BLAST run (a
    genuinely running pod OR a scheduled-but-unstarted job) and must be kept
    alive. ``live_latest_activity`` is the most recent start/finish
    timestamp across all ``app=blast`` jobs/pods, used to reset the idle
    clock so a just-finished burst still gets the full idle grace before a
    stop.

    Only call this for a cluster whose ARM ``power_state == 'Running'`` — a
    stopped cluster has no API server to query.
    """
    try:
        from api.services import get_credential
        from api.services.k8s.blast_status import k8s_check_blast_status

        status = k8s_check_blast_status(
            get_credential(),
            pref.subscription_id,
            pref.resource_group,
            pref.cluster_name,
            namespace,
            job_id=None,
        )
    except Exception as exc:
        LOGGER.debug(
            "auto_stop live blast probe failed cluster=%s: %s",
            pref.cluster_name,
            exc,
        )
        return None

    if not isinstance(status, dict):
        return None

    state = str(status.get("status") or "")
    if state == "unknown":
        # ``k8s_check_blast_status`` swallows K8s/API errors and returns
        # ``{"status": "unknown", ...}``. Treat that as "could not
        # determine" → fall back to the state_repo decision rather than
        # blocking the stop forever on a transient probe failure.
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


__all__ = ["probe_live_blast_activity"]

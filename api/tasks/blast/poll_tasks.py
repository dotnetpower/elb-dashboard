"""Celery polling tasks for BLAST job status updates.

Responsibility: Two short Celery tasks that pull live status for a single
BLAST job — ``check_status`` (one-shot K8s probe used by the SPA) and
``poll_running_status`` (self-rescheduling per-job poller that closes the
K8s → dashboard latency gap right after submit).
Edit boundaries: Polling cadence constants and the two task bodies live
here. Split-mode aggregation (``_aggregate_split_child_states`` /
``_finalize_split_parent_results``) and shared helpers
(``_update_state`` / ``_snippet``) stay in ``api.tasks.blast`` and are
called through the module attribute for monkeypatch safety.
Key entry points:
  - ``check_status`` (``@shared_task`` ``name="api.tasks.blast.check_status"``).
  - ``poll_running_status`` (``@shared_task``
     ``name="api.tasks.blast.poll_running_status"``, self-reschedules).
Risky contracts: Public task names must stay
``api.tasks.blast.check_status`` and ``api.tasks.blast.poll_running_status``
(referenced from routes + submit task). ``POLL_RUNNING_*`` constants are
re-exported through ``api.tasks.blast`` because ``submit`` (still in
``__init__.py``) and tests depend on the bare attribute paths.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from api.tasks import blast as _blast

LOGGER = logging.getLogger(__name__)

# Per-job poller cadence and cap.
#
# A submit task enqueues ``poll_running_status`` with countdown=POLL_RUNNING_START_DELAY,
# and each iteration that observes a still-active row self-reschedules with
# countdown=POLL_RUNNING_INTERVAL. The cap (POLL_RUNNING_MAX_ITERATIONS) bounds
# a single submit's poll chain to ~30 minutes so we never leave a runaway
# polling chain behind if something goes sideways. The 60 s beat reconcile is
# still the safety net for any row whose poll chain ended early.
POLL_RUNNING_START_DELAY = 10
POLL_RUNNING_INTERVAL = 10
POLL_RUNNING_MAX_ITERATIONS = 180
_POLL_RUNNING_ELIGIBLE_PHASES = frozenset({"submitted", "running", "results_pending"})

__all__ = (
    "POLL_RUNNING_INTERVAL",
    "POLL_RUNNING_MAX_ITERATIONS",
    "POLL_RUNNING_START_DELAY",
    "_POLL_RUNNING_ELIGIBLE_PHASES",
    "check_status",
    "poll_running_status",
)


@shared_task(name="api.tasks.blast.check_status", bind=True)
def check_status(
    self: Any,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
) -> dict[str, Any]:
    """Check the status of a running BLAST job via the direct K8s API."""

    del self
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        children = list(repo.list_children(job_id, limit=1000))
        if children:
            aggregation = _blast._aggregate_split_child_states(
                parent_job_id=job_id,
                repo=repo,
                child_limit=1000,
            )
            if aggregation["ready_for_merge"]:
                return _blast._finalize_split_parent_results(
                    parent_job_id=job_id,
                    storage_account=storage_account,
                    repo=repo,
                    child_limit=1000,
                )
            return aggregation
    except Exception as exc:
        LOGGER.info("split parent status aggregation skipped job_id=%s: %s", job_id, exc)

    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_check_blast_status

        result = k8s_check_blast_status(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            namespace="default",
            job_id=job_id,
        )
    except Exception as exc:
        error = _blast._snippet(exc)
        LOGGER.warning("blast status check failed job_id=%s: %s", job_id, error)
        _blast._update_state(job_id, "status_unavailable", status="running", error_code=error)
        return {"job_id": job_id, "status": "unknown", "error": error}

    status = str(result.get("status", "unknown"))
    state_status = {
        "completed": "completed",
        "failed": "failed",
        "running": "running",
        "creating": "running",
    }.get(status, "running")
    _blast._update_state(job_id, status, status=state_status, k8s=result)
    return {"job_id": job_id, "status": state_status, "phase": status, "k8s": result}


@shared_task(name="api.tasks.blast.poll_running_status", bind=True)
def poll_running_status(
    self: Any,
    *,
    job_id: str,
    iteration: int = 0,
) -> dict[str, Any]:
    """Per-job poller that closes the K8s → dashboard latency gap after submit.

    The ``submit`` task enqueues this with a short countdown so the dashboard
    flips a row to ``completed`` within ~10 s of the K8s job finishing, instead
    of waiting up to 60 s for the next beat tick of ``reconcile_stale_jobs``.
    This task is idempotent: it reads the current row, asks
    ``_refresh_running_blast_state`` to do one K8s check (subject to the same
    per-job throttle the detail/list endpoints use), and self-reschedules only
    while the row is still active.
    """
    del self
    summary: dict[str, Any] = {
        "job_id": job_id,
        "iteration": iteration,
        "status": "unknown",
        "phase": "unknown",
        "rescheduled": False,
    }

    try:
        from api.services.blast_job_state import (
            _K8S_REFRESH_PHASES,
            _refresh_running_blast_state,
        )
        from api.services.state_repo import JobStateRepository
    except Exception as exc:
        LOGGER.warning("poll_running_status: dependency unavailable: %s", exc)
        return {**summary, "error": type(exc).__name__}

    try:
        repo = JobStateRepository()
        row = repo.get(job_id)
    except Exception as exc:
        LOGGER.info("poll_running_status: state lookup failed job_id=%s: %s", job_id, exc)
        return {**summary, "error": type(exc).__name__}

    if row is None:
        return {**summary, "status": "missing"}

    current_status = str(getattr(row, "status", "") or "").strip().casefold()
    current_phase = str(getattr(row, "phase", "") or "").strip().casefold()
    summary["status"] = current_status
    summary["phase"] = current_phase

    if current_status not in {"running", "pending", "queued"}:
        return summary
    if current_phase not in _K8S_REFRESH_PHASES:
        return summary

    try:
        refreshed = _refresh_running_blast_state(repo, row)
    except Exception as exc:
        LOGGER.info(
            "poll_running_status: refresh failed job_id=%s iteration=%d: %s",
            job_id,
            iteration,
            type(exc).__name__,
        )
        refreshed = row

    refreshed_status = str(getattr(refreshed, "status", "") or "").strip().casefold()
    refreshed_phase = str(getattr(refreshed, "phase", "") or "").strip().casefold()
    summary["status"] = refreshed_status
    summary["phase"] = refreshed_phase

    if refreshed_status not in {"running", "pending", "queued"}:
        return summary
    if refreshed_phase not in _K8S_REFRESH_PHASES:
        return summary
    if iteration + 1 >= POLL_RUNNING_MAX_ITERATIONS:
        LOGGER.info(
            "poll_running_status: max iterations reached job_id=%s — beat reconcile takes over",
            job_id,
        )
        return summary

    try:
        poll_running_status.apply_async(
            kwargs={"job_id": job_id, "iteration": iteration + 1},
            countdown=POLL_RUNNING_INTERVAL,
            queue="blast",
        )
        summary["rescheduled"] = True
    except Exception as exc:
        LOGGER.warning(
            "poll_running_status: reschedule failed job_id=%s iteration=%d: %s",
            job_id,
            iteration,
            type(exc).__name__,
        )
    return summary

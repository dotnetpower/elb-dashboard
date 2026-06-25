"""Celery beat sweep that auto-resubmits transient-failed BLAST jobs.

Responsibility: Periodically scan terminal-failed BLAST jobs, and for the
transient submit-phase infrastructure failures (per ``failure_classification``)
resubmit them with bounded backoff + an attempt counter, quarantining a job once
its retry budget is exhausted or its submit kwargs cannot be reconstructed.
Edit boundaries: Decision logic lives in ``api.services.blast.auto_retry``; this
module owns only the side effects (state writes, re-enqueue) and the per-sweep
bound. The master gate ``BLAST_AUTO_RETRY_ENABLED`` defaults OFF — the task
returns immediately when disabled.
Key entry points: ``auto_retry_failed_jobs`` (beat task).
Risky contracts: Enqueue happens BEFORE the ``failed -> queued`` flip so a broker
outage never strips a job out of its terminal state; the next sweep retries it
under backoff. Only ``status='failed'`` rows are acted on, so a resubmitted job
(now ``queued``/``running``) is skipped on the next sweep — this is the
double-submit guard. Beat is single-instance, so two concurrent sweeps do not race.
Validation: ``uv run pytest -q api/tests/test_blast_auto_retry_task.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from api.services.blast.auto_retry import (
    RetryDecision,
    auto_retry_enabled,
    evaluate,
    max_scan,
    merge_meta_into_payload,
    sweep_limit,
)

LOGGER = logging.getLogger(__name__)


def _emit_event(action: str, state: Any, meta: Any) -> None:
    """Best-effort App Insights custom event for an auto-retry decision."""
    try:
        from api.services.feature_events import record_feature_event

        record_feature_event(
            "blast_auto_retry",
            status=action,
            job_id=str(getattr(state, "job_id", "") or ""),
            attempt=getattr(meta, "count", 0),
            error_code=getattr(meta, "last_error_code", "") or None,
        )
    except Exception:
        return


def _quarantine(repo: Any, state: Any, decision: RetryDecision) -> bool:
    if decision.next_meta is None:
        return False
    try:
        merged = merge_meta_into_payload(getattr(state, "payload", None), decision.next_meta)
        repo.update(state.job_id, payload=merged)
        repo.append_history(
            state.job_id,
            "auto_retry_quarantined",
            {"reason": decision.reason, "auto_retry": decision.next_meta.as_dict()},
        )
        _emit_event("quarantine", state, decision.next_meta)
        return True
    except KeyError:
        return False
    except Exception as exc:
        LOGGER.warning(
            "auto-retry quarantine failed job_id=%s: %s",
            getattr(state, "job_id", "?"),
            type(exc).__name__,
        )
        return False


def _resubmit(repo: Any, state: Any, decision: RetryDecision) -> bool:
    if decision.kwargs is None or decision.next_meta is None:
        return False

    # 1) Enqueue FIRST. If the broker is down this raises and we leave the row
    #    in its terminal ``failed`` state — the next sweep retries under backoff
    #    (the attempt counter is only advanced once the flip in step 2 lands, so
    #    a failed enqueue does not consume the budget).
    try:
        from api.routes import blast as blast_package
        from api.tasks.blast.submit_task import submit

        result = blast_package._safe_delay(submit, **decision.kwargs)
        task_id = str(getattr(result, "id", "") or "")
    except Exception as exc:
        LOGGER.warning(
            "auto-retry enqueue failed job_id=%s: %s",
            getattr(state, "job_id", "?"),
            type(exc).__name__,
        )
        return False

    # 2) Flip the row to ``queued`` with the new task id + advanced counter. The
    #    enqueued submit task already owns the subsequent state transitions, so a
    #    failure here is non-fatal — the task still runs and writes its own state.
    try:
        merged = merge_meta_into_payload(getattr(state, "payload", None), decision.next_meta)
        # Drop the stale per-phase progress timeline: the failed-attempt steps
        # would otherwise linger beside the fresh run's steps. The resubmitted
        # task rebuilds it from ``preparing`` onward; jobhistory keeps the audit
        # trail of the prior attempt.
        merged.pop("_progress", None)
        repo.update(
            state.job_id,
            status="queued",
            phase="queued",
            error_code="",
            task_id=task_id,
            payload=merged,
        )
        repo.append_history(
            state.job_id,
            "auto_retry_scheduled",
            {"attempt": decision.next_meta.count, "auto_retry": decision.next_meta.as_dict()},
        )
        _emit_event("retry", state, decision.next_meta)
    except Exception as exc:
        LOGGER.warning(
            "auto-retry row flip failed (task already enqueued) job_id=%s: %s",
            getattr(state, "job_id", "?"),
            type(exc).__name__,
        )
    return True


@shared_task(name="api.tasks.blast.auto_retry_failed_jobs", bind=True)
def auto_retry_failed_jobs(self: Any, *, scan_limit: int | None = None) -> dict[str, Any]:
    """Resubmit due transient-failed BLAST jobs; quarantine exhausted ones.

    Side effects: re-enqueues ``submit`` tasks and writes jobstate rows. No-op
    when ``BLAST_AUTO_RETRY_ENABLED`` is unset. Bounded by ``sweep_limit()``
    resubmits per run and ``max_scan()`` rows read per run.
    """
    del self
    summary: dict[str, Any] = {
        "enabled": auto_retry_enabled(),
        "scanned": 0,
        "retried": 0,
        "quarantined": 0,
        "skipped": 0,
    }
    if not summary["enabled"]:
        return summary

    effective_scan = scan_limit if scan_limit else max_scan()
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        rows = repo.list_recent_failed(limit=effective_scan)
    except Exception as exc:
        LOGGER.warning("auto-retry sweep listing failed: %s", type(exc).__name__)
        return summary

    summary["scanned"] = len(rows)
    cap = sweep_limit()
    for state in rows:
        if summary["retried"] >= cap:
            break
        try:
            decision = evaluate(state)
        except Exception as exc:
            LOGGER.warning(
                "auto-retry evaluate failed job_id=%s: %s",
                getattr(state, "job_id", "?"),
                type(exc).__name__,
            )
            continue
        if decision.action == "skip":
            summary["skipped"] += 1
        elif decision.action == "quarantine":
            if _quarantine(repo, state, decision):
                summary["quarantined"] += 1
            else:
                summary["skipped"] += 1
        elif decision.action == "retry":
            if _resubmit(repo, state, decision):
                summary["retried"] += 1
            else:
                summary["skipped"] += 1

    if summary["retried"] or summary["quarantined"]:
        LOGGER.info(
            "auto-retry sweep: scanned=%d retried=%d quarantined=%d skipped=%d",
            summary["scanned"],
            summary["retried"],
            summary["quarantined"],
            summary["skipped"],
        )
    return summary

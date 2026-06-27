"""Job state persistence + Celery progress / retry bookkeeping for BLAST tasks.

Responsibility: Persist job state + history rows via ``JobStateRepository``, emit Celery
``update_state`` progress checkpoints, and orchestrate the retry-or-fail bookkeeping that
submit / cancel / reconcile tasks share.
Edit boundaries: All writes go through ``JobStateRepository``; never raise out of the
state layer (task execution must keep running on storage faults). Symbols are re-exported
from ``api.tasks.blast`` so tests can ``monkeypatch.setattr(blast, "_update_state", …)``.
Key entry points: ``_enqueue_artifact_finalizer``, ``_update_state``, ``_progress``,
``_retry_or_fail``.
Risky contracts: ``_update_state`` no-ops when status/phase/error_code are unchanged
(unless ``event``/``details`` supplied) — beat tasks rely on this to avoid churn. The
``_retry_or_fail`` helper schedules ``task.retry`` with an exponential countdown capped
at 300s; widening that cap will change observable retry latency.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from api.services.feature_events import TERMINAL_STATUSES, record_feature_event
from api.tasks import blast as _blast
from api.tasks.blast.progress import _merge_progress_payload, _phase_is_terminal_for_artifacts

LOGGER = logging.getLogger(__name__)


def _enqueue_artifact_finalizer(job_id: str, phase: str, status: str) -> None:
    if not _phase_is_terminal_for_artifacts(phase, status):
        return
    try:
        from api.services.job_artifacts import artifact_build_should_enqueue
        from api.tasks.blast_artifacts import finalize_job_artifacts

        if not artifact_build_should_enqueue(job_id, ["artifact_finalizer"]):
            return
        finalize_job_artifacts.apply_async(kwargs={"job_id": job_id})
    except Exception as exc:
        LOGGER.info(
            "artifact finalizer enqueue skipped job_id=%s: %s", job_id, type(exc).__name__
        )


def _update_state(
    job_id: str,
    phase: str,
    status: str = "running",
    *,
    event: str | None = None,
    error_code: str | None = None,
    **details: Any,
) -> None:
    """Best-effort state + history update.

    State storage must never crash the task execution path, but failures are
    visible in worker logs. History receives event-shaped payloads while the
    current row only stores compact status/phase/error fields.
    """

    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        stored_error_code = error_code or ""
        merged_payload: dict[str, Any] | None = None
        try:
            state = repo.get(job_id)
            if (
                event is None
                and not details
                and state is not None
                and str(getattr(state, "status", "") or "") == status
                and str(getattr(state, "phase", "") or "") == phase
                and str(getattr(state, "error_code", "") or "") == stored_error_code
            ):
                _blast._enqueue_artifact_finalizer(job_id, phase, status)
                return
            existing_payload = state.payload if state is not None else None
            merged_payload = _merge_progress_payload(
                existing_payload if isinstance(existing_payload, Mapping) else None,
                phase=phase,
                status=status,
                error_code=stored_error_code,
                details=details,
            )
        except Exception as exc:
            LOGGER.debug("blast progress payload merge skipped job_id=%s: %s", job_id, exc)
        update_kwargs: dict[str, Any] = {
            "status": status,
            "phase": phase,
            "error_code": stored_error_code,
        }
        if merged_payload is not None:
            update_kwargs["payload"] = merged_payload
        repo.update(job_id, **update_kwargs)
        repo.append_history(
            job_id,
            event or phase,
            {
                "phase": phase,
                "status": status,
                "error_code": stored_error_code,
                "updated_at": _blast._now_iso(),
                **details,
            },
        )
        _blast._enqueue_artifact_finalizer(job_id, phase, status)
        if status in TERMINAL_STATUSES:
            # Recover submission_source so App Insights customEvents can split
            # blast outcomes by origin (`servicebus` / `dashboard` / `external_api`)
            # for SB throughput KQL queries. Best-effort — a parse failure leaves
            # the dimension out, never blocks the terminal write.
            source: str | None = None
            try:
                from api.services.blast.external_jobs import _stored_submission_source

                stored = _stored_submission_source(state) if state is not None else ""
                source = stored or None
            except Exception:
                source = None
            record_feature_event(
                "blast",
                status=status,
                job_id=job_id,
                phase=phase,
                error_code=stored_error_code or None,
                source=source,
            )
    except Exception as exc:
        LOGGER.warning("blast state update failed job_id=%s phase=%s: %s", job_id, phase, exc)


def _progress(task: Any, phase: str, **details: Any) -> None:
    try:
        task.update_state(state="PROGRESS", meta={"phase": phase, **details})
    except Exception:
        LOGGER.debug("celery progress update failed", exc_info=True)


def _retry_or_fail(
    task: Any,
    *,
    job_id: str,
    phase: str,
    exc: BaseException,
    error_code: str,
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    request = getattr(task, "request", None)
    retries = int(getattr(request, "retries", 0) or 0)
    max_retries = getattr(task, "max_retries", 0) or 0
    if retries >= max_retries:
        error = _blast._snippet(exc)
        _blast._update_state(job_id, phase, status="failed", error_code=error_code, error=error)
        return {"job_id": job_id, "status": "failed", "phase": phase, "error": error}

    countdown = retry_after_seconds or min(300, 15 * (2**retries))
    _blast._update_state(
        job_id,
        phase,
        status="running",
        event="retry_scheduled",
        error_code=error_code,
        retry_after_seconds=countdown,
        attempt=retries + 1,
        error=_blast._snippet(exc),
    )
    raise task.retry(exc=exc, countdown=countdown)


__all__ = (
    "_enqueue_artifact_finalizer",
    "_progress",
    "_retry_or_fail",
    "_update_state",
)

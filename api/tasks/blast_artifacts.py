"""Background BLAST artifact finalization tasks.

Responsibility: Finalize terminal BLAST artifacts and reconcile missed finalizer enqueue attempts
Edit boundaries: Keep long-running side effects here; route handlers should enqueue tasks and
persist state.
Key entry points: `finalize_job_artifacts`, `reconcile_terminal_artifacts`
Risky contracts: Tasks should be idempotent, retry-aware, and write progress/state checkpoints.
Validation: `uv run pytest -q api/tests/test_blast_tasks.py
api/tests/test_job_artifacts.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

LOGGER = logging.getLogger(__name__)


# When pod log persistence returns empty (no targets discovered, fetch
# failed, or pods haven't flushed yet) we schedule one or two delayed
# retries so the snapshot eventually picks up the trailing tail. After
# `_POD_LOG_RETRY_MAX` attempts we stop trying — by then the K8s log GC
# has almost certainly evicted the pod logs anyway.
_POD_LOG_RETRY_MAX = 3
_POD_LOG_RETRY_COUNTDOWN_S = 60
_RECONCILE_ATTEMPT_MAX = 5


def _record_pod_log_capture_state(
    repo: Any,
    *,
    job_id: str,
    status: str,
    error_code: str,
    runtime_identity: str,
    attempt: int,
) -> None:
    try:
        from api.services.job_artifacts import upsert_artifact_state

        upsert_artifact_state(
            job_id,
            "pod_logs",
            status=status,
            error_code=error_code,
            runtime_identity=runtime_identity,
            reconcile_attempts=attempt,
        )
    except Exception:
        LOGGER.warning(
            "pod log capture state write failed job_id=%s status=%s",
            job_id,
            status,
            exc_info=True,
        )
    if status != "failed":
        return
    LOGGER.warning(
        "pod log capture failed job_id=%s attempt=%d error_code=%s",
        job_id,
        attempt,
        error_code,
    )
    try:
        repo.append_history(
            job_id,
            "pod_logs_capture_failed",
            {
                "attempt": attempt,
                "error_code": error_code,
                "runtime_identity": runtime_identity,
            },
        )
    except Exception:
        LOGGER.debug("pod log capture history write failed job_id=%s", job_id, exc_info=True)


@shared_task(
    name="api.tasks.blast.artifacts.reconcile_terminal_artifacts",
    soft_time_limit=60,
    time_limit=75,
)
def reconcile_terminal_artifacts(
    *,
    limit: int = 200,
    since_seconds: int = 86_400,
) -> dict[str, int]:
    """Re-enqueue missing or stale artifact finalizers for recent terminal jobs.

    Side effects: conditionally writes an artifact-state sentinel and enqueues
    the idempotent finalizer. The scan and enqueue count are bounded.
    """

    bounded_limit = max(1, min(int(limit), 500))
    bounded_since = max(60, min(int(since_seconds), 7 * 86_400))
    summary = {"scanned": 0, "enqueued": 0, "errors": 0}
    try:
        from api.services.state_repo import JobStateRepository

        rows = JobStateRepository().list_recent_terminal(
            job_type="blast",
            limit=bounded_limit,
            since_seconds=bounded_since,
            include_payload=False,
        )
    except Exception as exc:
        LOGGER.warning("terminal artifact reconcile scan failed: %s", type(exc).__name__)
        summary["errors"] = 1
        return summary

    from api.tasks.blast.state import _enqueue_artifact_finalizer

    summary["scanned"] = len(rows)
    for row in rows:
        try:
            from api.services.job_artifacts import get_artifact_state
            from api.services.state.job_state import canonical_elastic_blast_job_id

            runtime_identity = canonical_elastic_blast_job_id(
                getattr(row, "elastic_blast_job_id", "")
            )
            sentinel = get_artifact_state(row.job_id, "artifact_finalizer")
            same_generation = bool(
                sentinel is not None
                and (
                    sentinel.runtime_identity.casefold() == runtime_identity.casefold()
                    or (
                        sentinel.status == "pending"
                        and not sentinel.runtime_identity
                        and not runtime_identity
                        and sentinel.error_code != "runtime_identity_pending"
                    )
                )
            )
            prior_attempts = (
                sentinel.reconcile_attempts
                if same_generation and sentinel is not None
                else 0
            )
            if prior_attempts >= _RECONCILE_ATTEMPT_MAX:
                LOGGER.warning(
                    "terminal artifact reconcile exhausted job_id=%s attempts=%d",
                    row.job_id,
                    prior_attempts,
                )
                continue
            if _enqueue_artifact_finalizer(
                row.job_id,
                str(row.phase or row.status or ""),
                str(row.status or ""),
                runtime_identity=runtime_identity,
                reconcile_attempts=prior_attempts + 1,
            ):
                summary["enqueued"] += 1
        except Exception as exc:
            summary["errors"] += 1
            LOGGER.warning(
                "terminal artifact reconcile row failed job_id=%s: %s",
                row.job_id,
                type(exc).__name__,
            )
    return summary


@shared_task(
    name="api.tasks.blast.artifacts.finalize_job_artifacts",
    bind=True,
    ignore_result=True,
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def finalize_job_artifacts(
    self: Any,
    *,
    job_id: str,
    pod_log_attempt: int = 1,
) -> dict[str, Any]:
    """Persist immutable UI artifacts for a terminal BLAST job.

    Side effects: writes Execution Steps and result analytics artifacts to the
    platform Storage account. Idempotent: existing artifacts are overwritten
    with deterministic payloads for the current job state/result blobs.
    """
    del self
    summary: dict[str, Any] = {
        "job_id": job_id,
        "execution_steps": "skipped",
        "results": "skipped",
        "pod_log_attempt": pod_log_attempt,
    }
    runtime_identity = ""
    sentinel: Any = None
    try:
        from api.services.job_artifacts import (
            get_artifact_state,
            upsert_artifact_state,
            write_execution_steps_snapshot,
        )
        from api.services.state.job_state import canonical_elastic_blast_job_id
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(job_id)
        if state is None:
            upsert_artifact_state(
                job_id,
                "artifact_finalizer",
                status="failed",
                error_code="missing",
            )
            return {**summary, "status": "missing"}
        runtime_identity = canonical_elastic_blast_job_id(
            getattr(state, "elastic_blast_job_id", "")
        )
        sentinel = get_artifact_state(job_id, "artifact_finalizer")
        reconcile_attempts = sentinel.reconcile_attempts if sentinel is not None else 0
        if (
            sentinel is not None
            and sentinel.error_code == "runtime_identity_pending"
            and not runtime_identity
        ):
            LOGGER.info(
                "finalize_job_artifacts: runtime identity pending job_id=%s",
                job_id,
            )
            return {**summary, "status": "identity_pending"}
        upsert_artifact_state(
            job_id,
            "artifact_finalizer",
            status="pending",
            runtime_identity=runtime_identity,
            reconcile_attempts=reconcile_attempts,
        )
        pod_logs_empty = True
        try:
            from api.services import get_credential
            from api.services.job_logs.persist import persist_completed_job_pod_logs

            persisted = persist_completed_job_pod_logs(get_credential(), state)
            if persisted:
                summary["pod_logs"] = persisted
                pod_logs_empty = False
                # Re-read so the execution-steps snapshot picks up the merged
                # last_output blobs we just wrote.
                state = repo.get(job_id) or state
        except Exception as exc:
            LOGGER.info(
                "finalize_job_artifacts: pod log persistence skipped job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )
        step_state = write_execution_steps_snapshot(state)
        if step_state is not None:
            summary["execution_steps"] = "ready"
        storage_account = str(getattr(state, "storage_account", "") or "")
        if not storage_account and isinstance(state.payload, dict):
            storage_account = str(state.payload.get("storage_account") or "")
        if str(state.status or "").casefold() == "completed" and storage_account:
            from api.services.blast.result_artifacts import build_and_write_default_result_artifacts

            summary["results"] = build_and_write_default_result_artifacts(
                job_id,
                storage_account,
            )
        upsert_artifact_state(
            job_id,
            "artifact_finalizer",
            status="ready",
            runtime_identity=runtime_identity,
            reconcile_attempts=reconcile_attempts,
        )

        # Pod logs may still be flushing at the K8s pod level right after the
        # job container exits. If the first capture returned nothing,
        # schedule a delayed self-retry so the snapshot can be re-built with
        # the trailing tail once pods finish writing. Cap the retries — past
        # that point the K8s log GC has likely evicted the pod logs anyway.
        if not pod_logs_empty:
            _record_pod_log_capture_state(
                repo,
                job_id=job_id,
                status="ready",
                error_code="",
                runtime_identity=runtime_identity,
                attempt=pod_log_attempt,
            )
        elif pod_log_attempt < _POD_LOG_RETRY_MAX:
            try:
                finalize_job_artifacts.apply_async(
                    kwargs={"job_id": job_id, "pod_log_attempt": pod_log_attempt + 1},
                    countdown=_POD_LOG_RETRY_COUNTDOWN_S,
                )
                summary["pod_log_retry_scheduled"] = True
                _record_pod_log_capture_state(
                    repo,
                    job_id=job_id,
                    status="pending",
                    error_code="capture_pending",
                    runtime_identity=runtime_identity,
                    attempt=pod_log_attempt,
                )
            except Exception as exc:
                LOGGER.info(
                    "finalize_job_artifacts: pod log retry enqueue skipped job_id=%s: %s",
                    job_id,
                    type(exc).__name__,
                )
                _record_pod_log_capture_state(
                    repo,
                    job_id=job_id,
                    status="failed",
                    error_code="retry_enqueue_failed",
                    runtime_identity=runtime_identity,
                    attempt=pod_log_attempt,
                )
                summary["pod_logs_error"] = "retry_enqueue_failed"
        else:
            _record_pod_log_capture_state(
                repo,
                job_id=job_id,
                status="failed",
                error_code="capture_exhausted",
                runtime_identity=runtime_identity,
                attempt=pod_log_attempt,
            )
            summary["pod_logs_error"] = "capture_exhausted"
        return {**summary, "status": "completed"}
    except Exception as exc:
        LOGGER.warning("finalize_job_artifacts failed job_id=%s: %s", job_id, type(exc).__name__)
        try:
            from api.services.job_artifacts import upsert_artifact_state

            upsert_artifact_state(
                job_id,
                "artifact_finalizer",
                status="failed",
                error_code=type(exc).__name__,
                runtime_identity=runtime_identity,
                reconcile_attempts=(
                    sentinel.reconcile_attempts if sentinel is not None else 0
                ),
            )
        except Exception:
            LOGGER.debug("artifact finalizer failure state write failed", exc_info=True)
        raise

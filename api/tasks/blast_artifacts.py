"""Background BLAST artifact finalization tasks.

Responsibility: Background BLAST artifact finalization tasks
Edit boundaries: Keep long-running side effects here; route handlers should enqueue tasks and
persist state.
Key entry points: `finalize_job_artifacts`
Risky contracts: Tasks should be idempotent, retry-aware, and write progress/state checkpoints.
Validation: `uv run pytest -q api/tests/test_blast_tasks.py`.
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


@shared_task(
    name="api.tasks.blast.artifacts.finalize_job_artifacts",
    bind=True,
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
    try:
        from api.services.job_artifacts import upsert_artifact_state, write_execution_steps_snapshot
        from api.services.state_repo import JobStateRepository

        upsert_artifact_state(job_id, "artifact_finalizer", status="pending")
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
            from api.services.blast_result_artifacts import build_and_write_default_result_artifacts

            summary["results"] = build_and_write_default_result_artifacts(
                job_id,
                storage_account,
            )
        upsert_artifact_state(job_id, "artifact_finalizer", status="ready")

        # Pod logs may still be flushing at the K8s pod level right after the
        # job container exits. If the first capture returned nothing,
        # schedule a delayed self-retry so the snapshot can be re-built with
        # the trailing tail once pods finish writing. Cap the retries — past
        # that point the K8s log GC has likely evicted the pod logs anyway.
        if pod_logs_empty and pod_log_attempt < _POD_LOG_RETRY_MAX:
            try:
                finalize_job_artifacts.apply_async(
                    kwargs={"job_id": job_id, "pod_log_attempt": pod_log_attempt + 1},
                    countdown=_POD_LOG_RETRY_COUNTDOWN_S,
                )
                summary["pod_log_retry_scheduled"] = True
            except Exception as exc:
                LOGGER.info(
                    "finalize_job_artifacts: pod log retry enqueue skipped job_id=%s: %s",
                    job_id,
                    type(exc).__name__,
                )
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
            )
        except Exception:
            LOGGER.debug("artifact finalizer failure state write failed", exc_info=True)
        raise

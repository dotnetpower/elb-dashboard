"""Shared helpers for the Azure infrastructure Celery tasks.

Responsibility: Provide stateless helpers (timestamp, best-effort state-repo update,
    Celery progress publishing) used by the AKS provision / lifecycle / RBAC task
    modules in this package.
Edit boundaries: Pure helpers only. No Azure SDK calls live here — they belong in the
    sibling task or service modules.
Key entry points: `now_iso`, `update_state`, `record_task_progress`, `publish_progress`.
Risky contracts: `update_state`, `record_task_progress`, and `publish_progress` must
    remain best-effort (never raise) so a state-repo or broker hiccup cannot fail the
    surrounding Celery task.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py
    api/tests/test_azure_tasks.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def update_state(task_id: str, phase: str, status: str = "running", **extra: Any) -> None:
    """Best-effort state update to the job state repo."""
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(task_id)
        if state:
            state.status = status
            state.phase = phase
            state.updated_at = now_iso()
            for k, v in extra.items():
                if hasattr(state, k):
                    setattr(state, k, v)
            repo.update(state.job_id, status=status, phase=phase)
            repo.append_history(task_id, phase, {"phase": phase, "status": status, **extra})
    except Exception as exc:
        LOGGER.warning("state update failed for %s: %s", task_id, exc)


def record_task_progress(task: Any, phase: str, **meta: Any) -> None:
    """Best-effort publish to Celery `result.info` so `/api/tasks/{id}.progress` sees it.

    Without this, `update_state` only writes to the JobStateRepository — the
    `/api/tasks/{id}` endpoint only surfaces `result.info`, so the FE banner
    would never see the live phase / sub-progress.
    """
    if task is None:
        return
    try:
        task.update_state(state="PROGRESS", meta={"phase": phase, **meta})
    except Exception as exc:  # broker outage, no backend in tests, etc.
        LOGGER.debug("task progress update failed: %s", type(exc).__name__)


def publish_progress(
    task: Any,
    job_id: str,
    phase: str,
    *,
    step: int | None = None,
    total_steps: int | None = None,
    status: str = "running",
    message: str | None = None,
    **extra: Any,
) -> None:
    """Publish a single progress tick to *both* the state repo and Celery meta.

    `phase` is the canonical machine string the FE switches on. `step` /
    `total_steps` drive the "Step N of M" indicator in the provisioning
    banner. `message` is the optional human sub-label (e.g. "AKS state:
    Creating"). `extra` is anything else useful for the banner (cluster
    state, per-pool states, ARM elapsed seconds, …) and lands in both
    the state-repo history row and the Celery meta payload.
    """
    payload: dict[str, Any] = dict(extra)
    if step is not None:
        payload["step"] = step
    if total_steps is not None:
        payload["total_steps"] = total_steps
    if message is not None:
        payload["message"] = message
    payload["updated_at"] = now_iso()
    update_state(job_id, phase, status=status, **payload)
    record_task_progress(task, phase, status=status, **payload)

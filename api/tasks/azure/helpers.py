"""Shared helpers for the Azure infrastructure Celery tasks.

Responsibility: Provide stateless helpers (timestamp, best-effort state-repo update)
    used by the AKS provision / lifecycle / RBAC task modules in this package.
Edit boundaries: Pure helpers only. No Azure SDK calls live here — they belong in the
    sibling task or service modules.
Key entry points: `now_iso`, `update_state`.
Risky contracts: `update_state` must remain best-effort (never raise) so a state-repo
    hiccup cannot fail the surrounding Celery task.
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

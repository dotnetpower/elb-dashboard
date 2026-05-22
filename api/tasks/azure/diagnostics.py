"""Diagnostic Celery task — proves enqueue ↔ consume round-trip works.

Responsibility: Provide a no-op Celery task the health route can fire to verify the
    `azure` queue is wired end-to-end (broker reachable, worker consuming, result
    backend writable).
Edit boundaries: Diagnostic surface only — do not add behaviour. Keep it a fast no-op
    so it remains a meaningful health check.
Key entry points: `diag_noop` (Celery task `api.tasks.azure.diag_noop`).
Risky contracts: Task name `api.tasks.azure.diag_noop` is referenced by
    `api/routes/health.py` — do not rename. `max_retries=0` is intentional so a broken
    backend surfaces immediately rather than masquerading as a retry.
Validation: `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from api.tasks.azure.helpers import now_iso

LOGGER = logging.getLogger(__name__)


@shared_task(name="api.tasks.azure.diag_noop", bind=True, max_retries=0)
def diag_noop(self: Any, *, message: str = "ping") -> dict[str, Any]:
    """Diagnostic-only no-op task — proves enqueue ↔ consume round-trip works."""
    LOGGER.info("DIAG_NOOP message=%r task_id=%s", message, self.request.id)
    return {"message": message, "task_id": self.request.id, "ts": now_iso()}

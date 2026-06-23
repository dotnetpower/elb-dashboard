"""Celery task wrapper for age-based result retention purge.

Responsibility: Beat-scheduled entry point that runs the age-based retention
purge (``api.services.storage.retention.purge_aged_results``). Default-OFF: it is
a cheap no-op every tick unless ``STORAGE_DFS_ENABLED`` is on AND
``BLAST_RESULT_RETENTION_DAYS > 0``, so leaving it scheduled costs one guard check.
Edit boundaries: Thin wrapper only — the cutoff/flag logic + per-job purge live in
the service. Do not add deletion logic here.
Key entry points: ``purge_aged_results_task``.
Risky contracts: Runs with ``dry_run=False`` (real deletion) — but the service
gates on the flag + window, so it deletes nothing until an operator opts in.
Validation: ``uv run pytest -q api/tests/test_retention.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

LOGGER = logging.getLogger(__name__)


@shared_task(name="api.tasks.storage.purge_aged_results")
def purge_aged_results_task() -> dict[str, Any]:
    """Run the retention purge (no-op unless flag + window are set)."""
    from api.services.storage.retention import purge_aged_results

    summary = purge_aged_results(dry_run=False)
    if summary.get("enabled") and (summary.get("purged") or summary.get("errors")):
        LOGGER.info(
            "retention purge run: purged=%s errors=%s scanned=%s days=%s",
            summary.get("purged"),
            summary.get("errors"),
            summary.get("scanned"),
            summary.get("days"),
        )
    return summary

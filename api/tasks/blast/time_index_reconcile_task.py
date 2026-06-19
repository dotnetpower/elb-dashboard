"""Celery task that periodically reconciles the jobstate time-ordered index (#50).

Responsibility: Heal the ``jobstateindex`` table by re-running the idempotent
backfill upserts on a schedule, so a job that an in-line best-effort
``_index_put`` failed to index (the row was written but the index write raced a
transient Table error) is re-added and stops being silently omitted from the
indexed ``/api/blast/jobs`` listing.
Edit boundaries: Side-effect entry point only — the scan/upsert logic lives in
``JobStateRepository.reconcile_time_index`` (shared with the one-shot backfill
script). Do not duplicate the upsert loop here.
Key entry points:
  - ``reconcile_time_index`` (``@shared_task``
     ``name="api.tasks.blast.reconcile_time_index"``, scheduled by Celery beat).
Risky contracts: Idempotent — re-running upserts the SAME immutable RowKey per
job, so a steady-state pass is a no-op write-for-write. No-op (returns early)
unless ``JOBSTATE_TIME_INDEX_ENABLED`` is set, so the task is free to leave
scheduled on every deployment (charter §12a Rule 4: new behaviour default-OFF).
Public task name must stay ``api.tasks.blast.reconcile_time_index`` (referenced
from ``api/celery_app.py`` beat schedule).
Validation: ``uv run pytest -q api/tests/test_jobstate_time_index.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

LOGGER = logging.getLogger(__name__)

__all__ = ("reconcile_time_index",)


@shared_task(name="api.tasks.blast.reconcile_time_index", bind=True)
def reconcile_time_index(self: Any) -> dict[str, Any]:
    """Re-run the idempotent jobstate time-index upserts to heal missed rows.

    Side effects: upserts one ``jobstateindex`` row per non-deleted ``jobstate``
    row (read-only against ``jobstate``). Idempotent — the RowKey is derived from
    the immutable ``owner_oid`` + ``created_at`` so a steady-state pass writes the
    same keys and adds nothing new.

    No-op when ``JOBSTATE_TIME_INDEX_ENABLED`` is off: with the flag off no index
    rows are written at all, so there is nothing to reconcile and the table must
    not be created. Returns a small summary dict for observability.
    """
    del self
    from api.services.state.time_index import time_index_enabled

    if not time_index_enabled():
        return {"skipped": "flag_off", "scanned": 0, "written": 0}

    try:
        from api.services.state.repository import get_state_repo

        repo = get_state_repo()
        scanned, written = repo.reconcile_time_index()
    except Exception as exc:
        LOGGER.warning(
            "reconcile_time_index: reconcile failed: %s", type(exc).__name__
        )
        return {"error": type(exc).__name__, "scanned": 0, "written": 0}

    LOGGER.info(
        "reconcile_time_index: scanned=%d written=%d", scanned, written
    )
    return {"scanned": scanned, "written": written}

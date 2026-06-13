"""`reconcile_stale_dbops_jobs` Celery task — terminalise stuck dbops/warmup rows.

Responsibility: Beat-scheduled reconciler that drives ``warmup`` and
    ``prepare_db_*`` / ``shard`` / ``oracle`` jobstate rows stuck in an active
    status to a terminal status when their owning work is provably gone (crashed
    worker, or a synchronous route whose audit row was born ``queued``). Closes
    the gap left by ``reconcile_stale_jobs`` (blast-only) and
    ``reconcile_orphaned_prepare_db`` (metadata-only).
Edit boundaries: Thin Celery wrapper. All detection + terminal-write logic lives
    in ``api.services.db.stale_dbops.reconcile_dbops``. Do not add business logic
    here.
Key entry points: ``reconcile_stale_dbops_jobs`` (Celery task
    ``api.tasks.storage.reconcile_stale_dbops_jobs``).
Risky contracts: Task name must stay ``api.tasks.storage.reconcile_stale_dbops_jobs``
    because the beat schedule references it by string. Honours the
    ``STALE_DBOPS_RECONCILE_ENABLED`` kill-switch via the service.
Validation: ``uv run pytest -q api/tests/test_stale_dbops_reconcile.py``.
"""

from __future__ import annotations

from typing import Any

from celery import shared_task


@shared_task(name="api.tasks.storage.reconcile_stale_dbops_jobs", bind=True)
def reconcile_stale_dbops_jobs(
    self: Any,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    """Terminalise stale dbops/warmup jobstate rows.

    Side effects: reads active jobstate rows + the Celery result backend and
    rewrites the Table row status for rows whose work is gone. Never
    re-dispatches any work.
    """
    del self

    from api.services.db.stale_dbops import reconcile_dbops

    return reconcile_dbops(limit=limit)

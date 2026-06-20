"""`reconcile_orphaned_prepare_db` Celery task — recover stuck prepare-db markers.

Responsibility: Beat-scheduled reconciler that drives ``{db}-metadata.json`` rows whose
    ``update_in_progress`` flag is stuck on a non-terminal ``copy_status.phase`` (because
    the worker that was polling an AKS-fanout download died before writing the terminal
    state) to a terminal ``partial`` phase. This clears the perpetual SPA spinner and the
    409 in-progress gate without human intervention.
Edit boundaries: Thin Celery wrapper. The detection + reset logic lives in
    `api.services.storage.orphan_prepare_db.reconcile_orphaned_prepare_db`. Do not add
    business logic here.
Key entry points: `reconcile_orphaned_prepare_db` (Celery task
    `api.tasks.storage.reconcile_orphaned_prepare_db`).
Risky contracts: Task name must stay `api.tasks.storage.reconcile_orphaned_prepare_db`
    because the beat schedule references it by string. Honours the
    `PREPARE_DB_ORPHAN_RECONCILE_ENABLED` kill-switch via the service. Stacked
    under `@shared_task` with `skip_tick_on_transient_infra`, so a transient
    Storage DNS/connection blip skips the tick (next beat retries) instead of
    crashing with an exception Celery cannot pickle.
Validation: `uv run pytest -q api/tests/test_orphan_prepare_db_reconcile.py`.
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

import api.tasks.storage as _facade
from api.tasks.transient import skip_tick_on_transient_infra


@shared_task(name="api.tasks.storage.reconcile_orphaned_prepare_db", bind=True)
@skip_tick_on_transient_infra
def reconcile_orphaned_prepare_db(
    self: Any,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    """Recover orphaned AKS-fanout prepare-db markers.

    Side effects: reads Storage metadata + AKS Job status and rewrites
    ``{db}-metadata.json`` for rows whose driving Job is gone/failed. Never
    re-dispatches a download.
    """

    from api.services.storage.orphan_prepare_db import (
        reconcile_orphaned_prepare_db as _reconcile,
    )

    return _reconcile(credential=_facade.get_credential(), limit=limit)

"""`reconcile_orphaned_prepare_db` Celery task — reconcile durable prepare-db state.

Responsibility: Beat-scheduled wrapper that refreshes live Direct progress, recovers fully
    staged generations after revision loss, and terminalizes genuinely partial metadata.
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

    Side effects: reads Storage metadata + AKS Job status, refreshes live
    Direct counters, promotes a fully validated generation, or rewrites partial
    rows whose Job is gone/failed.
    """

    from api.services.storage.orphan_prepare_db import (
        reconcile_orphaned_prepare_db as _reconcile,
    )

    result = _reconcile(credential=_facade.get_credential(), limit=limit)
    for recovered in result.get("recovered_direct", []):
        job_id = str(recovered.get("job_id") or "")
        if job_id:
            _facade._update_state(
                job_id,
                "completed",
                "completed",
                outcome="promoted_after_restart",
                generation_id=recovered.get("generation_id"),
            )
    return result

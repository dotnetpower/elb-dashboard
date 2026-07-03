"""`reconcile_db_consistency` Celery task — self-heal DB volume/shard drift.

Beat-scheduled reconciler that keeps every prepared BLAST DB internally
consistent: it prunes ghost volume blobs left behind when NCBI shrinks a DB and
regenerates the shard alias layout for the true volume set, so a DB can never
drift into the 3-way generation mismatch (metadata / volume files / shard
layout) that fails every BLAST job with "vol does not match lmdb vol".

Responsibility: Thin Celery wrapper. The detection + heal logic lives in
    ``api.services.db.consistency.reconcile_all_db_consistency``. Do not add
    business logic here.
Edit boundaries: Task name must stay ``api.tasks.storage.reconcile_db_consistency``
    because the beat schedule references it by string. Gated default-OFF via
    ``DB_CONSISTENCY_RECONCILE_ENABLED`` (charter §12a Rule 4) because it DELETES
    Storage blobs — enabling automatic self-heal is an explicit operator opt-in.
    Stacked under ``@shared_task`` + ``skip_tick_on_transient_infra`` so a
    transient Storage blip skips the tick instead of crashing beat.
Key entry points: ``reconcile_db_consistency`` (Celery task
    ``api.tasks.storage.reconcile_db_consistency``).
Risky contracts: The underlying reconcile can never prune when the njs authority
    is missing or when ghosts exceed 50% of volumes (defensive abort), and it
    holds the per-DB prepare-db lock non-blocking so it never races a live
    download.
Validation: ``uv run pytest -q api/tests/test_db_consistency.py``.
"""

from __future__ import annotations

import os
from typing import Any

from celery import shared_task

import api.tasks.storage as _facade
from api.tasks.transient import skip_tick_on_transient_infra

_TRUTHY = {"1", "true", "yes", "on"}


@shared_task(name="api.tasks.storage.reconcile_db_consistency", bind=True)
@skip_tick_on_transient_infra
def reconcile_db_consistency(self: Any, *, limit: int = 200) -> dict[str, Any]:
    """Self-heal DB volume/shard consistency for every prepared DB.

    Side effects (when enabled): deletes ghost volume blobs + regenerates the
    shard alias layout for drifted DBs. No-op (returns ``{"status": "disabled"}``)
    unless ``DB_CONSISTENCY_RECONCILE_ENABLED`` is truthy, so scheduling it while
    the feature is dormant is harmless (charter §12a Rule 4, default-OFF).
    """
    if os.environ.get("DB_CONSISTENCY_RECONCILE_ENABLED", "").strip().lower() not in _TRUTHY:
        return {"status": "disabled"}

    from api.services.db.consistency import reconcile_all_db_consistency

    return reconcile_all_db_consistency(_facade.get_credential(), limit=limit)

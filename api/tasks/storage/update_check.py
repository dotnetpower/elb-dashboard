"""`check_database_updates` Celery task — report stale BLAST DB generations.

Responsibility: Compare each prepared workload database's `source_version` against the
    NCBI latest-dir tag and report which ones are out of date.
Edit boundaries: Read-only. Do not enqueue downloads or warmups from here — the route
    layer owns those decisions.
Key entry points: `check_database_updates` (Celery task
    `api.tasks.storage.check_database_updates`).
Risky contracts: Task name must remain `api.tasks.storage.check_database_updates` for
    beat/route compatibility. Should never raise — failures degrade to an empty payload.
Validation: `uv run pytest -q api/tests/test_warmup_jobs.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

import api.tasks.storage as _facade

LOGGER = logging.getLogger(__name__)


@shared_task(name="api.tasks.storage.check_database_updates", bind=True)
def check_database_updates(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    storage_account: str,
) -> dict[str, Any]:
    """Check downloaded BLAST DB generations against NCBI latest-dir.

    Side effects: none. Scheduled by beat for periodic visibility; it reports
    stale DBs but deliberately does not auto-download large snapshots.
    """
    try:
        from api.routes.storage.common import _resolve_latest_dir
        from api.services.storage.data import list_databases

        cred = _facade.get_credential()
        databases = list_databases(cred, storage_account)
        latest_version = _resolve_latest_dir()
        updates_available = [
            {
                "db": str(database.get("name") or ""),
                "source_version": database.get("source_version"),
                "latest_version": latest_version,
                "update_in_progress": bool(database.get("update_in_progress")),
            }
            for database in databases
            if database.get("name")
            and database.get("source_version")
            and database.get("source_version") != latest_version
        ]
        return {
            "databases": databases,
            "latest_version": latest_version,
            "updates_available": updates_available,
            "status": "completed",
        }
    except Exception as exc:
        LOGGER.warning("check_database_updates failed: %s", exc)
        return {
            "databases": [],
            "updates_available": [],
            "status": "failed",
            "error": str(exc)[:500],
        }

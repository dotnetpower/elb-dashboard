"""Daily bounded retention task for Auto oracle preference targets.

Responsibility: Inventory known Auto oracle Storage/database targets and invoke
    conservative per-DB retention when the destructive kill switch is enabled.
Edit boundaries: Thin Celery/inventory wrapper only; mark-and-sweep logic lives
    in `api.services.db.oracle_retention`.
Key entry points: `purge_oracle_history_task`.
Risky contracts: `AUTO_ORACLE_RETENTION_ENABLED` defaults OFF; at most 50 unique
    targets are inspected per run and each service sweep has strict run/blob caps.
Validation: `uv run pytest -q api/tests/test_oracle_retention.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from celery import shared_task

LOGGER = logging.getLogger(__name__)


@shared_task(name="api.tasks.storage.purge_oracle_history")
def purge_oracle_history_task() -> dict[str, Any]:
    enabled = os.environ.get("AUTO_ORACLE_RETENTION_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    if not enabled:
        return {"status": "disabled", "targets": []}

    from api.services import get_credential
    from api.services.auto_oracle import (
        get_auto_oracle_scan_cursor,
        list_auto_oracle_preference_page,
        save_auto_oracle_scan_cursor,
    )
    from api.services.db.oracle_retention import purge_oracle_history
    from api.services.db.oracle_state import oracle_container

    credential = get_credential()
    cursor = get_auto_oracle_scan_cursor("retention")
    cursor_reset = False
    try:
        preferences, next_cursor = list_auto_oracle_preference_page(
            limit=50,
            continuation_token=cursor,
        )
    except Exception:
        if not cursor:
            raise
        LOGGER.warning("oracle retention preference cursor invalid; restarting scan")
        cursor_reset = True
        preferences, next_cursor = list_auto_oracle_preference_page(
            limit=50,
            continuation_token="",
        )
    unique_targets = sorted({(pref.storage_account, pref.db_name) for pref in preferences})[:50]
    results = []
    for storage_account, db_name in unique_targets:
        try:
            results.append(
                purge_oracle_history(
                    oracle_container(credential, storage_account),
                    db_name=db_name,
                    days=14,
                    dry_run=False,
                )
            )
        except Exception as exc:
            LOGGER.warning(
                "oracle retention target failed db=%s reason=%s",
                db_name,
                type(exc).__name__,
            )
            results.append(
                {
                    "db_name": db_name,
                    "status": "failed",
                    "error": type(exc).__name__,
                }
            )
    try:
        save_auto_oracle_scan_cursor("retention", next_cursor)
    except Exception as exc:
        LOGGER.warning(
            "oracle retention preference cursor write failed reason=%s",
            type(exc).__name__,
        )
        return {
            "status": "partial",
            "targets": results,
            "cursor_reset": cursor_reset,
            "error": "retention_cursor_write_failed",
        }
    return {
        "status": (
            "partial"
            if any(result.get("status") in {"failed", "partial"} for result in results)
            else "completed"
        ),
        "targets": results,
        "cursor_reset": cursor_reset,
    }

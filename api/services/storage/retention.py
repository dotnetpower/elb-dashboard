"""Age-based retention purge for completed BLAST job results.

Responsibility: Delete the result/query storage of completed jobs older than a
configurable window, then tombstone the row — reclaiming Storage for jobs no one
is looking at anymore. Reuses the #69 best-effort recursive purge.
Edit boundaries: Orchestration only — the cutoff/flag gate, iterate completed
rows, per-job purge + tombstone. The recursive delete + its guards live in
``dfs_io`` / ``job_purge``. Never raises per job.
Key entry points: ``purge_aged_results``, ``retention_days``.
Risky contracts: Gated on ``dfs_enabled()`` AND ``retention_days > 0`` — default
``BLAST_RESULT_RETENTION_DAYS=0`` means DISABLED (no deletion). ``dry_run=True`` is
the default: it reports the plan without touching anything. Deletion of user data
is irreversible at the API level; do not enable in a shared/customer environment
without sign-off AND blob soft-delete (see the storage module).
Validation: ``uv run pytest -q api/tests/test_retention.py``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

LOGGER = logging.getLogger(__name__)

_RETENTION_DAYS_ENV = "BLAST_RESULT_RETENTION_DAYS"


def retention_days() -> int:
    """Configured retention window in days; 0 (default) = disabled."""
    raw = os.environ.get(_RETENTION_DAYS_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


def _row_age_cutoff(state: Any) -> datetime | None:
    """Parse the row's last-activity timestamp (updated_at, then created_at)."""
    for attr in ("updated_at", "created_at"):
        raw = getattr(state, attr, None)
        if raw:
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def purge_aged_results(
    *, days: int | None = None, dry_run: bool = True, limit: int = 200
) -> dict[str, Any]:
    """Purge + tombstone completed jobs older than the retention window.

    Returns ``{enabled, dry_run, days, scanned, planned, purged, skipped, errors,
    plan}``. A no-op (``enabled=False``) unless ``dfs_enabled()`` AND the window is
    > 0. ``dry_run`` (default) fills ``plan`` without touching storage/rows.
    Bounded by ``limit`` so a daily beat drains gradually.
    """
    from api.services.storage.dfs_client_pool import dfs_enabled

    window = days if days is not None else retention_days()
    summary: dict[str, Any] = {
        "enabled": False,
        "dry_run": dry_run,
        "days": window,
        "scanned": 0,
        "planned": 0,
        "purged": 0,
        "skipped": 0,
        "errors": 0,
        "plan": [],
    }
    if not dfs_enabled() or window <= 0:
        return summary
    summary["enabled"] = True

    from api.services.state_repo import get_state_repo
    from api.services.storage.job_purge import purge_job_result_storage

    repo = get_state_repo()
    try:
        rows = repo.list_completed(limit=limit)
    except Exception as exc:
        LOGGER.warning("retention list_completed failed: %s", type(exc).__name__)
        return summary

    cutoff = datetime.now(UTC) - timedelta(days=window)
    for state in rows:
        summary["scanned"] += 1
        job_id = str(getattr(state, "job_id", "") or "")
        if not job_id or str(getattr(state, "status", "") or "").lower() == "deleted":
            summary["skipped"] += 1
            continue
        activity = _row_age_cutoff(state)
        if activity is None or activity > cutoff:
            summary["skipped"] += 1
            continue
        entry = {"job_id": job_id, "age_cutoff": cutoff.isoformat()}
        if dry_run:
            summary["plan"].append(entry)
            summary["planned"] += 1
            continue
        try:
            purge_job_result_storage(state)  # best-effort recursive delete (#69)
            repo.update(job_id, status="deleted", phase="deleted")
            summary["purged"] += 1
            summary["plan"].append(entry)
        except Exception as exc:
            LOGGER.warning(
                "retention purge failed job_id=%s: %s", job_id, type(exc).__name__
            )
            summary["errors"] += 1
    return summary

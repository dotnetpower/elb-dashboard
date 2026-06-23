"""Backfill legacy flat-layout BLAST jobs into the date-tiered results layout.

Responsibility: Move a completed job's legacy ``results/{job_id}/`` tree into the
date-tiered ``results/YYYY/MM/DD/{job_id}/`` layout via an atomic dfs rename, then
persist the new prefix on the job row — so old (flat) jobs can gain the
operational benefits of the dated layout (#67) and the retention story (#76).
Edit boundaries: Orchestration only — flag-gate, idempotency skip, date derivation
from ``created_at``, the dfs rename call, and the row update. The low-level rename
+ its guard live in ``dfs_io``. Never raises per job; a failure is recorded and
the next run retries.
Key entry points: ``backfill_results_layout``.
Risky contracts: Gated on ``dfs_enabled()`` AND ``date_layout_enabled()`` (the
dated layout must be the target). ``dry_run=True`` is the default — it reports the
plan without touching storage. Each move is idempotent: a job already on the dated
layout is skipped; a rename whose source is already gone still updates the row, so
a partial prior run self-heals. ``expected_src_leaf=job_id`` guards every rename.
Validation: ``uv run pytest -q api/tests/test_results_backfill.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)


def _parse_created_at(value: str | None) -> datetime:
    """Best-effort ISO parse of ``created_at``; falls back to now (UTC)."""
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError:
            pass
    return datetime.now(UTC)


def backfill_results_layout(*, dry_run: bool = True, limit: int = 50) -> dict[str, Any]:
    """Move flat-layout completed jobs into the dated results layout.

    Returns a summary ``{enabled, dry_run, scanned, planned, moved, skipped,
    errors, plan}``. A no-op (``enabled=False``) unless BOTH ``STORAGE_DFS_ENABLED``
    and ``STORAGE_DATE_LAYOUT_ENABLED`` are on — moving blobs to a dated layout
    only makes sense when the dated layout is the live target. ``dry_run`` (default)
    fills ``plan`` without touching storage. Bounded by ``limit`` so a beat/manual
    drain proceeds gradually.
    """
    from api.services.storage.dfs_client_pool import dfs_enabled
    from api.services.storage.job_prefix import date_layout_enabled

    summary: dict[str, Any] = {
        "enabled": False,
        "dry_run": dry_run,
        "scanned": 0,
        "planned": 0,
        "moved": 0,
        "skipped": 0,
        "errors": 0,
        "plan": [],
    }
    if not (dfs_enabled() and date_layout_enabled()):
        return summary
    summary["enabled"] = True

    from api.services import get_credential
    from api.services.state_repo import get_state_repo
    from api.services.storage.dfs_io import rename_directory_dfs
    from api.services.storage.job_prefix import (
        build_dated_results_prefix,
        default_results_prefix,
        results_prefix_from_state,
    )

    repo = get_state_repo()
    try:
        rows = repo.list_completed(limit=limit)
    except Exception as exc:
        LOGGER.warning("backfill list_completed failed: %s", type(exc).__name__)
        return summary

    cred = get_credential()
    for state in rows:
        summary["scanned"] += 1
        job_id = str(getattr(state, "job_id", "") or "")
        account = str(getattr(state, "storage_account", "") or "")
        if not job_id or not account:
            summary["skipped"] += 1
            continue
        current = results_prefix_from_state(state)
        # Already on a non-flat (dated) layout → nothing to do (idempotent).
        if current != default_results_prefix(job_id):
            summary["skipped"] += 1
            continue
        dated = build_dated_results_prefix(job_id, now=_parse_created_at(state.created_at))
        src = default_results_prefix(job_id).rstrip("/")  # {job_id}
        dst = dated.rstrip("/")  # YYYY/MM/DD/{job_id}
        entry = {"job_id": job_id, "from": f"results/{src}", "to": f"results/{dst}"}
        if dry_run:
            summary["plan"].append(entry)
            summary["planned"] += 1
            continue
        try:
            # results tree (must exist for a completed job; absent → self-heal
            # by still stamping the row so reads resolve the dated path).
            rename_directory_dfs(
                cred, account, "results", src, dst, expected_src_leaf=job_id
            )
            # Queries/uploads stay flat here — query dating is owned by #74.
            repo.update(job_id, results_prefix=dated)
            summary["moved"] += 1
            summary["plan"].append(entry)
        except Exception as exc:
            LOGGER.warning(
                "backfill move failed job_id=%s: %s", job_id, type(exc).__name__
            )
            summary["errors"] += 1
    return summary

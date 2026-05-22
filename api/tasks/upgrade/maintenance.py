"""Scheduled maintenance tasks for the in-app self-upgrade flow.

Module summary: Daily ACR orphan-tag purge + weekly history compaction.
Both run as Celery beat tasks (see `api.celery_app.beat_schedule`).

Responsibility: Background housekeeping that closes the loop on
  partial-failure side effects (orphan ACR tags from `_fail_pre`)
  and bounds long-term storage growth (history append blob).
Edit boundaries: Pure idempotent housekeeping — never advance the
  upgrade state machine, never raise on failure. Errors are logged
  and the task returns a status dict.
Key entry points: `purge_orphan_acr_tags_inline`, `purge_orphan_acr_tags`,
  `compact_history_inline`, `compact_history`.
Risky contracts: `purge_orphan_acr_tags` walks the same audit blob
  the SPA reads — keep the tail limit (`limit=200`) generous enough
  to find old orphan rows but bounded so the task stays cheap.
Validation: `uv run pytest -q api/tests/test_upgrade_task.py`.
"""

from __future__ import annotations

import logging

from celery import shared_task

from api.services.upgrade import acr_inventory, history

LOGGER = logging.getLogger(__name__)


def purge_orphan_acr_tags_inline(
    *,
    acr: object | None = None,
    history_mod: object | None = None,
) -> dict:
    """Retry `delete_tag` for orphan_acr_tags audit rows still marked orphaned.

    Idempotent: re-running yields the same result for already-deleted tags
    (the helper returns `(True, "already absent")`). The retry result is
    appended as `orphan_purge_attempt` so an operator can see the
    cleanup trajectory in the SPA history view.
    """
    acr_mod = acr or acr_inventory
    hist_mod = history_mod or history
    events = hist_mod.tail_events(limit=200)
    refs_to_retry: dict[str, str] = {}  # ref -> job_id (last seen)
    for evt in events:
        if evt.event != "orphan_acr_tags":
            continue
        cleanup = evt.detail.get("cleanup_results", {}) or {}
        for ref, status in cleanup.items():
            if isinstance(status, str) and "orphaned" in status:
                refs_to_retry[ref] = evt.job_id
    if not refs_to_retry:
        return {"checked": 0, "retried": 0, "deleted": 0}
    retried = 0
    deleted = 0
    results: dict[str, str] = {}
    for ref, _job in refs_to_retry.items():
        retried += 1
        ok, reason = acr_mod.delete_tag_best_effort(ref)
        results[ref] = "deleted" if ok else f"orphaned ({reason})"
        if ok:
            deleted += 1
    hist_mod.record_event(
        "orphan_purge_attempt",
        job_id="",
        attempted=retried,
        deleted=deleted,
        results=results,
    )
    LOGGER.info(
        "upgrade.purge_orphan_acr_tags: attempted=%d deleted=%d", retried, deleted
    )
    return {"checked": len(refs_to_retry), "retried": retried, "deleted": deleted}


@shared_task(name="api.tasks.upgrade.purge_orphan_acr_tags")
def purge_orphan_acr_tags() -> dict:
    return purge_orphan_acr_tags_inline()


def compact_history_inline(*, history_mod: object | None = None) -> dict:
    """Rewrite the upgrade-history blob, keeping only events newer than the
    read-time age cap. Caps unbounded blob growth over multi-year
    deployments.

    A failed compact is a no-op (the blob remains as-is) so a transient
    Storage outage does not lose audit data.
    """
    hist = history_mod or history
    try:
        return hist.compact_blob()
    except Exception as exc:
        LOGGER.warning("upgrade.compact_history failed: %s", exc)
        return {"compacted": False, "reason": str(exc)}


@shared_task(name="api.tasks.upgrade.compact_history")
def compact_history() -> dict:
    return compact_history_inline()

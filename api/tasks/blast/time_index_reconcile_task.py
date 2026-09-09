"""Celery task that periodically reconciles the jobstate time-ordered index (#50).

Responsibility: Heal the ``jobstateindex`` table by re-running the idempotent
backfill repair on a schedule, so a job that an in-line best-effort
``_index_put`` failed to index (the row was written but the index write raced a
transient Table error) is re-added and stops being silently omitted from the
indexed ``/api/blast/jobs`` listing.
Edit boundaries: Side-effect entry point only — the scan/upsert logic lives in
``JobStateRepository.reconcile_time_index`` (shared with the one-shot backfill
script). Do not duplicate the upsert loop here.
Key entry points:
  - ``reconcile_time_index`` (``@shared_task``
     ``name="api.tasks.blast.reconcile_time_index"``, scheduled by Celery beat).
Risky contracts: Idempotent — re-running derives the SAME immutable RowKeys per
job, and existing rows are never rewritten. A token-owned Redis lock prevents
old/new revisions from running the full scan together; its TTL exceeds the hard
task limit. No-op (returns early) unless ``JOBSTATE_TIME_INDEX_ENABLED`` is set,
so the task is free to leave scheduled on every deployment (charter §12a Rule 4:
new behaviour default-OFF). Celery soft limits and repair failures must escape
so monitoring records FAILURE instead of a false SUCCESS.
Public task name must stay ``api.tasks.blast.reconcile_time_index`` (referenced
from ``api/celery_app.py`` beat schedule).
Validation: ``uv run pytest -q api/tests/test_jobstate_time_index.py``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from celery import shared_task

LOGGER = logging.getLogger(__name__)
_RECONCILE_LOCK_KEY = "blast:jobstate-time-index:reconcile-lock"
_RECONCILE_SOFT_TIME_LIMIT_SECONDS = 3000
_RECONCILE_HARD_TIME_LIMIT_SECONDS = 3200
_RECONCILE_LOCK_TTL_SECONDS = 3300
_RECONCILE_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

__all__ = ("reconcile_time_index",)


def _acquire_reconcile_lock() -> tuple[tuple[Any, str] | None, str]:
    from api.services.redis_clients import get_ops_redis_client

    try:
        client = get_ops_redis_client(
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
        token = uuid.uuid4().hex
        if client.set(
            _RECONCILE_LOCK_KEY,
            token,
            nx=True,
            ex=_RECONCILE_LOCK_TTL_SECONDS,
        ):
            return (client, token), ""
        return None, "reconcile_already_running"
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        LOGGER.warning("time-index reconcile lock unavailable: %s", type(exc).__name__)
        return None, "lock_unavailable"


def _release_reconcile_lock(handle: tuple[Any, str]) -> None:
    client, token = handle
    try:
        released = int(client.eval(_RECONCILE_RELEASE_LUA, 1, _RECONCILE_LOCK_KEY, token) or 0)
        if released == 0:
            LOGGER.warning("time-index reconcile lock ownership changed or expired before release")
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        LOGGER.warning("time-index reconcile lock release failed: %s", type(exc).__name__)


@shared_task(
    name="api.tasks.blast.reconcile_time_index",
    bind=True,
    soft_time_limit=_RECONCILE_SOFT_TIME_LIMIT_SECONDS,
    time_limit=_RECONCILE_HARD_TIME_LIMIT_SECONDS,
    acks_late=False,
    reject_on_worker_lost=False,
)
def reconcile_time_index(self: Any) -> dict[str, Any]:
    """Run one bounded, single-flight pass that heals missing index rows.

    Side effects: creates any missing owner/global ``jobstateindex`` rows for
    each non-deleted ``jobstate`` row (read-only against ``jobstate``).
    Idempotent — each RowKey is derived from immutable ``owner_oid`` +
    ``created_at`` values, so a steady-state pass performs no writes.

    This periodic maintenance task intentionally acknowledges on start: losing
    one pass to worker termination is safe because existing writes are
    idempotent and the next hourly tick resumes the same missing-row repair.
    Submit and execution tasks retain the app-level late-ack contract.

    No-op when ``JOBSTATE_TIME_INDEX_ENABLED`` is off: with the flag off no index
    rows are written at all, so there is nothing to reconcile and the table must
    not be created. Returns a small summary dict for observability.
    """
    del self
    from api.services.state.time_index import time_index_enabled

    if not time_index_enabled():
        return {"skipped": "flag_off", "scanned": 0, "written": 0}

    lock_handle, lock_reason = _acquire_reconcile_lock()
    if lock_handle is None:
        return {"skipped": lock_reason, "scanned": 0, "written": 0}

    try:
        from api.services.state.repository import get_state_repo

        repo = get_state_repo()
        scanned, written = repo.reconcile_time_index()
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        LOGGER.exception("reconcile_time_index failed: %s", type(exc).__name__)
        raise
    finally:
        _release_reconcile_lock(lock_handle)

    LOGGER.info("reconcile_time_index: scanned=%d written=%d", scanned, written)
    return {"scanned": scanned, "written": written}

"""In-app job notifications — a derived view over jobstate plus per-user markers.

Responsibility: Build the notification feed for a caller from terminal BLAST jobs
already stored in ``jobstate`` and track per-user seen/cleared markers without a
dedicated notification table or any terminal-transition write hook.
Edit boundaries: Azure-Tables access for the marker row lives here; job listing is
delegated to ``JobStateRepository`` (never queried directly with the SDK). No HTTP
or response shaping — that belongs to ``api/routes/notifications.py``.
Key entry points: ``build_notifications``, ``mark_all_seen``,
``clear_all_notifications``, ``get_notification_marker``.
Risky contracts: ``updated_at`` is the "became terminal at" anchor and relies on the
fact that terminal jobstate rows are not re-written (``_update_state`` no-op shortcut +
reconcile skips terminal rows + finalizers do not bump the row). Unread comparison is a
lexicographic string compare, which is correct only because every writer uses the same
fixed-offset ``isoformat(timespec="seconds")`` UTC format. ``build_notifications`` seeds
the marker to "now" on first read so a brand-new user starts at zero unread instead of a
flood of historical completions. Seen and clear actions update independent fields with
MERGE so concurrent requests cannot erase or reorder each other's state. Failure detail
is sanitised before it crosses the HTTP boundary.
Validation: ``uv run pytest -q api/tests/test_notifications.py``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential
from api.services.feature_events import TERMINAL_STATUSES
from api.services.state.table_pool import (
    _ENSURED_TABLES,
    _ENSURED_TABLES_LOCK,
    _TABLE_ENDPOINT_ENV,
    _PooledTableClient,
)

LOGGER = logging.getLogger(__name__)

_TABLE_NAME = "notifseen"
_MARKER_PARTITION_PREFIX = "notif:"
_MARKER_ROW_KEY = "current"

# Hard cap on how many recent jobs we scan to assemble the feed. A caller only
# ever sees the most-recent terminal jobs, so a bounded scan keeps the Table read
# cheap while still surfacing enough rows after the terminal/parent filter.
_SCAN_LIMIT = 200
_MAX_FEED_LIMIT = 100
_MAX_ERROR_DETAIL_LENGTH = 500
_ERROR_DETAIL_SELECT = ["PartitionKey", "RowKey", "payload_json"]

_TABLE_POOL: _PooledTableClient | None = None
_TABLE_POOL_LOCK = Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _marker_key(owner_oid: str) -> str:
    raw = owner_oid or "anonymous"
    return _MARKER_PARTITION_PREFIX + re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)


def _table_client() -> TableClient:
    global _TABLE_POOL
    pool = _TABLE_POOL
    if pool is not None:
        return pool  # type: ignore[return-value]
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _TABLE_POOL_LOCK:
        if _TABLE_POOL is None:
            _TABLE_POOL = _PooledTableClient(
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _TABLE_POOL  # type: ignore[return-value]


def _reset_table_pool() -> None:
    """Test hook + credential-reset safety valve."""
    global _TABLE_POOL
    with _TABLE_POOL_LOCK:
        pool = _TABLE_POOL
        _TABLE_POOL = None
    if pool is not None:
        close = getattr(pool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: S110 - close races are not fatal
                pass


def _ensure_table() -> None:
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    cache_key = (endpoint, _TABLE_NAME)
    if cache_key in _ENSURED_TABLES:
        return
    with _ENSURED_TABLES_LOCK:
        if cache_key in _ENSURED_TABLES:
            return
        with TableServiceClient(endpoint=endpoint, credential=get_credential()) as service:
            try:
                service.create_table_if_not_exists(_TABLE_NAME)
            except AttributeError:
                try:
                    service.create_table(_TABLE_NAME)
                except ResourceExistsError:
                    pass
        _ENSURED_TABLES.add(cache_key)


@dataclass(frozen=True)
class NotificationMarker:
    last_seen_at: str = ""
    cleared_before_at: str = ""


class NotificationMarkerWriteError(RuntimeError):
    """Raised when an explicit notification marker action cannot be persisted."""


def get_notification_marker(owner_oid: str) -> NotificationMarker:
    """Return the caller's seen/cleared marker, or empty values when unavailable.

    Best-effort: any storage fault degrades to "" (everything reads as already
    seen) so the feed never fails because the marker is unavailable.
    """
    try:
        _ensure_table()
        with _table_client() as table:
            try:
                entity = table.get_entity(
                    partition_key=_marker_key(owner_oid), row_key=_MARKER_ROW_KEY
                )
            except ResourceNotFoundError:
                return NotificationMarker()
            values = dict(entity)
            return NotificationMarker(
                last_seen_at=str(values.get("last_seen_at") or ""),
                cleared_before_at=str(values.get("cleared_before_at") or ""),
            )
    except Exception as exc:
        LOGGER.warning("notif marker read failed: %s", type(exc).__name__)
        return NotificationMarker()


def get_last_seen(owner_oid: str) -> str:
    """Backward-compatible accessor for the caller's ``last_seen_at`` value."""
    return get_notification_marker(owner_oid).last_seen_at


def _set_notification_marker(
    owner_oid: str,
    *,
    last_seen_at: str | None = None,
    cleared_before_at: str | None = None,
) -> bool:
    """Merge selected marker fields and report whether persistence succeeded.

    ``MERGE`` is intentional: a mark-seen request racing a clear request must not
    erase ``cleared_before_at``. Marker timestamps only move forward in normal
    use, so last-writer-wins remains safe for writes to the same field.
    """
    try:
        _ensure_table()
        entity = {
            "PartitionKey": _marker_key(owner_oid),
            "RowKey": _MARKER_ROW_KEY,
            "owner_oid": owner_oid or "",
            "updated_at": _now_iso(),
        }
        if last_seen_at is not None:
            entity["last_seen_at"] = last_seen_at
        if cleared_before_at is not None:
            entity["cleared_before_at"] = cleared_before_at
        with _table_client() as table:
            table.upsert_entity(entity, mode=UpdateMode.MERGE)
        return True
    except Exception as exc:
        LOGGER.warning("notif marker write failed: %s", type(exc).__name__)
        return False


def set_last_seen(owner_oid: str, last_seen_at: str) -> bool:
    """Merge the caller's ``last_seen_at`` marker and return success."""
    return _set_notification_marker(owner_oid, last_seen_at=last_seen_at)


@dataclass(frozen=True)
class NotificationItem:
    job_id: str
    status: str
    title: str
    program: str
    db: str
    updated_at: str
    error_code: str
    error_detail: str
    unread: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "title": self.title,
            "program": self.program,
            "db": self.db,
            "updated_at": self.updated_at,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "unread": self.unread,
        }


def _is_split_child(job: Any) -> bool:
    return bool(getattr(job, "parent_job_id", None))


def _terminal_jobs(jobs: list[Any]) -> list[Any]:
    """Keep only terminal, non-child jobs, most-recent first."""
    selected = [
        job
        for job in jobs
        if str(getattr(job, "status", "") or "") in TERMINAL_STATUSES
        and not _is_split_child(job)
    ]
    selected.sort(key=lambda job: str(getattr(job, "updated_at", "") or ""), reverse=True)
    return selected


def _load_error_details(repo: Any, jobs: list[Any]) -> dict[str, str]:
    """Batch-load and sanitise human failure details for visible failed jobs."""
    failed_ids = [
        str(getattr(job, "job_id", "") or "")
        for job in jobs
        if str(getattr(job, "status", "") or "") == "failed"
    ]
    if not failed_ids:
        return {}
    try:
        detail_rows = repo.get_many(failed_ids, select=_ERROR_DETAIL_SELECT)
    except Exception as exc:
        LOGGER.warning("notif error-detail lookup failed: %s", type(exc).__name__)
        return {}

    from api.services.sanitise import sanitise

    details: dict[str, str] = {}
    for job_id, row in detail_rows.items():
        payload = getattr(row, "payload", None)
        if not isinstance(payload, dict):
            continue
        raw = str(payload.get("error") or "").strip()
        if raw:
            details[job_id] = sanitise(raw)[:_MAX_ERROR_DETAIL_LENGTH]
    return details


def build_notifications(
    owner_oid: str,
    *,
    limit: int = 50,
    seed_if_missing: bool = True,
) -> dict[str, Any]:
    """Return the caller's terminal-job notification feed plus the unread count.

    Derived entirely from ``jobstate``: the most-recent terminal, non-child jobs
    owned by (or cluster-shared with) the caller. ``unread`` is true for a job
    whose ``updated_at`` is newer than the stored ``last_seen_at`` marker.

    When the caller has no marker yet and ``seed_if_missing`` is true, the marker
    is seeded to "now" so a first-time user starts at zero unread; only jobs that
    finish after this first read then count as unread.

    Best-effort: a storage fault on the job listing degrades to an empty feed.
    """
    feed_limit = max(1, min(limit, _MAX_FEED_LIMIT))
    marker = get_notification_marker(owner_oid)
    last_seen = marker.last_seen_at
    if not last_seen and seed_if_missing:
        last_seen = _now_iso()
        set_last_seen(owner_oid, last_seen)

    try:
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        jobs = repo.list_for_owner(owner_oid, limit=_SCAN_LIMIT, include_payload=False)
    except Exception as exc:
        LOGGER.warning("notif feed listing failed: %s", type(exc).__name__)
        return {"items": [], "unread_count": 0, "last_seen_at": last_seen}

    terminal = _terminal_jobs(jobs)
    if marker.cleared_before_at:
        terminal = [
            job
            for job in terminal
            if str(getattr(job, "updated_at", "") or "") > marker.cleared_before_at
        ]
    visible_jobs = terminal[:feed_limit]
    error_details = _load_error_details(repo, visible_jobs)
    items: list[NotificationItem] = []
    unread_count = 0
    for job in visible_jobs:
        job_id = str(getattr(job, "job_id", "") or "")
        updated_at = str(getattr(job, "updated_at", "") or "")
        unread = bool(last_seen) and updated_at > last_seen
        if unread:
            unread_count += 1
        items.append(
            NotificationItem(
                job_id=job_id,
                status=str(getattr(job, "status", "") or ""),
                title=str(getattr(job, "job_title", "") or ""),
                program=str(getattr(job, "program", "") or ""),
                db=str(getattr(job, "db", "") or ""),
                updated_at=updated_at,
                error_code=str(getattr(job, "error_code", "") or ""),
                error_detail=error_details.get(job_id, ""),
                unread=unread,
            )
        )

    return {
        "items": [item.as_dict() for item in items],
        "unread_count": unread_count,
        "last_seen_at": last_seen,
    }


def mark_all_seen(owner_oid: str) -> dict[str, Any]:
    """Advance the caller's marker to "now" so every current job reads as seen."""
    now = _now_iso()
    if not set_last_seen(owner_oid, now):
        raise NotificationMarkerWriteError("failed to persist last_seen_at")
    return {"last_seen_at": now, "unread_count": 0}


def clear_all_notifications(owner_oid: str) -> dict[str, Any]:
    """Hide current notifications without deleting their underlying job history."""
    now = _now_iso()
    if not _set_notification_marker(owner_oid, cleared_before_at=now):
        raise NotificationMarkerWriteError("failed to persist cleared_before_at")
    return {
        "cleared_before_at": now,
        "unread_count": 0,
    }

"""Cached Storage container usage snapshots.

Responsibility: Cache best-effort Storage container usage totals for monitor cards.
Edit boundaries: Keep only in-process usage snapshot caching here; Storage SDK enumeration stays
in `api.services.storage_data` and HTTP response shaping stays in routes/services.
Key entry points: `UsageCacheResult`, `cached_container_usage_summaries`,
`reset_storage_usage_cache`
Risky contracts: Cold cache misses must not block dashboard rendering on full blob enumeration.
Validation: `uv run pytest -q api/tests/test_storage_usage_cache.py`.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from azure.core.credentials import TokenCredential

from api.services import storage_data

LOGGER = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 300.0
_DEFAULT_STALE_SECONDS = 3600.0
_DEFAULT_MAX_ENTRIES = 256
_MAX_TTL_SECONDS = 3600.0
_MAX_STALE_SECONDS = 86_400.0
_MAX_ENTRIES_CAP = 4096


@dataclass(frozen=True)
class UsageCacheResult:
    summaries: dict[str, dict[str, Any]]
    state: str
    hit: bool
    pending: bool
    refreshed_at: str | None
    age_seconds: float | None


@dataclass
class _UsageEntry:
    # Serialized JSON bytes so reads do not pay a ``deepcopy`` tax on every
    # monitor poll. ``json.loads`` yields a fresh mutable dict on each hit.
    summaries_bytes: bytes | None
    refreshed_monotonic: float | None
    refreshed_wall: float | None
    expires_at: float
    stale_until: float
    refreshing: bool = False


_CACHE: OrderedDict[str, _UsageEntry] = OrderedDict()
_LOCK = threading.Lock()


def reset_storage_usage_cache() -> None:
    """Clear all cached usage snapshots. Test-only helper."""
    with _LOCK:
        _CACHE.clear()


def cached_container_usage_summaries(
    credential: TokenCredential,
    account_name: str,
    container_names: Iterable[str],
    *,
    max_blobs_per_container: int | None = None,
) -> UsageCacheResult:
    """Return cached per-container usage and refresh cold/stale entries asynchronously."""
    names = tuple(sorted({str(name) for name in container_names if str(name)}))
    if not names:
        return UsageCacheResult(
            summaries={},
            state="empty",
            hit=True,
            pending=False,
            refreshed_at=None,
            age_seconds=None,
        )

    ttl_seconds = _coerced_seconds(
        "STORAGE_USAGE_CACHE_TTL_SECONDS",
        default=_DEFAULT_TTL_SECONDS,
        maximum=_MAX_TTL_SECONDS,
    )
    stale_seconds = _coerced_seconds(
        "STORAGE_USAGE_CACHE_STALE_SECONDS",
        default=_DEFAULT_STALE_SECONDS,
        maximum=_MAX_STALE_SECONDS,
    )
    if ttl_seconds <= 0:
        summaries = _load_container_usage(
            credential,
            account_name,
            names,
            max_blobs_per_container=max_blobs_per_container,
        )
        now = _monotonic()
        wall = _wall_time()
        return _result_from_summaries(
            summaries,
            state="disabled",
            hit=False,
            pending=False,
            refreshed_monotonic=now,
            refreshed_wall=wall,
        )

    cache_key = _cache_key(account_name, names, max_blobs_per_container)
    now = _monotonic()
    refresh_needed = False
    with _LOCK:
        entry = _CACHE.get(cache_key)
        if entry is not None:
            _CACHE.move_to_end(cache_key)
            if entry.summaries_bytes is not None and entry.expires_at > now:
                return _result_from_entry(entry, state="fresh", hit=True)
            if entry.summaries_bytes is not None and entry.stale_until > now:
                if not entry.refreshing:
                    entry.refreshing = True
                    refresh_needed = True
                result = _result_from_entry(entry, state="stale", hit=True)
            elif entry.summaries_bytes is None and entry.refreshing:
                return _pending_result(names, state="pending", hit=True)
            else:
                entry = _UsageEntry(
                    summaries_bytes=None,
                    refreshed_monotonic=None,
                    refreshed_wall=None,
                    expires_at=now,
                    stale_until=now + stale_seconds,
                    refreshing=True,
                )
                _CACHE[cache_key] = entry
                refresh_needed = True
                result = _pending_result(names, state="pending", hit=False)
        else:
            entry = _UsageEntry(
                summaries_bytes=None,
                refreshed_monotonic=None,
                refreshed_wall=None,
                expires_at=now,
                stale_until=now + stale_seconds,
                refreshing=True,
            )
            _CACHE[cache_key] = entry
            refresh_needed = True
            result = _pending_result(names, state="pending", hit=False)
        _evict_over_capacity(now)

    if refresh_needed:
        _start_refresh_thread(
            lambda: _refresh(
                cache_key,
                credential,
                account_name,
                names,
                max_blobs_per_container=max_blobs_per_container,
                ttl_seconds=ttl_seconds,
                stale_seconds=stale_seconds,
            )
        )
        with _LOCK:
            refreshed = _CACHE.get(cache_key)
            if (
                refreshed is not None
                and refreshed.summaries_bytes is not None
                and not refreshed.refreshing
                and refreshed.expires_at > _monotonic()
            ):
                return _result_from_entry(refreshed, state="fresh", hit=True)
    return result


def _refresh(
    cache_key: str,
    credential: TokenCredential,
    account_name: str,
    container_names: tuple[str, ...],
    *,
    max_blobs_per_container: int | None,
    ttl_seconds: float,
    stale_seconds: float,
) -> None:
    try:
        summaries = _load_container_usage(
            credential,
            account_name,
            container_names,
            max_blobs_per_container=max_blobs_per_container,
        )
    except Exception as exc:
        LOGGER.warning(
            "storage usage refresh failed account=%s: %s",
            account_name,
            type(exc).__name__,
            exc_info=True,
        )
        summaries = _failed_summaries(container_names, type(exc).__name__)
    now = _monotonic()
    wall = _wall_time()
    serialized = json.dumps(summaries, default=str).encode("utf-8")
    with _LOCK:
        _CACHE[cache_key] = _UsageEntry(
            summaries_bytes=serialized,
            refreshed_monotonic=now,
            refreshed_wall=wall,
            expires_at=now + ttl_seconds,
            stale_until=now + ttl_seconds + stale_seconds,
            refreshing=False,
        )
        _CACHE.move_to_end(cache_key)
        _evict_over_capacity(now)


def _load_container_usage(
    credential: TokenCredential,
    account_name: str,
    container_names: tuple[str, ...],
    *,
    max_blobs_per_container: int | None,
) -> dict[str, dict[str, Any]]:
    return storage_data.container_usage_summaries(
        credential,
        account_name,
        container_names,
        max_blobs_per_container=max_blobs_per_container,
    )


def _pending_result(
    container_names: tuple[str, ...],
    *,
    state: str,
    hit: bool,
) -> UsageCacheResult:
    return UsageCacheResult(
        summaries={
            name: {
                "blob_count": None,
                "size_bytes": None,
                "usage_error": None,
                "usage_truncated": False,
            }
            for name in container_names
        },
        state=state,
        hit=hit,
        pending=True,
        refreshed_at=None,
        age_seconds=None,
    )


def _failed_summaries(
    container_names: tuple[str, ...], error_type: str
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "blob_count": None,
            "size_bytes": None,
            "usage_error": error_type,
            "usage_truncated": False,
        }
        for name in container_names
    }


def _result_from_entry(_entry: _UsageEntry, *, state: str, hit: bool) -> UsageCacheResult:
    if _entry.summaries_bytes is None:
        return UsageCacheResult(
            summaries={},
            state=state,
            hit=hit,
            pending=True,
            refreshed_at=None,
            age_seconds=None,
        )
    return _result_from_summaries(
        _entry.summaries_bytes,
        state=state,
        hit=hit,
        pending=False,
        refreshed_monotonic=_entry.refreshed_monotonic,
        refreshed_wall=_entry.refreshed_wall,
    )


def _result_from_summaries(
    summaries: dict[str, dict[str, Any]] | bytes,
    *,
    state: str,
    hit: bool,
    pending: bool,
    refreshed_monotonic: float | None,
    refreshed_wall: float | None,
) -> UsageCacheResult:
    age_seconds = None
    if refreshed_monotonic is not None:
        age_seconds = round(max(0.0, _monotonic() - refreshed_monotonic), 3)
    # Accept either a raw dict (cold path) or pre-serialized bytes (cache
    # hit path). ``json.loads`` plus the bytes path gives the caller a
    # fresh mutable dict at a fraction of ``deepcopy``'s cost.
    if isinstance(summaries, (bytes, bytearray)):
        out = json.loads(summaries)
    else:
        out = json.loads(json.dumps(summaries, default=str))
    return UsageCacheResult(
        summaries=out,
        state=state,
        hit=hit,
        pending=pending,
        refreshed_at=_wall_iso(refreshed_wall),
        age_seconds=age_seconds,
    )


def _cache_key(
    account_name: str,
    container_names: tuple[str, ...],
    max_blobs_per_container: int | None,
) -> str:
    limit = "unlimited" if max_blobs_per_container is None else str(max_blobs_per_container)
    return f"{account_name}:{limit}:{','.join(container_names)}"


def _coerced_seconds(env_name: str, *, default: float, maximum: float) -> float:
    raw = os.environ.get(env_name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, min(value, maximum))


def _max_entries() -> int:
    raw = os.environ.get("STORAGE_USAGE_CACHE_MAX_ENTRIES", str(_DEFAULT_MAX_ENTRIES)).strip()
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_ENTRIES
    return max(1, min(value, _MAX_ENTRIES_CAP))


def _evict_over_capacity(_now: float) -> None:
    max_entries = _max_entries()
    while len(_CACHE) > max_entries:
        _CACHE.popitem(last=False)


def _start_refresh_thread(target: Any) -> None:
    """Submit ``target`` to the shared storage-usage refresher pool."""

    def run() -> None:
        try:
            target()
        except Exception:
            LOGGER.warning("storage usage refresh thread failed", exc_info=True)

    pool = _refresher_pool()
    try:
        pool.submit(run)
    except RuntimeError:
        thread = threading.Thread(
            target=run, name="storage-usage-refresh-fallback", daemon=True
        )
        thread.start()


_REFRESHER_POOL_MAX_WORKERS = 4
_REFRESHER_POOL: ThreadPoolExecutor | None = None
_REFRESHER_POOL_LOCK = threading.Lock()


def _refresher_pool() -> ThreadPoolExecutor:
    global _REFRESHER_POOL
    pool = _REFRESHER_POOL
    if pool is not None:
        return pool
    with _REFRESHER_POOL_LOCK:
        if _REFRESHER_POOL is None:
            raw = os.environ.get("STORAGE_USAGE_REFRESHER_POOL_MAX_WORKERS", "")
            try:
                workers = (
                    max(1, min(int(raw), 32)) if raw else _REFRESHER_POOL_MAX_WORKERS
                )
            except ValueError:
                workers = _REFRESHER_POOL_MAX_WORKERS
            _REFRESHER_POOL = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="storage-usage-refresh",
            )
        return _REFRESHER_POOL


def _shutdown_refresher_pool() -> None:
    global _REFRESHER_POOL
    with _REFRESHER_POOL_LOCK:
        pool = _REFRESHER_POOL
        _REFRESHER_POOL = None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown_refresher_pool)


def _monotonic() -> float:
    return time.monotonic()


def _wall_time() -> float:
    return time.time()


def _wall_iso(wall_time: float | None) -> str | None:
    if wall_time is None:
        return None
    return datetime.fromtimestamp(wall_time, UTC).isoformat(timespec="seconds")

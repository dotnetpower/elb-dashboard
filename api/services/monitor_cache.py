"""Short-lived monitor snapshot cache.

Dashboard monitor routes read slow Azure control-plane and Kubernetes APIs.
This cache keeps the HTTP hot path fast for repeated polls while still letting
the first request after a cold start fetch authoritative data.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 30.0
_DEFAULT_STALE_SECONDS = 300.0
_DEFAULT_MAX_ENTRIES = 256
_MAX_TTL_SECONDS = 300.0
_MAX_STALE_SECONDS = 3600.0
_MAX_ENTRIES_CAP = 4096


@dataclass
class _SnapshotEntry:
    value: dict[str, Any]
    refreshed_at: float
    expires_at: float
    stale_until: float
    refreshing: bool = False


_CACHE: dict[str, _SnapshotEntry] = {}
_LOCK = threading.Lock()
_GENERATION = 0


def reset_monitor_snapshot_cache() -> None:
    """Clear all monitor snapshots. Test-only helper."""
    global _GENERATION
    with _LOCK:
        _CACHE.clear()
        _GENERATION += 1


def invalidate_monitor_snapshot_prefix(prefix: str) -> int:
    """Drop cached snapshots whose key equals ``prefix`` or starts with ``prefix + ":"``.

    Used by mutation endpoints (e.g. AKS start/stop/delete) so the next
    monitor poll bypasses the cached "Stopped" response and re-fetches
    authoritative state from ARM. Returns the number of entries removed.

    Bumping ``_GENERATION`` here is load-bearing: a background refresh
    triggered by the previous stale read may still be in flight when
    invalidation happens, and its `_refresh` callback would otherwise
    re-insert the (now-stale) ARM reading into the cache. The generation
    bump makes that callback a no-op.

    The match is boundary-safe (``prefix == key`` or ``key.startswith(prefix + ":")``)
    so resource groups whose names share a string prefix (``rg`` vs
    ``rg-elb-01``) do not invalidate each other.
    """
    global _GENERATION
    if not prefix:
        return 0
    removed = 0
    boundary = prefix + ":"
    with _LOCK:
        keys = [key for key in _CACHE if key == prefix or key.startswith(boundary)]
        for key in keys:
            _CACHE.pop(key, None)
            removed += 1
        if removed:
            _GENERATION += 1
    if removed:
        LOGGER.debug("monitor snapshot invalidate prefix=%s removed=%d", prefix, removed)
    return removed


def cached_snapshot(
    cache_key: str,
    loader: Callable[[], dict[str, Any]],
    *,
    ttl_seconds: float | None = None,
    stale_seconds: float | None = None,
) -> dict[str, Any]:
    """Return a cached monitor payload, refreshing stale entries in background.

    Fresh hit: return immediately.
    Stale hit: return immediately and refresh in a background thread.
    Cold miss / expired beyond stale window: refresh synchronously.
    """
    ttl = _coerced_seconds(
        ttl_seconds,
        env_name="MONITOR_SNAPSHOT_TTL_SECONDS",
        default=_DEFAULT_TTL_SECONDS,
        maximum=_MAX_TTL_SECONDS,
    )
    stale = _coerced_seconds(
        stale_seconds,
        env_name="MONITOR_SNAPSHOT_STALE_SECONDS",
        default=_DEFAULT_STALE_SECONDS,
        maximum=_MAX_STALE_SECONDS,
    )
    if ttl <= 0:
        return _with_cache_meta(
            loader(),
            state="disabled",
            hit=False,
            refreshed_at=_monotonic(),
            ttl_seconds=ttl,
        )

    now = _monotonic()
    refresh_in_background = False
    with _LOCK:
        generation = _GENERATION
        entry = _CACHE.get(cache_key)
        if entry is not None and entry.expires_at > now:
            return _with_cache_meta(
                entry.value,
                state="fresh",
                hit=True,
                refreshed_at=entry.refreshed_at,
                ttl_seconds=ttl,
            )
        if entry is not None and entry.stale_until > now:
            value = _with_cache_meta(
                entry.value,
                state="stale",
                hit=True,
                refreshed_at=entry.refreshed_at,
                ttl_seconds=ttl,
            )
            if not entry.refreshing:
                entry.refreshing = True
                refresh_in_background = True
        else:
            value = None

    if refresh_in_background:
        _start_refresh_thread(
            lambda: _refresh(
                cache_key,
                loader,
                ttl_seconds=ttl,
                stale_seconds=stale,
                generation=generation,
            )
        )
    if value is not None:
        return value

    try:
        refreshed = _refresh(
            cache_key,
            loader,
            ttl_seconds=ttl,
            stale_seconds=stale,
            generation=generation,
        )
    except Exception:
        with _LOCK:
            fallback = _CACHE.get(cache_key)
        if fallback is not None and fallback.stale_until > _monotonic():
            out = _with_cache_meta(
                fallback.value,
                state="stale_error",
                hit=True,
                refreshed_at=fallback.refreshed_at,
                ttl_seconds=ttl,
            )
            out["degraded"] = True
            out["degraded_reason"] = "snapshot_refresh_failed"
            return out
        raise
    return _with_cache_meta(
        refreshed.value,
        state="refreshed",
        hit=False,
        refreshed_at=refreshed.refreshed_at,
        ttl_seconds=ttl,
    )


def _refresh(
    cache_key: str,
    loader: Callable[[], dict[str, Any]],
    *,
    ttl_seconds: float,
    stale_seconds: float,
    generation: int,
) -> _SnapshotEntry:
    try:
        payload = loader()
        now = _monotonic()
        entry = _SnapshotEntry(
            value=deepcopy(payload),
            refreshed_at=now,
            expires_at=now + ttl_seconds,
            stale_until=now + ttl_seconds + stale_seconds,
        )
        with _LOCK:
            if generation == _GENERATION:
                _CACHE[cache_key] = entry
                _evict_over_capacity(now)
        return entry
    except Exception:
        with _LOCK:
            entry = _CACHE.get(cache_key)
            if entry is not None:
                entry.refreshing = False
        LOGGER.warning("monitor snapshot refresh failed key=%s", cache_key, exc_info=True)
        raise


def _start_refresh_thread(target: Callable[[], None]) -> None:
    def run() -> None:
        try:
            target()
        except Exception:
            # _refresh already logs the cache key and traceback. Keep the
            # daemon thread from emitting a second unhandled-exception traceback.
            return

    thread = threading.Thread(target=run, name="monitor-snapshot-refresh", daemon=True)
    thread.start()


def _with_cache_meta(
    value: dict[str, Any],
    *,
    state: str,
    hit: bool,
    refreshed_at: float,
    ttl_seconds: float,
) -> dict[str, Any]:
    out = deepcopy(value)
    age_seconds = max(0.0, _monotonic() - refreshed_at)
    out["cache"] = {
        "hit": hit,
        "state": state,
        "age_seconds": round(age_seconds, 3),
        "ttl_seconds": ttl_seconds,
        "refreshed_at": _wall_iso(refreshed_at),
    }
    return out


def _coerced_seconds(
    explicit: float | None,
    *,
    env_name: str,
    default: float,
    maximum: float,
) -> float:
    raw = explicit if explicit is not None else os.environ.get(env_name, "")
    if raw == "":
        return default
    try:
        return max(0.0, min(float(raw), maximum))
    except (TypeError, ValueError):
        return default


def _coerced_int(
    env_name: str,
    *,
    default: int,
    maximum: int,
) -> int:
    raw = os.environ.get(env_name, "")
    if raw == "":
        return default
    try:
        return max(1, min(int(raw), maximum))
    except (TypeError, ValueError):
        return default


def _evict_over_capacity(now: float) -> None:
    max_entries = _coerced_int(
        "MONITOR_SNAPSHOT_CACHE_MAX_ENTRIES",
        default=_DEFAULT_MAX_ENTRIES,
        maximum=_MAX_ENTRIES_CAP,
    )
    if len(_CACHE) <= max_entries:
        return

    stale_keys = [key for key, entry in _CACHE.items() if entry.stale_until <= now]
    for key in stale_keys:
        if len(_CACHE) <= max_entries:
            return
        _CACHE.pop(key, None)

    if len(_CACHE) <= max_entries:
        return

    for key, _entry in sorted(_CACHE.items(), key=lambda item: item[1].refreshed_at):
        if len(_CACHE) <= max_entries:
            return
        _CACHE.pop(key, None)


def _monotonic() -> float:
    return time.monotonic()


def _wall_iso(monotonic_value: float) -> str:
    wall = time.time() - (_monotonic() - monotonic_value)
    return datetime.fromtimestamp(wall, UTC).isoformat(timespec="seconds")

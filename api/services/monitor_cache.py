"""Short-lived monitor snapshot cache.

Responsibility: Short-lived monitor snapshot cache
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_SnapshotEntry`, `reset_monitor_snapshot_cache`,
`invalidate_monitor_snapshot_prefix`, `cached_snapshot`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from api.services.background_refresh import DaemonRefreshPool

LOGGER = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 30.0
_DEFAULT_STALE_SECONDS = 300.0
_DEFAULT_MAX_ENTRIES = 256
_MAX_TTL_SECONDS = 300.0
_MAX_STALE_SECONDS = 3600.0
_MAX_ENTRIES_CAP = 4096


@dataclass
class _SnapshotEntry:
    # Serialized JSON bytes so reads do not pay a ``deepcopy`` tax. JSON
    # round-trip on read yields a fresh mutable dict (same isolation as
    # deepcopy) at a fraction of the cost for the dict-of-primitives
    # shape monitor snapshots actually carry.
    payload_bytes: bytes
    refreshed_at: float
    expires_at: float
    stale_until: float
    refreshing: bool = False


_CACHE: dict[str, _SnapshotEntry] = {}
_LOCK = threading.Lock()
_GENERATION = 0


# Transient-failure dedup window. The same cache key (and the same exception
# class) repeating inside this window is logged only once with `exc_info=True`;
# further occurrences are demoted to a one-line warning so the Azure Monitor
# OpenTelemetry logging exporter does not record a fresh AppInsights exception
# row every poll tick. Window is intentionally short — long enough to absorb
# a dashboard poll burst (every 15-30 s) but not so long that a sustained
# outage stops emitting exceptions entirely.
_TRANSIENT_DEDUP_WINDOW_SECONDS = 300.0
_TRANSIENT_DEDUP_MAX_ENTRIES = 256
# (cache_key, exc_class_name) -> last_emitted_monotonic
_TRANSIENT_DEDUP: dict[tuple[str, str], float] = {}
_TRANSIENT_DEDUP_ORDER: deque[tuple[str, str]] = deque()
_TRANSIENT_DEDUP_LOCK = threading.Lock()


def _is_transient_refresh_failure(exc: BaseException) -> bool:
    """Classify ``exc`` as a transient cluster/network reach failure.

    Used by ``_refresh`` to decide whether to emit a full stack trace (and
    therefore a recorded App Insights exception) or just a one-line warning.
    Returns True for the well-known "the cluster is stopped / DNS just
    hiccuped / ARM returned 5xx" family — those routinely degrade to the
    stale cache fallback and do not warrant a per-poll exception row.
    """
    try:
        from requests.exceptions import (
            ConnectionError as _RequestsConnectionError,
        )
        from requests.exceptions import (
            ConnectTimeout as _RequestsConnectTimeout,
        )
        from requests.exceptions import (
            ReadTimeout as _RequestsReadTimeout,
        )
    except ImportError:  # pragma: no cover - requests is a transitive dep
        _RequestsConnectionError = ()  # type: ignore[assignment]
        _RequestsConnectTimeout = ()  # type: ignore[assignment]
        _RequestsReadTimeout = ()  # type: ignore[assignment]

    if isinstance(exc, (_RequestsConnectionError, _RequestsConnectTimeout, _RequestsReadTimeout)):
        return True

    try:
        from azure.core.exceptions import (
            HttpResponseError,
            ResourceNotFoundError,
            ServiceRequestError,
        )
    except ImportError:  # pragma: no cover
        return False

    if isinstance(exc, (ResourceNotFoundError, ServiceRequestError)):
        return True
    if isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status in {404, 408, 429, 500, 502, 503, 504}:
            return True
    return False


def _should_suppress_transient_telemetry(cache_key: str, exc: BaseException) -> bool:
    """Return True when an identical transient failure was logged with stack
    inside the dedup window. Caller still logs a one-line warning — only the
    `exc_info=True` stack (which the OTel logging exporter turns into an
    App Insights exception row) is suppressed on repeats.
    """
    key = (cache_key, type(exc).__name__)
    now = _monotonic()
    cutoff = now - _TRANSIENT_DEDUP_WINDOW_SECONDS
    with _TRANSIENT_DEDUP_LOCK:
        # Evict expired entries from the front of the order queue first so the
        # cap stays meaningful (the dict can outlive the deque if we skip).
        while _TRANSIENT_DEDUP_ORDER:
            head = _TRANSIENT_DEDUP_ORDER[0]
            last = _TRANSIENT_DEDUP.get(head)
            if last is None or last < cutoff:
                _TRANSIENT_DEDUP_ORDER.popleft()
                _TRANSIENT_DEDUP.pop(head, None)
                continue
            break
        last = _TRANSIENT_DEDUP.get(key)
        if last is not None and last >= cutoff:
            return True
        # Record this emission as the new "last full record" timestamp.
        _TRANSIENT_DEDUP[key] = now
        _TRANSIENT_DEDUP_ORDER.append(key)
        # Hard cap defence — should rarely fire because the cutoff loop above
        # already evicts; leave as belt-and-braces against an unbounded set
        # of cache keys.
        while len(_TRANSIENT_DEDUP_ORDER) > _TRANSIENT_DEDUP_MAX_ENTRIES:
            oldest = _TRANSIENT_DEDUP_ORDER.popleft()
            _TRANSIENT_DEDUP.pop(oldest, None)
    return False


def _reset_transient_dedup() -> None:
    """Test-only: clear the dedup window so tests are deterministic."""
    with _TRANSIENT_DEDUP_LOCK:
        _TRANSIENT_DEDUP.clear()
        _TRANSIENT_DEDUP_ORDER.clear()


# OpenTelemetry counter for "monitor snapshot refresh failed" events. Lets
# the operator alert on a SUSTAINED refresh failure spike (a real env-wide
# outage) without parsing AppInsights exception rows, which we now dedup.
# The meter is created lazily so a process without OTel initialised still
# imports this module cleanly.
_REFRESH_FAILURE_COUNTER: Any = None
_REFRESH_FAILURE_COUNTER_LOCK = threading.Lock()


def _get_refresh_failure_counter() -> Any:
    global _REFRESH_FAILURE_COUNTER
    if _REFRESH_FAILURE_COUNTER is not None:
        return _REFRESH_FAILURE_COUNTER
    with _REFRESH_FAILURE_COUNTER_LOCK:
        if _REFRESH_FAILURE_COUNTER is not None:
            return _REFRESH_FAILURE_COUNTER
        try:
            from opentelemetry import metrics

            meter = metrics.get_meter("api.services.monitor_cache")
            _REFRESH_FAILURE_COUNTER = meter.create_counter(
                "elb_monitor_snapshot_refresh_failed",
                unit="1",
                description=(
                    "Count of monitor snapshot loader failures, "
                    "labelled by exception class and whether a stale "
                    "fallback was available."
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.debug("OTel meter unavailable: %s", type(exc).__name__)
            _REFRESH_FAILURE_COUNTER = _NullCounter()
    return _REFRESH_FAILURE_COUNTER


class _NullCounter:
    def add(self, value: float, attributes: dict[str, Any] | None = None) -> None:
        return None


def _reset_refresh_failure_counter() -> None:
    """Test-only: drop the cached counter so the meter is re-resolved on
    next use (e.g. after monkeypatching OTel)."""
    global _REFRESH_FAILURE_COUNTER
    with _REFRESH_FAILURE_COUNTER_LOCK:
        _REFRESH_FAILURE_COUNTER = None


def reset_monitor_snapshot_cache() -> None:
    """Clear all monitor snapshots. Test-only helper."""
    global _GENERATION
    with _LOCK:
        _CACHE.clear()
        _GENERATION += 1
    _reset_transient_dedup()


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
    force: bool = False,
) -> dict[str, Any]:
    """Return a cached monitor payload, refreshing stale entries in background.

    Fresh hit: return immediately.
    Stale hit: return immediately and refresh in a background thread.
    Cold miss / expired beyond stale window: refresh synchronously.

    ``force=True`` bypasses the fresh/stale cache read and always refreshes
    synchronously from ``loader`` (still storing the result for subsequent
    normal reads). This is the cross-process-safe way for the SPA to get an
    authoritative ARM reading the moment a lifecycle transition settles: the
    monitor cache is per-process (the ``worker`` sidecar cannot invalidate the
    ``api`` sidecar's cache), so an in-flight transition asks the api process
    to re-query ARM directly instead of waiting out the TTL.
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
            json.dumps(loader(), default=str).encode("utf-8"),
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
        if not force and entry is not None and entry.expires_at > now:
            return _with_cache_meta(
                entry.payload_bytes,
                state="fresh",
                hit=True,
                refreshed_at=entry.refreshed_at,
                ttl_seconds=ttl,
            )
        if not force and entry is not None and entry.stale_until > now:
            value = _with_cache_meta(
                entry.payload_bytes,
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
                fallback.payload_bytes,
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
        refreshed.payload_bytes,
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
        # ``default=str`` makes datetime / UUID values JSON-safe instead of
        # raising; monitor loaders historically returned anything dict-ish.
        serialized = json.dumps(payload, default=str).encode("utf-8")
        entry = _SnapshotEntry(
            payload_bytes=serialized,
            refreshed_at=now,
            expires_at=now + ttl_seconds,
            stale_until=now + ttl_seconds + stale_seconds,
        )
        with _LOCK:
            if generation == _GENERATION:
                _CACHE[cache_key] = entry
                _evict_over_capacity(now)
            else:
                # A generation bump raced this in-flight refresh, so we discard
                # this now-possibly-stale result (the invalidation wanted a
                # re-fetch). The generation counter is GLOBAL but invalidation
                # is per-prefix, so an invalidation of an UNRELATED key also
                # lands here for THIS key while leaving its entry in the cache.
                # Without clearing the stuck ``refreshing`` flag that entry
                # would block every future background refresh of this key until
                # its stale window expires (up to _MAX_STALE_SECONDS), serving
                # stale far longer than intended. Reset the flag so the next
                # poll re-triggers a background refresh immediately (liveness).
                current = _CACHE.get(cache_key)
                if current is not None:
                    current.refreshing = False
        return entry
    except Exception as exc:
        with _LOCK:
            stale_entry = _CACHE.get(cache_key)
            if stale_entry is not None:
                stale_entry.refreshing = False
        # Demote the well-known "cluster stopped / DNS hiccup / ARM 5xx"
        # family to a one-line warning so the Azure Monitor OpenTelemetry
        # logging exporter does not record a fresh AppInsights exception
        # row every poll tick. The full stack trace is still emitted on the
        # first failure inside the dedup window (or whenever no stale
        # fallback exists) so a real new fault is never hidden.
        transient = _is_transient_refresh_failure(exc) and stale_entry is not None
        if transient and _should_suppress_transient_telemetry(cache_key, exc):
            LOGGER.warning(
                "monitor snapshot refresh failed key=%s reason=%s (stale fallback, deduped)",
                cache_key,
                type(exc).__name__,
            )
        else:
            LOGGER.warning("monitor snapshot refresh failed key=%s", cache_key, exc_info=True)
        try:
            _get_refresh_failure_counter().add(
                1,
                {
                    "exception_class": type(exc).__name__,
                    "stale_fallback": bool(stale_entry is not None),
                    "transient": bool(transient),
                },
            )
        except Exception:  # noqa: S110 - counter must never break refresh
            pass
        raise


def _start_refresh_thread(target: Callable[[], object]) -> None:
    """Submit ``target`` to the shared refresher pool.

    Previously spawned a brand-new ``threading.Thread`` per stale entry.
    Under SSE + multiple monitor routes that meant 10+ thread spawns
    per dashboard tick. A capped daemon worker pool keeps the background
    refresh fan-out bounded (never more than N concurrent ARM refreshes
    against the same subscription) without leaking a non-daemon thread that
    blocks interpreter / xdist-worker shutdown — see ``DaemonRefreshPool``.
    """

    def run() -> None:
        try:
            target()
        except Exception:
            # _refresh already logs the cache key and traceback. Keep
            # the worker from emitting a second unhandled-exception trace.
            return

    # In the test suite, run the refresh inline so no background daemon thread
    # survives to interpreter / pytest-xdist worker shutdown. A daemon worker
    # blocked in a C-level network call when the interpreter finalizes can crash
    # the worker ("[gwN] node down: Not properly terminated", no traceback),
    # which intermittently hung CI to the job timeout. Refresh-path tests already
    # monkeypatch this function to run inline; the gate only affects tests that
    # trigger a refresh without monkeypatching. Unset in production (daemon pool).
    if os.environ.get("ELB_TEST_INLINE_BACKGROUND_REFRESH"):
        run()
        return

    _refresher_pool().submit(run)


_REFRESHER_POOL_MAX_WORKERS = 8


def _resolve_refresher_pool_max_workers() -> int:
    raw = os.environ.get("MONITOR_REFRESHER_POOL_MAX_WORKERS", "")
    if raw:
        try:
            return max(1, min(int(raw), 64))
        except ValueError:
            return _REFRESHER_POOL_MAX_WORKERS
    return _REFRESHER_POOL_MAX_WORKERS


_REFRESHER_POOL: DaemonRefreshPool | None = None
_REFRESHER_POOL_LOCK = threading.Lock()


def _refresher_pool() -> DaemonRefreshPool:
    global _REFRESHER_POOL
    pool = _REFRESHER_POOL
    if pool is not None:
        return pool
    with _REFRESHER_POOL_LOCK:
        if _REFRESHER_POOL is None:
            workers = _resolve_refresher_pool_max_workers()
            _REFRESHER_POOL = DaemonRefreshPool(
                max_workers=workers,
                # Bounded backlog: under a sustained ARM outage every poll tick
                # enqueues a refresh while all workers are blocked; cap the
                # backlog so memory stays bounded (excess refreshes are dropped
                # and the stale cache is served instead).
                max_queue=max(64, workers * 16),
                name="monitor-snapshot-refresh",
            )
        return _REFRESHER_POOL


def _with_cache_meta(
    payload_bytes: bytes,
    *,
    state: str,
    hit: bool,
    refreshed_at: float,
    ttl_seconds: float,
) -> dict[str, Any]:
    out = json.loads(payload_bytes)
    if not isinstance(out, dict):
        out = {"value": out}
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

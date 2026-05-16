"""Cross-sidecar UI animation event emitter.

The Control Plane Sidecars card animates a single particle along each
row of its topology graph for every *real* event that just happened —
HTTP request to the api, task enqueued onto the broker, scheduled beat
tick, terminal proxy hit. The infinite CSS animation that originally
shipped was decorative and didn't reflect anything; this module is the
plumbing that replaces it with truth.

Counters live in the in-revision Redis (db 2) under a single hash so the
api sidecar's snapshot builder can drain them with one round-trip every
SSE tick (5 s). Each emitter call is a single HINCRBY — fire-and-forget,
non-blocking, never raised: animation is decorative and must not affect
the request that triggered it.

Row → meaning mapping (mirrored in `web/src/components/cards/SidecarsCard.tsx`):

    row1  Browser → frontend → api      every non-health, non-terminal request
    row2  api → redis → worker          every Celery task enqueued by api
    row3  beat → redis                   every Celery task enqueued by beat
    row4  api ↔ terminal                 every /api/terminal/* request

Note: emitting from worker / beat / terminal still writes to the same hash
because they all share OPS_REDIS_URL; the api sidecar drains and reports.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import redis

LOGGER = logging.getLogger(__name__)

EVENTS_HASH = "sidecar:events"

ROW_HTTP = "row1"
ROW_ASYNC = "row2"
ROW_SCHED = "row3"
ROW_TERM = "row4"

ROW_FIELDS: tuple[str, ...] = (ROW_HTTP, ROW_ASYNC, ROW_SCHED, ROW_TERM)


def _float_env(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        LOGGER.warning("event_emitter: invalid %s=%r; using %.3f", name, raw, default)
        return default
    return max(minimum, value)


def _int_env(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("event_emitter: invalid %s=%r; using %d", name, raw, default)
        return default
    return max(minimum, value)


_CONNECT_TIMEOUT_SECONDS = _float_env("EVENT_EMIT_CONNECT_TIMEOUT_SECONDS", 0.05, minimum=0.001)
_SOCKET_TIMEOUT_SECONDS = _float_env("EVENT_EMIT_SOCKET_TIMEOUT_SECONDS", 0.05, minimum=0.001)
_FAILURE_COOLDOWN_SECONDS = _float_env("EVENT_EMIT_FAILURE_COOLDOWN_SECONDS", 5.0, minimum=0.0)
_MAX_COUNT = _int_env("EVENT_EMIT_MAX_COUNT", 1000, minimum=1)

_lock = threading.Lock()
_client: redis.Redis | None = None
_disabled = False
_disabled_until = 0.0


def _get_client() -> redis.Redis | None:
    """Return a cached Redis client, or None if connection setup failed."""
    global _client, _disabled, _disabled_until
    if _disabled:
        return None
    now = time.monotonic()
    if now < _disabled_until:
        return None
    with _lock:
        now = time.monotonic()
        if now < _disabled_until:
            return None
        if _client is not None:
            return _client
        try:
            url = os.environ.get("OPS_REDIS_URL", "redis://127.0.0.1:6379/2")
            _client = redis.Redis.from_url(
                url,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                health_check_interval=30,
            )
        except Exception as exc:
            LOGGER.warning("event_emitter: client init failed: %s", exc)
            _disabled_until = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
            _client = None
        return _client


def _record_failure() -> None:
    global _client, _disabled_until
    with _lock:
        _client = None
        _disabled_until = time.monotonic() + _FAILURE_COOLDOWN_SECONDS


def _normalise_count(count: int) -> int:
    if count <= 0:
        return 0
    return min(count, _MAX_COUNT)


def emit(row: str, count: int = 1) -> None:
    """Best-effort: bump a row counter so the next snapshot drains it.

    Failures are swallowed — the animation is decorative and must never
    bubble back into the request path.
    """
    amount = _normalise_count(count)
    if amount <= 0 or row not in ROW_FIELDS:
        return
    cli = _get_client()
    if cli is None:
        return
    try:
        cli.hincrby(EVENTS_HASH, row, amount)
    except Exception as exc:
        _record_failure()
        # Don't spam logs on every redis blip — keep at debug level since
        # `emit` is called per request and decorative animation must not
        # affect request latency or user-visible logs.
        LOGGER.debug("event_emitter: emit failed: %s", exc)


def drain(client: redis.Redis | None = None) -> dict[str, int]:
    """Atomically read+reset all row counters. Used by snapshot builder.

    Returns a dict with all four row keys present (zeroes when missing or
    on Redis error) so the SPA can rely on a stable shape.
    """
    cli = client or _get_client()
    zero = {field: 0 for field in ROW_FIELDS}
    if cli is None:
        return zero
    try:
        pipe = cli.pipeline()
        pipe.hgetall(EVENTS_HASH)
        pipe.delete(EVENTS_HASH)
        results = pipe.execute()
    except Exception as exc:
        # Same circuit breaker as emit() — keeps the next snapshot tick
        # from re-paying the timeout while Redis is misbehaving.
        _record_failure()
        LOGGER.warning("event_emitter: drain failed: %s", exc)
        return zero
    raw = results[0] or {}
    out = dict(zero)
    for key, value in raw.items():
        field = key.decode() if isinstance(key, bytes) else str(key)
        if field not in ROW_FIELDS:
            continue
        try:
            out[field] = max(0, min(int(value), _MAX_COUNT))
        except (TypeError, ValueError):
            out[field] = 0
    return out


def reset_for_tests() -> None:
    """Test helper — clear the cached client so tests can patch the URL."""
    global _client, _disabled, _disabled_until
    with _lock:
        _client = None
        _disabled = False
        _disabled_until = 0.0

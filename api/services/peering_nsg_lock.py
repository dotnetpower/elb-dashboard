"""Distributed NSG-write serialisation lock.

Responsibility: Provide a cross-process, cross-replica mutual-exclusion
primitive keyed by NSG ARM id so two Settings-page operators cannot race
on the inbound-rule priority picker. Tries the in-revision Redis sidecar
first (``CELERY_BROKER_URL``); falls back to a process-local
``threading.Lock`` with TTL eviction when Redis is unavailable so unit
tests can exercise the route without spinning up a broker.

Edit boundaries: stdlib + ``redis`` only. No FastAPI / Celery / Azure
SDK imports. Routes call ``acquire_nsg_lock(...)`` once per apply and
release in a ``finally`` block. The route layer never touches the
private fallback dict directly.

Key entry points: ``acquire_nsg_lock``, ``NsgLockHandle.release``.

Risky contracts: Lock token is per-acquire and uses
``secrets.token_hex(16)`` so no two acquires can ever collide on the
same string — the Lua CAS release would otherwise free another holder.
Memory fallback entries are TTL-evicted on release so a long-running
api sidecar does not leak ``threading.Lock`` objects for retired NSGs.
A short-lived circuit breaker around the Redis client lookup avoids
hammering ``redis_clients`` on every acquire when Redis is down.

Validation: ``uv run pytest -q api/tests/test_peering_nsg_lock.py``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any

LOGGER = logging.getLogger(__name__)

NSG_LOCK_KEY_PREFIX = "elb:peering-nsg:apply"
NSG_LOCK_DEFAULT_TTL_SECONDS = 180
NSG_LOCK_DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 15.0
_MEMORY_GRACE_SECONDS = 30.0

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
_RELEASE_LUA_SHA1 = hashlib.sha1(  # noqa: S324 - SHA1 is required by Redis EVALSHA
    _RELEASE_LUA.encode("utf-8")
).hexdigest()

# Short-circuit window after a Redis client lookup failure. Avoids paying
# the import + try/except cost of ``get_broker_redis_client`` on every
# acquire when Redis has been down for more than a few seconds. 1.0 s is
# tight enough that recovery (sidecar restart) is observed almost
# immediately and loose enough to cut overhead during sustained outages.
_REDIS_BREAKER_WINDOW_SECONDS = 1.0


def _short_key(nsg_id: str) -> str:
    digest = hashlib.sha256(nsg_id.encode("utf-8")).hexdigest()[:16]
    return f"{NSG_LOCK_KEY_PREFIX}:{digest}"


@dataclass
class _MemoryEntry:
    lock: threading.Lock
    holder_token: str | None
    free_since: float  # monotonic timestamp when last released; 0 while held


_MEM_GUARD = threading.Lock()
_MEM_LOCKS: dict[str, _MemoryEntry] = {}

# Circuit breaker state for `_redis_client_or_none`. Protected by `_MEM_GUARD`
# so we don't add a second lock.
_REDIS_BREAKER_LAST_FAILURE: float = 0.0


def _evict_free_entries_locked() -> None:
    """Drop fallback entries that have been free past the grace window.

    Called under ``_MEM_GUARD``. Held entries (``holder_token is not None``)
    are intentionally kept — they're bounded by the route's overall
    deadline.
    """
    now = time.monotonic()
    stale = [
        k for k, e in _MEM_LOCKS.items()
        if e.holder_token is None and e.free_since and (now - e.free_since) > _MEMORY_GRACE_SECONDS
    ]
    for k in stale:
        _MEM_LOCKS.pop(k, None)


def _try_release_via_redis(client: Any, key: str, token: str) -> None:
    """Run the Lua CAS release, preferring EVALSHA to reduce wire bytes.

    Falls back to EVAL once on a NOSCRIPT response so a fresh Redis
    instance auto-loads the script. Any other error is logged at
    WARNING — the TTL set at acquire time guarantees the lock cannot
    linger forever.
    """
    try:
        try:
            client.evalsha(_RELEASE_LUA_SHA1, 1, key, token)
            return
        except AttributeError:
            # Stripped-down fake without evalsha — fall through to eval.
            pass
        except Exception as exc:
            message = str(exc).upper()
            if "NOSCRIPT" not in message:
                raise
            LOGGER.debug("peering_nsg lock: EVALSHA NOSCRIPT, retrying via EVAL")
        client.eval(_RELEASE_LUA, 1, key, token)
    except Exception as exc:
        # WARNING (not INFO) — release failure means the lock will sit
        # for the remainder of its TTL and a re-clicked Apply will see a
        # 423 BUSY. The short key already hashes the NSG id so logging
        # it is safe.
        LOGGER.warning(
            "peering_nsg lock redis release failed key=%s err=%s",
            key,
            type(exc).__name__,
        )


@dataclass
class NsgLockHandle:
    """Handle returned by :func:`acquire_nsg_lock`. Always ``release()``."""

    key: str
    token: str
    backend: str  # "redis" or "memory"
    _client: Any | None
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.backend == "redis" and self._client is not None:
            _try_release_via_redis(self._client, self.key, self.token)
            return
        # memory backend.
        with _MEM_GUARD:
            entry = _MEM_LOCKS.get(self.key)
            if entry is not None and entry.holder_token == self.token:
                entry.holder_token = None
                entry.free_since = time.monotonic()
                try:
                    entry.lock.release()
                except RuntimeError:
                    # Already released externally — defensive.
                    pass
                _evict_free_entries_locked()

    def __enter__(self) -> NsgLockHandle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        self.release()


def _redis_client_or_none() -> Any | None:
    """Return a Redis client, or ``None`` when unreachable.

    A 1-second circuit breaker (``_REDIS_BREAKER_WINDOW_SECONDS``)
    short-circuits the import + try/except cost while Redis is known
    down. ``api.services.redis_clients.get_broker_redis_client`` is
    re-imported on every lookup so the test suite can swap
    ``_redis_client_or_none`` itself without dealing with the breaker.
    """
    global _REDIS_BREAKER_LAST_FAILURE

    now = time.monotonic()
    with _MEM_GUARD:
        if (
            _REDIS_BREAKER_LAST_FAILURE
            and now - _REDIS_BREAKER_LAST_FAILURE < _REDIS_BREAKER_WINDOW_SECONDS
        ):
            return None

    try:
        from api.services.redis_clients import get_broker_redis_client

        return get_broker_redis_client()
    except Exception as exc:
        LOGGER.info(
            "peering_nsg lock: redis client unavailable (%s) — using memory fallback",
            type(exc).__name__,
        )
        with _MEM_GUARD:
            _REDIS_BREAKER_LAST_FAILURE = time.monotonic()
        return None


def acquire_nsg_lock(
    nsg_id: str,
    *,
    timeout_seconds: float = NSG_LOCK_DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
    ttl_seconds: int = NSG_LOCK_DEFAULT_TTL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> NsgLockHandle | None:
    """Acquire a per-NSG mutual-exclusion lock or return ``None`` on timeout.

    ``timeout_seconds`` bounds how long the caller will wait for the
    lock to become free. ``ttl_seconds`` is the Redis SET-EX TTL — the
    outer-bound on a single apply (ARM retries + LRO) so a crashed
    holder can never permanently block subsequent callers.
    """
    global _REDIS_BREAKER_LAST_FAILURE

    key = _short_key(nsg_id)
    token = secrets.token_hex(16)
    deadline = now() + max(0.0, timeout_seconds)

    client = _redis_client_or_none()
    if client is not None:
        while True:
            try:
                acquired = client.set(key, token, nx=True, ex=ttl_seconds)
            except Exception as exc:
                LOGGER.warning(
                    "peering_nsg lock redis SET failed (%s) — falling back to memory",
                    type(exc).__name__,
                )
                # Trip the breaker so the next acquire skips the import +
                # client lookup until the window expires.
                with _MEM_GUARD:
                    _REDIS_BREAKER_LAST_FAILURE = time.monotonic()
                client = None
                break
            if acquired:
                return NsgLockHandle(
                    key=key, token=token, backend="redis", _client=client
                )
            remaining = deadline - now()
            if remaining <= 0:
                return None
            sleep(min(0.25, max(0.05, remaining)))

    # Memory fallback path.
    with _MEM_GUARD:
        _evict_free_entries_locked()
        entry = _MEM_LOCKS.get(key)
        if entry is None:
            entry = _MemoryEntry(
                lock=threading.Lock(),
                holder_token=None,
                free_since=now(),
            )
            _MEM_LOCKS[key] = entry
    wait = deadline - now()
    if wait <= 0:
        # Past the deadline already — do not attempt a non-blocking
        # acquire; callers pass ``timeout_seconds=0`` to mean "give up",
        # not "try once".
        return None
    acquired = entry.lock.acquire(timeout=wait)
    if not acquired:
        return None
    with _MEM_GUARD:
        entry.holder_token = token
        entry.free_since = 0.0
    return NsgLockHandle(key=key, token=token, backend="memory", _client=None)


def reset_memory_locks_for_tests() -> None:
    """Test hook — drop the fallback dict so each test starts clean."""
    global _REDIS_BREAKER_LAST_FAILURE
    with _MEM_GUARD:
        _MEM_LOCKS.clear()
        _REDIS_BREAKER_LAST_FAILURE = 0.0


# Backwards-compatible alias for any caller that still imports the
# underscore-prefixed name. Will be removed once external callers update.
_reset_memory_locks_for_tests = reset_memory_locks_for_tests


__all__ = [
    "NSG_LOCK_DEFAULT_ACQUIRE_TIMEOUT_SECONDS",
    "NSG_LOCK_DEFAULT_TTL_SECONDS",
    "NSG_LOCK_KEY_PREFIX",
    "NsgLockHandle",
    "acquire_nsg_lock",
    "reset_memory_locks_for_tests",
]

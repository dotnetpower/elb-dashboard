"""Redis coordination primitives for Service Bus queue draining.

Responsibility: Coordinate queue-scoped drain leases and auto-stop intent
    fences through Redis without knowing Service Bus messages or Celery tasks.
Edit boundaries: Redis key construction, bounded lease configuration, and
    acquire/release operations only. Drain admission and message handling remain
    in ``api.tasks.servicebus.tasks``.
Key entry points: ``drain_concurrency_from_env``, ``drain_lock_ttl_from_env``,
    ``acquire_drain_lock``, ``release_drain_lock``,
    ``acquire_drain_stop_intent``, ``release_drain_stop_intent``,
    ``acquire_request_send``, ``release_request_send``,
    ``acquire_config_mutation``, ``release_config_mutation``.
Risky contracts: Drain lease acquisition fails closed on Redis errors because
    Settings also uses this lease as its routing-mutation fence; the atomic
    bridge claim remains the execution-idempotency backstop.
    Stop-intent acquisition fails closed because an uncoordinated stop could
    interrupt a PEEK_LOCKed submit. The in-flight token set covers every
    config-dependent data-plane mutation, not only request sends. The config
    mutation mutex serializes full-row Settings writes. Compare-and-delete
    release must never remove a lease owned by a newer caller. Celery soft
    deadlines propagate through every Redis catch-all. Every active-operation
    lease has the same 900-second safety floor and shared-key TTLs never shrink.
Validation: ``uv run pytest -q api/tests/test_servicebus_tasks.py
    api/tests/test_auto_stop_task.py api/tests/test_servicebus_load.py``.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from typing import Any, cast

from billiard.exceptions import SoftTimeLimitExceeded

LOGGER = logging.getLogger(__name__)

RedisFactory = Callable[..., Any]
DEFAULT_STOP_INTENT_BASE_KEY = "servicebus:drain:stop-intent"
DEFAULT_SEND_INFLIGHT_BASE_KEY = "servicebus:send:inflight"
DEFAULT_CONFIG_MUTATION_KEY = "servicebus:config:mutation"
MIN_DRAIN_LOCK_TTL_SECONDS = 900
DEFAULT_SEND_INFLIGHT_TTL_SECONDS = MIN_DRAIN_LOCK_TTL_SECONDS
DEFAULT_CONFIG_MUTATION_TTL_SECONDS = MIN_DRAIN_LOCK_TTL_SECONDS


class RequestSendCoordinationUnavailable(RuntimeError):
    """Raised when an internal send cannot be fenced safely through Redis."""


LOCK_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)
LOCK_ACQUIRE_LUA = (
    "if redis.call('exists', KEYS[2]) == 1 then return 0 end "
    "if redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then return 1 end "
    "return 0"
)
STOP_INTENT_ACQUIRE_LUA = (
    "local now = redis.call('TIME') "
    "local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000) "
    "redis.call('zremrangebyscore', KEYS[3], '-inf', now_ms) "
    "if redis.call('exists', KEYS[1]) == 1 then return 0 end "
    "if redis.call('exists', KEYS[2]) == 1 then return 0 end "
    "if redis.call('zcard', KEYS[3]) > 0 then return 0 end "
    "if redis.call('set', KEYS[2], ARGV[1], 'NX', 'EX', ARGV[2]) "
    "then return 1 end return 0"
)
SEND_ACQUIRE_LUA = (
    "if redis.call('exists', KEYS[1]) == 1 then return 0 end "
    "local now = redis.call('TIME') "
    "local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000) "
    "redis.call('zremrangebyscore', KEYS[2], '-inf', now_ms) "
    "redis.call('zadd', KEYS[2], now_ms + tonumber(ARGV[2]) * 1000, ARGV[1]) "
    "local ttl = redis.call('ttl', KEYS[2]) "
    "if ttl < tonumber(ARGV[2]) then redis.call('expire', KEYS[2], ARGV[2]) end "
    "return 1"
)
SEND_RELEASE_LUA = (
    "local retain = tonumber(ARGV[2]) or 0 "
    "if retain > 0 then "
    "local now = redis.call('TIME') "
    "local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000) "
    "redis.call('zadd', KEYS[1], now_ms + retain * 1000, ARGV[1]) "
    "local ttl = redis.call('ttl', KEYS[1]) "
    "if ttl < retain then redis.call('expire', KEYS[1], retain) end return 1 end "
    "local removed = redis.call('zrem', KEYS[1], ARGV[1]) "
    "if redis.call('zcard', KEYS[1]) == 0 then redis.call('del', KEYS[1]) end "
    "return removed"
)


def _redis_factory() -> RedisFactory:
    from api.services.redis_clients import get_broker_redis_client

    return cast(RedisFactory, get_broker_redis_client)


def drain_concurrency_from_env(logger: logging.Logger) -> int:
    """Resolve drain fan-out from env, clamped to [1, 32]."""
    raw = os.environ.get("SERVICEBUS_DRAIN_CONCURRENCY", "1")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("invalid SERVICEBUS_DRAIN_CONCURRENCY=%r; defaulting to 1 (serial)", raw)
        value = 1
    return max(1, min(32, value))


def drain_lock_key(queue_name: str, *, base_key: str) -> str:
    """Return a queue-scoped lease key."""
    return f"{base_key}:{queue_name}" if queue_name else base_key


def drain_stop_intent_key(queue_name: str, *, base_key: str) -> str:
    """Return a queue-scoped auto-stop intent key."""
    return f"{base_key}:{queue_name}" if queue_name else base_key


def send_inflight_key(queue_name: str, *, base_key: str) -> str:
    """Return a queue-scoped internal-producer counter key."""
    return f"{base_key}:{queue_name}" if queue_name else base_key


def drain_lock_ttl_from_env(logger: logging.Logger) -> int:
    """Resolve the drain lease TTL, floored above every bounded drain pass."""
    raw = os.environ.get(
        "SERVICEBUS_DRAIN_LOCK_TTL_SECONDS",
        str(MIN_DRAIN_LOCK_TTL_SECONDS),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "invalid SERVICEBUS_DRAIN_LOCK_TTL_SECONDS=%r; defaulting to %d",
            raw,
            MIN_DRAIN_LOCK_TTL_SECONDS,
        )
        value = MIN_DRAIN_LOCK_TTL_SECONDS
    if value < MIN_DRAIN_LOCK_TTL_SECONDS:
        logger.warning(
            "SERVICEBUS_DRAIN_LOCK_TTL_SECONDS=%r is below the routing-safety floor; using %d",
            raw,
            MIN_DRAIN_LOCK_TTL_SECONDS,
        )
    return max(MIN_DRAIN_LOCK_TTL_SECONDS, value)


def acquire_drain_lock(
    queue_name: str,
    *,
    enabled: bool,
    lock_ttl: int,
    lock_base_key: str,
    stop_intent_base_key: str,
    logger: logging.Logger,
    redis_factory: RedisFactory | None = None,
) -> tuple[bool, str | None]:
    """Acquire a queue-scoped drain lease, failing closed on Redis errors."""
    if not enabled:
        return (True, None)
    try:
        client = (redis_factory or _redis_factory())(socket_timeout=2)
        token = uuid.uuid4().hex
        acquired = client.eval(
            LOCK_ACQUIRE_LUA,
            2,
            drain_lock_key(queue_name, base_key=lock_base_key),
            drain_stop_intent_key(queue_name, base_key=stop_intent_base_key),
            token,
            max(MIN_DRAIN_LOCK_TTL_SECONDS, int(lock_ttl)),
        )
        return (True, token) if acquired else (False, None)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        logger.warning(
            "drain lock acquire failed; deferring queue receive reason=%s",
            type(exc).__name__,
        )
        return (False, None)


def release_drain_lock(
    token: str | None,
    queue_name: str,
    *,
    lock_base_key: str,
    logger: logging.Logger,
    redis_factory: RedisFactory | None = None,
) -> None:
    """Release a drain lease only when ``token`` still owns it."""
    if not token:
        return
    try:
        client = (redis_factory or _redis_factory())(socket_timeout=2)
        client.eval(
            LOCK_RELEASE_LUA,
            1,
            drain_lock_key(queue_name, base_key=lock_base_key),
            token,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        logger.warning(
            "drain lock release failed (will expire via TTL) reason=%s",
            type(exc).__name__,
        )


def acquire_drain_stop_intent(
    queue_name: str,
    *,
    lock_base_key: str,
    stop_intent_base_key: str,
    stop_intent_ttl: int,
    logger: logging.Logger,
    send_inflight_base_key: str = DEFAULT_SEND_INFLIGHT_BASE_KEY,
    redis_factory: RedisFactory | None = None,
) -> tuple[bool, str | None]:
    """Fence new drains/sends when neither kind of work is in flight."""
    try:
        factory = redis_factory or _redis_factory()
        client = factory(socket_timeout=2)
        token = uuid.uuid4().hex
        acquired = client.eval(
            STOP_INTENT_ACQUIRE_LUA,
            3,
            drain_lock_key(queue_name, base_key=lock_base_key),
            drain_stop_intent_key(queue_name, base_key=stop_intent_base_key),
            send_inflight_key(queue_name, base_key=send_inflight_base_key),
            token,
            max(MIN_DRAIN_LOCK_TTL_SECONDS, int(stop_intent_ttl)),
        )
        if not acquired:
            return (False, None)
        return (True, token)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.warning("drain stop-intent acquire failed; auto-stop must defer", exc_info=True)
        return (False, None)


def release_drain_stop_intent(
    queue_name: str,
    token: str | None,
    *,
    stop_intent_base_key: str,
    logger: logging.Logger,
    redis_factory: RedisFactory | None = None,
) -> None:
    """Release an auto-stop intent only when ``token`` still owns it."""
    if not token:
        return
    try:
        client = (redis_factory or _redis_factory())(socket_timeout=2)
        client.eval(
            LOCK_RELEASE_LUA,
            1,
            drain_stop_intent_key(queue_name, base_key=stop_intent_base_key),
            token,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.warning("drain stop-intent release failed (TTL backstop)", exc_info=True)


def acquire_request_send(
    queue_name: str,
    *,
    stop_intent_base_key: str = DEFAULT_STOP_INTENT_BASE_KEY,
    send_inflight_base_key: str = DEFAULT_SEND_INFLIGHT_BASE_KEY,
    send_inflight_ttl: int = DEFAULT_SEND_INFLIGHT_TTL_SECONDS,
    redis_factory: RedisFactory | None = None,
) -> tuple[bool, str | None]:
    """Enter the internal-producer set unless reconfiguration is fenced.

    Returns ``(proceed, token)``. Redis errors fail closed: allowing an
    untracked send would race Settings if Redis recovered before broker I/O
    completed and the reconfiguration fence then became acquirable.
    """
    try:
        client = (redis_factory or _redis_factory())(socket_timeout=2)
        token = uuid.uuid4().hex
        acquired = client.eval(
            SEND_ACQUIRE_LUA,
            2,
            drain_stop_intent_key(queue_name, base_key=stop_intent_base_key),
            send_inflight_key(queue_name, base_key=send_inflight_base_key),
            token,
            max(MIN_DRAIN_LOCK_TTL_SECONDS, int(send_inflight_ttl)),
        )
        return (True, token) if acquired else (False, None)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        raise RequestSendCoordinationUnavailable(
            "Service Bus request-send coordination is unavailable"
        ) from exc


def release_request_send(
    queue_name: str,
    *,
    token: str | None,
    retain_seconds: int = 0,
    send_inflight_base_key: str = DEFAULT_SEND_INFLIGHT_BASE_KEY,
    redis_factory: RedisFactory | None = None,
) -> None:
    """Leave the internal-producer set after broker send success or failure."""
    if not token:
        return
    try:
        client = (redis_factory or _redis_factory())(socket_timeout=2)
        client.eval(
            SEND_RELEASE_LUA,
            1,
            send_inflight_key(queue_name, base_key=send_inflight_base_key),
            token,
            max(0, int(retain_seconds)),
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        LOGGER.warning("Service Bus request-send token release failed", exc_info=True)
        return


def acquire_config_mutation(
    *,
    key: str = DEFAULT_CONFIG_MUTATION_KEY,
    ttl_seconds: int = DEFAULT_CONFIG_MUTATION_TTL_SECONDS,
    redis_factory: RedisFactory | None = None,
) -> tuple[bool, str | None]:
    """Serialize full-row Settings writes through one deployment-wide mutex."""
    try:
        client = (redis_factory or _redis_factory())(socket_timeout=2)
        token = uuid.uuid4().hex
        acquired = client.set(
            key,
            token,
            nx=True,
            ex=max(MIN_DRAIN_LOCK_TTL_SECONDS, int(ttl_seconds)),
        )
        return (True, token) if acquired else (False, None)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        LOGGER.warning("Service Bus config mutation lock acquire failed", exc_info=True)
        return (False, None)


def release_config_mutation(
    token: str | None,
    *,
    key: str = DEFAULT_CONFIG_MUTATION_KEY,
    redis_factory: RedisFactory | None = None,
) -> None:
    """Release the Settings mutex only when ``token`` still owns it."""
    if not token:
        return
    try:
        client = (redis_factory or _redis_factory())(socket_timeout=2)
        client.eval(LOCK_RELEASE_LUA, 1, key, token)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        LOGGER.warning("Service Bus config mutation lock release failed", exc_info=True)
        return

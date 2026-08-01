"""Redis coordination primitives for Service Bus queue draining.

Responsibility: Coordinate queue-scoped drain leases and auto-stop intent
    fences through Redis without knowing Service Bus messages or Celery tasks.
Edit boundaries: Redis key construction, bounded lease configuration, and
    acquire/release operations only. Drain admission and message handling remain
    in ``api.tasks.servicebus.tasks``.
Key entry points: ``drain_concurrency_from_env``, ``drain_lock_ttl_from_env``,
    ``acquire_drain_lock``, ``release_drain_lock``,
    ``acquire_drain_stop_intent``, ``release_drain_stop_intent``.
Risky contracts: Drain lease acquisition fails open because the atomic bridge
    claim preserves correctness; stop-intent acquisition fails closed because an
    uncoordinated stop could interrupt a PEEK_LOCKed submit. Compare-and-delete
    release must never remove a lease owned by a newer caller.
Validation: ``uv run pytest -q api/tests/test_servicebus_tasks.py
    api/tests/test_auto_stop_task.py api/tests/test_servicebus_load.py``.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from typing import Any

RedisFactory = Callable[..., Any]

LOCK_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)
LOCK_ACQUIRE_LUA = (
    "if redis.call('exists', KEYS[2]) == 1 then return 0 end "
    "if redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then return 1 end "
    "return 0"
)


def _redis_factory() -> RedisFactory:
    from api.services.redis_clients import get_broker_redis_client

    return get_broker_redis_client


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


def drain_lock_ttl_from_env(logger: logging.Logger) -> int:
    """Resolve the drain lease TTL, floored at ten seconds."""
    raw = os.environ.get("SERVICEBUS_DRAIN_LOCK_TTL_SECONDS", "900")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("invalid SERVICEBUS_DRAIN_LOCK_TTL_SECONDS=%r; defaulting to 900", raw)
        value = 900
    return max(10, value)


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
    """Acquire a queue-scoped drain lease, failing open on Redis errors."""
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
            lock_ttl,
        )
        return (True, token) if acquired else (False, None)
    except Exception:
        logger.debug("drain lock acquire failed; proceeding without lease", exc_info=True)
        return (True, None)


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
    except Exception:
        logger.debug("drain lock release failed (will expire via TTL)", exc_info=True)


def acquire_drain_stop_intent(
    queue_name: str,
    *,
    lock_base_key: str,
    stop_intent_base_key: str,
    stop_intent_ttl: int,
    logger: logging.Logger,
    redis_factory: RedisFactory | None = None,
) -> tuple[bool, str | None]:
    """Fence new drains before auto-stop checks the active drain lease."""
    try:
        factory = redis_factory or _redis_factory()
        client = factory(socket_timeout=2)
        token = uuid.uuid4().hex
        acquired = client.set(
            drain_stop_intent_key(queue_name, base_key=stop_intent_base_key),
            token,
            nx=True,
            ex=stop_intent_ttl,
        )
        if not acquired:
            return (False, None)
        if client.exists(drain_lock_key(queue_name, base_key=lock_base_key)):
            release_drain_stop_intent(
                queue_name,
                token,
                stop_intent_base_key=stop_intent_base_key,
                logger=logger,
                redis_factory=factory,
            )
            return (False, None)
        return (True, token)
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
    except Exception:
        logger.debug("drain stop-intent release failed (TTL backstop)", exc_info=True)

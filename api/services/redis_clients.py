"""Process-shared Redis client singletons for the broker and ops databases.

Responsibility: Hand out a cached ``redis.Redis`` per ``(url, kwargs)`` so
modules that talk to Redis (Celery broker for locks, OPS db for cache
invalidation, runtime config, animation events) reuse one connection pool
instead of creating a fresh one on every call. A stray
``redis.Redis.from_url(...)`` per BLAST submit / per HTTP poll exhausts FDs
within hours of steady load — this module is the single allocation site
that prevents that.
Edit boundaries: Only stdlib + ``redis``. No FastAPI / Celery / Azure SDK
imports. Callers MUST go through ``get_ops_redis_client`` /
``get_broker_redis_client`` / ``get_redis_client``; do not call
``redis.Redis.from_url`` elsewhere in ``api/``.
Key entry points: ``get_redis_client``, ``get_ops_redis_client``,
``get_broker_redis_client``, ``reset_redis_clients`` (test hook + safety
valve for graceful shutdown).
Risky contracts: The returned client is shared — callers must NOT call
``.close()`` on it. Cache key is ``(url, frozenset(kwargs.items()))`` so
two callsites that pass identical kwargs share one pool; callsites that
pass different kwargs (different socket_timeout etc.) get distinct pools.
``reset_redis_clients()`` closes every pooled client and clears the cache;
intended for pytest teardown and ``atexit`` only — never call it from a
hot path because it tears down the connection pool every other caller is
holding.
Validation: ``uv run pytest -q api/tests/test_redis_clients.py``.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import Any

LOGGER = logging.getLogger(__name__)

BROKER_URL_ENV = "CELERY_BROKER_URL"
BROKER_URL_DEFAULT = "redis://127.0.0.1:6379/0"
OPS_URL_ENV = "OPS_REDIS_URL"
OPS_URL_DEFAULT = "redis://127.0.0.1:6379/2"

_CLIENTS_LOCK = threading.Lock()
_CLIENTS: dict[tuple[str, frozenset[tuple[str, Any]]], Any] = {}


def _freeze_kwargs(kwargs: dict[str, Any]) -> frozenset[tuple[str, Any]]:
    # Only hashable scalar kwargs are expected (timeouts, bool, str). Anything
    # else is a programming error — let it raise a TypeError so we notice.
    return frozenset(kwargs.items())


def get_redis_client(url: str, **kwargs: Any) -> Any:
    """Return a cached ``redis.Redis`` for ``(url, kwargs)``.

    The first call for a given key builds a new client via
    ``redis.Redis.from_url(url, **kwargs)``; subsequent calls return the
    same instance so ``redis-py``'s internal ``ConnectionPool`` is shared.
    """
    # Local import keeps module import cheap; ``redis`` is also patched as a
    # ``sys.modules`` entry in some tests so we must look it up at call time.
    import redis

    key = (url, _freeze_kwargs(kwargs))
    with _CLIENTS_LOCK:
        cached = _CLIENTS.get(key)
        if cached is not None:
            return cached
        client = redis.Redis.from_url(url, **kwargs)
        _CLIENTS[key] = client
        return client


def get_ops_redis_client(**kwargs: Any) -> Any:
    """Return the cached client for ``OPS_REDIS_URL`` (cache invalidation, ops state)."""
    url = os.environ.get(OPS_URL_ENV, OPS_URL_DEFAULT)
    return get_redis_client(url, **kwargs)


def get_broker_redis_client(**kwargs: Any) -> Any:
    """Return the cached client for ``CELERY_BROKER_URL`` (lock primitives, queue probes)."""
    url = os.environ.get(BROKER_URL_ENV, BROKER_URL_DEFAULT)
    return get_redis_client(url, **kwargs)


def reset_redis_clients() -> None:
    """Drop every cached client; close each one outside the registry lock.

    Test hook + ``atexit`` cleanup. Production code MUST NOT call this on
    every request — the whole point of this module is to keep clients alive
    across requests.
    """
    with _CLIENTS_LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for client in clients:
        close = getattr(client, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception as exc:
            LOGGER.debug("redis client close skipped: %s", type(exc).__name__)


atexit.register(reset_redis_clients)

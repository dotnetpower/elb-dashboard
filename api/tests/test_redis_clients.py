"""Tests for the shared Redis client pool helper.

Responsibility: Verify ``api.services.redis_clients`` returns one client per
``(url, kwargs)`` so production paths (BLAST submit lock, cache invalidation,
OpenAPI runtime, queue probes) no longer leak a fresh connection pool per
call.
Edit boundaries: Test the helper's public contract only. Do not import
``redis`` real client — patch ``redis.Redis.from_url`` via ``sys.modules``
so the suite runs without a live broker.
Key entry points: ``test_*`` functions below.
Risky contracts: ``reset_redis_clients()`` must close every cached client;
otherwise pytest's autouse reset would leak across test files.
Validation: ``uv run pytest -q api/tests/test_redis_clients.py``.
"""

from __future__ import annotations

import sys
from typing import Any, ClassVar

import pytest


class _FakeRedis:
    last_built: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, url: str, kwargs: dict[str, Any]) -> None:
        self.url = url
        self.kwargs = kwargs
        self.closed = False

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> _FakeRedis:
        instance = cls(url, kwargs)
        cls.last_built.append({"url": url, "kwargs": kwargs})
        return instance

    def close(self) -> None:
        self.closed = True


class _FakeRedisModule:
    Redis = _FakeRedis


@pytest.fixture(autouse=True)
def _patch_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRedis.last_built = []
    monkeypatch.setitem(sys.modules, "redis", _FakeRedisModule)
    from api.services.redis_clients import reset_redis_clients

    reset_redis_clients()
    yield
    reset_redis_clients()


def test_get_redis_client_reuses_singleton() -> None:
    from api.services import redis_clients

    a = redis_clients.get_redis_client("redis://x/0", socket_timeout=1.0)
    b = redis_clients.get_redis_client("redis://x/0", socket_timeout=1.0)
    assert a is b
    assert len(_FakeRedis.last_built) == 1


def test_get_redis_client_distinct_per_kwargs() -> None:
    from api.services import redis_clients

    a = redis_clients.get_redis_client("redis://x/0", socket_timeout=1.0)
    b = redis_clients.get_redis_client("redis://x/0", socket_timeout=2.0)
    assert a is not b
    assert len(_FakeRedis.last_built) == 2


def test_get_redis_client_distinct_per_url() -> None:
    from api.services import redis_clients

    a = redis_clients.get_redis_client("redis://x/0")
    b = redis_clients.get_redis_client("redis://x/1")
    assert a is not b


def test_get_ops_redis_client_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import redis_clients

    monkeypatch.setenv("OPS_REDIS_URL", "redis://ops-host/2")
    client = redis_clients.get_ops_redis_client(socket_timeout=1.5)
    assert isinstance(client, _FakeRedis)
    assert client.url == "redis://ops-host/2"
    assert client.kwargs == {"socket_timeout": 1.5}


def test_get_broker_redis_client_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import redis_clients

    monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker/0")
    client = redis_clients.get_broker_redis_client()
    assert isinstance(client, _FakeRedis)
    assert client.url == "redis://broker/0"


def test_reset_redis_clients_closes_each_cached_client() -> None:
    from api.services import redis_clients

    one = redis_clients.get_redis_client("redis://x/0")
    two = redis_clients.get_redis_client("redis://y/0")
    redis_clients.reset_redis_clients()
    assert one.closed is True
    assert two.closed is True
    # After reset, next get_redis_client builds a fresh instance.
    fresh = redis_clients.get_redis_client("redis://x/0")
    assert fresh is not one


def test_acquire_submit_lock_returns_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two submit_lock acquisitions must NOT each allocate a new from_url client."""
    from api.services import redis_clients
    from api.tasks.blast import submit_lock

    # Make set() always succeed so we exercise the happy path twice.
    class _AlwaysAcquiringRedis(_FakeRedis):
        def set(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

        def eval(self, *_args: Any, **_kwargs: Any) -> int:
            return 1

    class _Module:
        Redis = _AlwaysAcquiringRedis

    monkeypatch.setitem(sys.modules, "redis", _Module)
    redis_clients.reset_redis_clients()

    first = submit_lock.acquire_submit_lock("job1", lock_key="k")
    second = submit_lock.acquire_submit_lock("job2", lock_key="k")
    assert first is not None
    assert second is not None
    assert first[0] is second[0], "submit lock must reuse the shared Redis client"
    # release must be safe (no .close() on shared client).
    submit_lock.release_submit_lock(first[0], first[1], lock_key="k")
    submit_lock.release_submit_lock(second[0], second[1], lock_key="k")
    assert first[0].closed is False

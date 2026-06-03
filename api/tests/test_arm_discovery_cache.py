"""Tests for the ARM discovery TTL cache (`api.services.arm_discovery_cache`).

Responsibility: Tests for the ARM discovery TTL cache (`api.services.arm_discovery_cache`).
Edit boundaries: Pure in-memory; no Azure SDK, no FastAPI. Resets module state per test.
Key entry points: `test_*`.
Risky contracts: The cache returns deep copies (callers must not mutate cached entries) and
its read-modify-write is lock-guarded so concurrent threadpool access cannot raise
`RuntimeError: dictionary changed size during iteration`.
Validation: `uv run pytest -q api/tests/test_arm_discovery_cache.py`.
"""

from __future__ import annotations

import threading

import pytest
from api.services import arm_discovery_cache as cache


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    cache._CACHE.clear()
    yield
    cache._CACHE.clear()


def test_miss_returns_none() -> None:
    assert cache.cached_discovery("storage", "sub", "rg") is None


def test_store_then_hit_returns_value() -> None:
    payload = [{"name": "a"}, {"name": "b"}]
    returned = cache.store_discovery("storage", "sub", "rg", payload)
    # store returns the original list unchanged.
    assert returned is payload
    hit = cache.cached_discovery("storage", "sub", "rg")
    assert hit == payload


def test_hit_is_deep_copied() -> None:
    payload = [{"name": "a"}]
    cache.store_discovery("acr", "sub", "rg", payload)
    first = cache.cached_discovery("acr", "sub", "rg")
    assert first is not None
    first[0]["name"] = "mutated"
    # A subsequent read must not see the caller's mutation.
    second = cache.cached_discovery("acr", "sub", "rg")
    assert second == [{"name": "a"}]


def test_ttl_expiry_evicts_on_read(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1_000.0}
    monkeypatch.setattr(cache.time, "monotonic", lambda: clock["now"])
    cache.store_discovery("storage", "sub", "rg", [{"name": "a"}])
    assert cache.cached_discovery("storage", "sub", "rg") is not None
    clock["now"] += cache.DISCOVERY_CACHE_TTL_SECONDS + 0.1
    assert cache.cached_discovery("storage", "sub", "rg") is None


def test_eviction_bounds_cache_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "DISCOVERY_CACHE_MAX_ENTRIES", 3)
    for i in range(5):
        cache.store_discovery("storage", "sub", f"rg{i}", [{"i": i}])
    assert len(cache._CACHE) <= 3


def test_concurrent_store_does_not_raise() -> None:
    errors: list[BaseException] = []

    def worker(start: int) -> None:
        try:
            for i in range(start, start + 200):
                cache.store_discovery("storage", "sub", f"rg{i}", [{"i": i}])
                cache.cached_discovery("storage", "sub", f"rg{i}")
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n * 200,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

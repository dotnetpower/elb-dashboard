"""Tests for monitor snapshot caching."""

from __future__ import annotations

import pytest
from api.services import monitor_cache


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch):
    monitor_cache.reset_monitor_snapshot_cache()
    monkeypatch.delenv("MONITOR_SNAPSHOT_TTL_SECONDS", raising=False)
    monkeypatch.delenv("MONITOR_SNAPSHOT_STALE_SECONDS", raising=False)
    monkeypatch.delenv("MONITOR_SNAPSHOT_CACHE_MAX_ENTRIES", raising=False)
    yield
    monitor_cache.reset_monitor_snapshot_cache()


def test_cached_snapshot_returns_fresh_hit_without_loader_call() -> None:
    calls = 0

    def loader() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"clusters": [{"name": f"aks-{calls}"}]}

    first = monitor_cache.cached_snapshot("aks:sub:rg", loader, ttl_seconds=30)
    second = monitor_cache.cached_snapshot("aks:sub:rg", loader, ttl_seconds=30)

    assert calls == 1
    assert first["clusters"] == [{"name": "aks-1"}]
    assert first["cache"]["state"] == "refreshed"
    assert second["clusters"] == [{"name": "aks-1"}]
    assert second["cache"]["state"] == "fresh"
    assert second["cache"]["hit"] is True


def test_cached_snapshot_returns_stale_while_refreshing(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    values = iter([{"nodes": ["old"]}, {"nodes": ["new"]}])

    monkeypatch.setattr(monitor_cache, "_monotonic", lambda: now)
    monkeypatch.setattr(monitor_cache, "_start_refresh_thread", lambda target: target())

    first = monitor_cache.cached_snapshot(
        "nodes:sub:rg:aks",
        lambda: next(values),
        ttl_seconds=10,
        stale_seconds=60,
    )
    assert first["nodes"] == ["old"]

    now = 111.0
    stale = monitor_cache.cached_snapshot(
        "nodes:sub:rg:aks",
        lambda: next(values),
        ttl_seconds=10,
        stale_seconds=60,
    )
    assert stale["nodes"] == ["old"]
    assert stale["cache"]["state"] == "stale"

    fresh = monitor_cache.cached_snapshot(
        "nodes:sub:rg:aks",
        lambda: {"nodes": ["unused"]},
        ttl_seconds=10,
        stale_seconds=60,
    )
    assert fresh["nodes"] == ["new"]
    assert fresh["cache"]["state"] == "fresh"


def test_cached_snapshot_cold_miss_failure_propagates() -> None:
    def loader() -> dict[str, object]:
        raise RuntimeError("arm unavailable")

    with pytest.raises(RuntimeError, match="arm unavailable"):
        monitor_cache.cached_snapshot("aks:broken", loader, ttl_seconds=30)


def test_cached_snapshot_can_be_disabled() -> None:
    calls = 0

    def loader() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"count": calls}

    first = monitor_cache.cached_snapshot("key", loader, ttl_seconds=0)
    second = monitor_cache.cached_snapshot("key", loader, ttl_seconds=0)

    assert first["count"] == 1
    assert second["count"] == 2
    assert second["cache"]["state"] == "disabled"


def test_cached_snapshot_evicts_oldest_entry_when_capacity_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setenv("MONITOR_SNAPSHOT_CACHE_MAX_ENTRIES", "2")
    monkeypatch.setattr(monitor_cache, "_monotonic", lambda: now)

    monitor_cache.cached_snapshot("k1", lambda: {"value": "one"}, ttl_seconds=30)
    now = 101.0
    monitor_cache.cached_snapshot("k2", lambda: {"value": "two"}, ttl_seconds=30)
    now = 102.0
    monitor_cache.cached_snapshot("k3", lambda: {"value": "three"}, ttl_seconds=30)

    k2 = monitor_cache.cached_snapshot("k2", lambda: {"value": "miss"}, ttl_seconds=30)
    k3 = monitor_cache.cached_snapshot("k3", lambda: {"value": "miss"}, ttl_seconds=30)
    k1 = monitor_cache.cached_snapshot("k1", lambda: {"value": "reloaded"}, ttl_seconds=30)

    assert k2["value"] == "two"
    assert k2["cache"]["state"] == "fresh"
    assert k3["value"] == "three"
    assert k3["cache"]["state"] == "fresh"
    assert k1["value"] == "reloaded"
    assert k1["cache"]["state"] == "refreshed"


def test_reset_during_refresh_prevents_stale_repopulation() -> None:
    calls = 0

    def resetting_loader() -> dict[str, object]:
        monitor_cache.reset_monitor_snapshot_cache()
        return {"value": "before-reset"}

    def after_reset_loader() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"value": "after-reset"}

    first = monitor_cache.cached_snapshot("key", resetting_loader, ttl_seconds=30)
    second = monitor_cache.cached_snapshot("key", after_reset_loader, ttl_seconds=30)

    assert first["value"] == "before-reset"
    assert second["value"] == "after-reset"
    assert second["cache"]["state"] == "refreshed"
    assert calls == 1

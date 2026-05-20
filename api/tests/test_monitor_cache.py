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


def test_invalidate_prefix_removes_only_boundary_matched_keys() -> None:
    payloads = {
        "monitor:aks:sub:rg": {"clusters": ["a"]},
        "monitor:aks:sub:rg:child": {"nodes": []},  # boundary-safe child of the prefix.
        "monitor:aks:sub:rg-elb-02": {"clusters": ["b"]},  # different rg, shares prefix.
        "monitor:aks:nodes:sub:rg:elb": {"nodes": []},  # different prefix branch.
        "monitor:storage:sub:rg": {"containers": []},
    }
    for key, value in payloads.items():
        monitor_cache.cached_snapshot(key, lambda v=value: v, ttl_seconds=30)

    removed = monitor_cache.invalidate_monitor_snapshot_prefix("monitor:aks:sub:rg")

    # Removes the exact key + its ":<child>" subkey, nothing else.
    assert removed == 2
    # The neighbour with a different RG that happens to share a string prefix MUST survive.
    survivor = monitor_cache.cached_snapshot(
        "monitor:aks:sub:rg-elb-02", lambda: {"clusters": ["miss"]}, ttl_seconds=30
    )
    assert survivor["clusters"] == ["b"]
    assert survivor["cache"]["state"] == "fresh"
    # Different prefix branch (nodes) also survives — it needs its own invalidation call.
    nodes_survivor = monitor_cache.cached_snapshot(
        "monitor:aks:nodes:sub:rg:elb", lambda: {"nodes": ["miss"]}, ttl_seconds=30
    )
    assert nodes_survivor["cache"]["state"] == "fresh"


def test_invalidate_prefix_cancels_inflight_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """A background refresh that completes AFTER invalidation must not repopulate."""
    now = 1000.0
    monkeypatch.setattr(monitor_cache, "_monotonic", lambda: now)

    # Capture refresh callables so we control when they run.
    refresh_callables: list = []
    monkeypatch.setattr(
        monitor_cache,
        "_start_refresh_thread",
        lambda target: refresh_callables.append(target),
    )

    # Seed cache with v1, then go stale so the next read triggers a background refresh.
    monitor_cache.cached_snapshot(
        "monitor:aks:sub:rg",
        lambda: {"clusters": ["v1"]},
        ttl_seconds=10,
        stale_seconds=300,
    )
    now = 1015.0  # past TTL, still within stale window.
    stale = monitor_cache.cached_snapshot(
        "monitor:aks:sub:rg",
        lambda: {"clusters": ["v2-from-bg"]},
        ttl_seconds=10,
        stale_seconds=300,
    )
    assert stale["cache"]["state"] == "stale"
    assert refresh_callables, "stale read should have queued a background refresh"

    # User mutates ARM (e.g. clicks Start); we invalidate before the background refresh runs.
    monitor_cache.invalidate_monitor_snapshot_prefix("monitor:aks:sub:rg")

    # Now the in-flight refresh resolves with what was the live ARM reading at queue time.
    # Because invalidation bumped _GENERATION, it must NOT repopulate the cache.
    refresh_callables[0]()

    after = monitor_cache.cached_snapshot(
        "monitor:aks:sub:rg",
        lambda: {"clusters": ["v3-fresh"]},
        ttl_seconds=10,
        stale_seconds=300,
    )
    assert after["clusters"] == ["v3-fresh"]
    assert after["cache"]["state"] == "refreshed"


def test_invalidate_prefix_no_match_is_noop() -> None:
    monitor_cache.cached_snapshot("monitor:aks:sub:rg", lambda: {"a": 1}, ttl_seconds=30)
    assert monitor_cache.invalidate_monitor_snapshot_prefix("monitor:storage:sub:rg") == 0
    # Existing entry must still be fresh.
    hit = monitor_cache.cached_snapshot(
        "monitor:aks:sub:rg", lambda: {"a": 2}, ttl_seconds=30
    )
    assert hit["cache"]["state"] == "fresh"
    assert hit["a"] == 1


def test_invalidate_prefix_empty_is_noop() -> None:
    monitor_cache.cached_snapshot("monitor:aks:sub:rg", lambda: {"a": 1}, ttl_seconds=30)
    assert monitor_cache.invalidate_monitor_snapshot_prefix("") == 0
    hit = monitor_cache.cached_snapshot(
        "monitor:aks:sub:rg", lambda: {"a": 2}, ttl_seconds=30
    )
    assert hit["a"] == 1

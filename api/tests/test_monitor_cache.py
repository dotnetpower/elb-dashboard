"""Tests for monitor snapshot caching.

Responsibility: Tests for monitor snapshot caching
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_reset_cache`, `test_cached_snapshot_returns_fresh_hit_without_loader_call`,
`test_cached_snapshot_returns_stale_while_refreshing`,
`test_cached_snapshot_cold_miss_failure_propagates`, `test_cached_snapshot_can_be_disabled`,
`test_cached_snapshot_evicts_oldest_entry_when_capacity_is_reached`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_monitor_cache.py`.
"""

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
        monitor_cache.cached_snapshot(key, lambda v=value: v, ttl_seconds=30)  # type: ignore[misc]

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


def test_cross_key_invalidation_does_not_stick_unrelated_refreshing_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated-key invalidation must not permanently block key Y's bg refresh.

    ``_GENERATION`` is global but ``invalidate_monitor_snapshot_prefix`` only
    removes the matched keys. So when key X is invalidated while key Y has a
    background refresh in flight, Y's refresh sees the bumped generation and is
    discarded — but Y's entry stays in the cache. The stuck ``refreshing`` flag
    must be cleared so the NEXT poll of Y can re-trigger a background refresh
    instead of serving stale until the stale window expires.
    """
    now = 1000.0
    monkeypatch.setattr(monitor_cache, "_monotonic", lambda: now)

    refresh_callables: list = []
    monkeypatch.setattr(
        monitor_cache,
        "_start_refresh_thread",
        lambda target: refresh_callables.append(target),
    )

    key_y = "monitor:aks:sub:rg-Y"
    # Seed Y, then go stale so the next read queues a background refresh.
    monitor_cache.cached_snapshot(
        key_y, lambda: {"clusters": ["y1"]}, ttl_seconds=10, stale_seconds=300
    )
    now = 1015.0  # past TTL, within stale window.
    stale = monitor_cache.cached_snapshot(
        key_y, lambda: {"clusters": ["y2-bg"]}, ttl_seconds=10, stale_seconds=300
    )
    assert stale["cache"]["state"] == "stale"
    assert refresh_callables, "stale read should have queued a background refresh"
    # The entry is now flagged refreshing=True under the hood.
    assert monitor_cache._CACHE[key_y].refreshing is True

    # Invalidate an UNRELATED key X — this bumps the global generation but leaves
    # Y's entry in the cache.
    monitor_cache.cached_snapshot(
        "monitor:aks:sub:rg-X", lambda: {"clusters": ["x1"]}, ttl_seconds=10
    )
    assert monitor_cache.invalidate_monitor_snapshot_prefix("monitor:aks:sub:rg-X") == 1

    # Y's in-flight background refresh now resolves against the bumped generation.
    # It must be discarded (generation mismatch) BUT must clear Y's stuck flag.
    refresh_callables[0]()
    assert monitor_cache._CACHE[key_y].refreshing is False, (
        "cross-key invalidation left Y's refreshing flag stuck -> bg refresh blocked"
    )

    # Proof of liveness: the next stale poll of Y re-triggers a background refresh
    # (a second callable is queued) instead of being blocked by the stuck flag.
    refresh_callables.clear()
    again = monitor_cache.cached_snapshot(
        key_y, lambda: {"clusters": ["y3-bg"]}, ttl_seconds=10, stale_seconds=300
    )
    assert again["cache"]["state"] == "stale"
    assert refresh_callables, "Y should re-queue a background refresh after the flag was cleared"


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


def test_force_bypasses_fresh_hit_and_requeries() -> None:
    """``force=True`` must re-run the loader even within the TTL window.

    This is the cross-process-safe path the SPA uses while a cluster start/stop
    is in flight: it cannot rely on the worker invalidating the api process's
    cache, so it asks the api to re-query ARM directly.
    """
    calls = 0

    def loader() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"clusters": [{"provisioning_state": "Starting" if calls == 1 else "Succeeded"}]}

    first = monitor_cache.cached_snapshot("monitor:aks:sub:rg", loader, ttl_seconds=30)
    assert first["clusters"][0]["provisioning_state"] == "Starting"
    assert first["cache"]["state"] == "refreshed"

    # A normal read within TTL would serve the cached "Starting" reading.
    cached = monitor_cache.cached_snapshot("monitor:aks:sub:rg", loader, ttl_seconds=30)
    assert cached["cache"]["state"] == "fresh"
    assert cached["clusters"][0]["provisioning_state"] == "Starting"
    assert calls == 1

    # force=True bypasses the fresh hit, re-queries, and stores the new reading.
    forced = monitor_cache.cached_snapshot(
        "monitor:aks:sub:rg", loader, ttl_seconds=30, force=True
    )
    assert forced["cache"]["state"] == "refreshed"
    assert forced["clusters"][0]["provisioning_state"] == "Succeeded"
    assert calls == 2

    # The forced refresh updated the cache, so the next normal read is fresh.
    after = monitor_cache.cached_snapshot("monitor:aks:sub:rg", loader, ttl_seconds=30)
    assert after["cache"]["state"] == "fresh"
    assert after["clusters"][0]["provisioning_state"] == "Succeeded"
    assert calls == 2


def test_force_bypasses_stale_hit_synchronously(monkeypatch: pytest.MonkeyPatch) -> None:
    """``force=True`` must not return a stale entry or defer to a bg refresh."""
    now = 1000.0
    monkeypatch.setattr(monitor_cache, "_monotonic", lambda: now)
    bg_refreshes: list = []
    monkeypatch.setattr(
        monitor_cache, "_start_refresh_thread", lambda target: bg_refreshes.append(target)
    )

    monitor_cache.cached_snapshot(
        "monitor:aks:sub:rg", lambda: {"v": "old"}, ttl_seconds=10, stale_seconds=300
    )
    now = 1015.0  # past TTL, within stale window.

    forced = monitor_cache.cached_snapshot(
        "monitor:aks:sub:rg",
        lambda: {"v": "new"},
        ttl_seconds=10,
        stale_seconds=300,
        force=True,
    )
    assert forced["v"] == "new"
    assert forced["cache"]["state"] == "refreshed"
    # force must refresh synchronously, never queue a background refresh.
    assert bg_refreshes == []


# ---------------------------------------------------------------------------
# Transient-failure telemetry suppression (App Insights noise reduction).
# Anchors:
#   - api/services/monitor_cache.py::_is_transient_refresh_failure
#   - api/services/monitor_cache.py::_should_suppress_transient_telemetry
# ---------------------------------------------------------------------------


def _make_requests_connection_error() -> Exception:
    """Build the same exception class the kubernetes client raises when DNS
    fails (`requests.exceptions.ConnectionError(NameResolutionError(...))`).
    """
    from requests.exceptions import ConnectionError as RequestsConnectionError

    return RequestsConnectionError(
        "HTTPSConnectionPool(host='cluster.example', port=443): "
        "Failed to resolve 'cluster.example' ([Errno -2] Name or service not known)"
    )


def _make_requests_connect_timeout() -> Exception:
    from requests.exceptions import ConnectTimeout

    return ConnectTimeout("connect timeout=10")


def _make_arm_404() -> Exception:
    from azure.core.exceptions import ResourceNotFoundError

    return ResourceNotFoundError("cluster not found")


def test_is_transient_refresh_failure_classifies_known_families() -> None:
    assert monitor_cache._is_transient_refresh_failure(_make_requests_connection_error()) is True
    assert monitor_cache._is_transient_refresh_failure(_make_requests_connect_timeout()) is True
    assert monitor_cache._is_transient_refresh_failure(_make_arm_404()) is True
    # Unknown / programmer errors must remain "non-transient" so a real bug
    # still produces a full stack trace + App Insights exception row.
    assert monitor_cache._is_transient_refresh_failure(RuntimeError("boom")) is False
    assert monitor_cache._is_transient_refresh_failure(ValueError("bad")) is False


def test_transient_refresh_with_stale_entry_logs_one_line_after_first(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """First failure inside the dedup window emits exc_info=True; the next
    failure for the same (cache_key, exc class) only logs a one-liner so the
    OTel logging exporter doesn't record a fresh AppInsights exception row.
    """
    monitor_cache._reset_transient_dedup()
    now = 5000.0
    monkeypatch.setattr(monitor_cache, "_monotonic", lambda: now)

    def _inline_runner(target):
        # Mirror the production wrapper in `_start_refresh_thread.run`: swallow
        # the refresh exception so the caller does not see it bubble up. The
        # log records (which is what the test asserts on) are still emitted.
        try:
            target()
        except Exception:  # noqa: S110 - mirror prod _start_refresh_thread wrapper
            pass

    monkeypatch.setattr(monitor_cache, "_start_refresh_thread", _inline_runner)

    # Seed a stale entry so the refresh path takes the "transient + stale" branch.
    monitor_cache.cached_snapshot(
        "monitor:aks:top-nodes:sub:rg:stopped",
        lambda: {"nodes": ["seed"]},
        ttl_seconds=10,
        stale_seconds=300,
    )
    now = 5020.0  # past TTL, still within stale window.

    failing_loader_calls = 0

    def failing_loader() -> dict[str, object]:
        nonlocal failing_loader_calls
        failing_loader_calls += 1
        raise _make_requests_connect_timeout()

    caplog.set_level("WARNING", logger="api.services.monitor_cache")

    # First read returns the stale entry AND queues a background refresh
    # (which we run inline via the monkeypatched _start_refresh_thread).
    first = monitor_cache.cached_snapshot(
        "monitor:aks:top-nodes:sub:rg:stopped",
        failing_loader,
        ttl_seconds=10,
        stale_seconds=300,
    )
    assert first["nodes"] == ["seed"]  # stale fallback preserved
    assert first["cache"]["state"] == "stale"
    # The refresh exception was caught by _start_refresh_thread.run() — we
    # only care about the log records here.
    assert failing_loader_calls == 1
    first_record = [r for r in caplog.records if "refresh failed" in r.getMessage()][-1]
    assert first_record.exc_info is not None  # full stack on first occurrence

    caplog.clear()

    # Second poll tick still inside the dedup window: same cache_key, same
    # exception class -> stack-trace suppressed, one-line warning only.
    now = 5025.0
    # Make it stale again (the previous failed refresh did not write anything).
    # Trigger a fresh background refresh:
    second = monitor_cache.cached_snapshot(
        "monitor:aks:top-nodes:sub:rg:stopped",
        failing_loader,
        ttl_seconds=10,
        stale_seconds=300,
    )
    assert second["cache"]["state"] == "stale"
    assert failing_loader_calls == 2
    second_record = [r for r in caplog.records if "refresh failed" in r.getMessage()][-1]
    assert second_record.exc_info is None  # suppressed
    assert "deduped" in second_record.getMessage()


def test_transient_refresh_without_stale_entry_keeps_full_stack(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cold-miss failures (no stale entry to fall back on) MUST keep
    exc_info=True so an unhealthy first-ever fetch is fully observable.
    """
    monitor_cache._reset_transient_dedup()

    def _inline_runner(target):
        try:
            target()
        except Exception:  # noqa: S110 - mirror prod _start_refresh_thread wrapper
            pass

    monkeypatch.setattr(monitor_cache, "_start_refresh_thread", _inline_runner)
    caplog.set_level("WARNING", logger="api.services.monitor_cache")

    def failing_loader() -> dict[str, object]:
        raise _make_requests_connection_error()

    from requests.exceptions import ConnectionError as _RC

    with pytest.raises(_RC):
        monitor_cache.cached_snapshot(
            "monitor:aks:nodes:sub:rg:firstever",
            failing_loader,
            ttl_seconds=10,
            stale_seconds=300,
        )

    rec = [r for r in caplog.records if "refresh failed" in r.getMessage()][-1]
    assert rec.exc_info is not None


def test_non_transient_refresh_failure_always_keeps_full_stack(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuine code bug (RuntimeError / ValueError / ...) is never deduped —
    even if a stale entry exists.
    """
    monitor_cache._reset_transient_dedup()
    now = 7000.0
    monkeypatch.setattr(monitor_cache, "_monotonic", lambda: now)

    def _inline_runner(target):
        try:
            target()
        except Exception:  # noqa: S110 - mirror prod _start_refresh_thread wrapper
            pass

    monkeypatch.setattr(monitor_cache, "_start_refresh_thread", _inline_runner)
    caplog.set_level("WARNING", logger="api.services.monitor_cache")

    monitor_cache.cached_snapshot(
        "monitor:aks:top-nodes:sub:rg:bug",
        lambda: {"nodes": ["seed"]},
        ttl_seconds=10,
        stale_seconds=300,
    )
    now = 7020.0

    def buggy_loader() -> dict[str, object]:
        raise RuntimeError("programmer error")

    # Two consecutive failures — both must keep the full stack.
    for _ in range(2):
        monitor_cache.cached_snapshot(
            "monitor:aks:top-nodes:sub:rg:bug",
            buggy_loader,
            ttl_seconds=10,
            stale_seconds=300,
        )
        now += 5.0

    records = [r for r in caplog.records if "refresh failed" in r.getMessage()]
    assert len(records) >= 2
    for rec in records:
        assert rec.exc_info is not None, "non-transient failures must keep stack"


def test_refresh_failure_increments_otel_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every refresh failure (transient OR programmer error) must increment
    the OpenTelemetry counter so operators can alert on real env-wide
    outages independently of the AppInsights exception stream we now dedup.
    """
    monitor_cache._reset_transient_dedup()
    monitor_cache._reset_refresh_failure_counter()

    recorded: list[tuple[int, dict[str, object]]] = []

    class _RecordingCounter:
        def add(self, value: int, attributes: dict[str, object] | None = None) -> None:
            recorded.append((value, dict(attributes or {})))

    monkeypatch.setattr(
        monitor_cache, "_get_refresh_failure_counter", lambda: _RecordingCounter()
    )

    def _inline_runner(target):
        try:
            target()
        except Exception:  # noqa: S110 - mirror prod _start_refresh_thread wrapper
            pass

    monkeypatch.setattr(monitor_cache, "_start_refresh_thread", _inline_runner)

    # Cold-miss failure (no stale fallback) — transient classification still
    # records the counter.
    def failing_cold() -> dict[str, object]:
        raise _make_requests_connect_timeout()

    from requests.exceptions import ConnectTimeout as _CT

    with pytest.raises(_CT):
        monitor_cache.cached_snapshot(
            "monitor:aks:cold-miss",
            failing_cold,
            ttl_seconds=10,
            stale_seconds=300,
        )

    assert recorded, "counter must increment on refresh failure"
    value, attrs = recorded[-1]
    assert value == 1
    assert attrs["exception_class"] == "ConnectTimeout"
    assert attrs["stale_fallback"] is False
    assert attrs["transient"] is False  # transient classification needs stale fallback

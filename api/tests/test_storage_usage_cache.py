"""Tests for cached Storage usage snapshots.

Responsibility: Tests for Storage usage cache behavior
Edit boundaries: Keep assertions focused on cache state transitions; use fake loaders only.
Key entry points: `test_cached_usage_returns_pending_on_cold_miss`,
`test_cached_usage_returns_stale_while_refreshing`
Risky contracts: Cold cache misses must not block dashboard responses on blob enumeration.
Validation: `uv run pytest -q api/tests/test_storage_usage_cache.py`.
"""

from __future__ import annotations

import pytest
from api.services.storage import usage_cache as storage_usage_cache


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch):
    storage_usage_cache.reset_storage_usage_cache()
    monkeypatch.delenv("STORAGE_USAGE_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.delenv("STORAGE_USAGE_CACHE_STALE_SECONDS", raising=False)
    monkeypatch.delenv("STORAGE_USAGE_CACHE_MAX_ENTRIES", raising=False)
    yield
    storage_usage_cache.reset_storage_usage_cache()


def test_cached_usage_returns_pending_on_cold_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_targets: list[object] = []
    loader_calls = 0

    def fake_loader(*_args: object, **_kwargs: object) -> dict[str, dict[str, object]]:
        nonlocal loader_calls
        loader_calls += 1
        return {
            "queries": {
                "blob_count": 2,
                "size_bytes": 128,
                "usage_error": None,
                "usage_truncated": False,
            }
        }

    monkeypatch.setattr(storage_usage_cache, "_load_container_usage", fake_loader)
    monkeypatch.setattr(
        storage_usage_cache,
        "_start_refresh_thread",
        lambda target: refresh_targets.append(target),
    )

    first = storage_usage_cache.cached_container_usage_summaries(object(), "elbstg01", ["queries"])

    assert first.state == "pending"
    assert first.pending is True
    assert first.summaries["queries"]["size_bytes"] is None
    assert loader_calls == 0
    assert len(refresh_targets) == 1

    refresh_targets[0]()  # type: ignore[operator]
    second = storage_usage_cache.cached_container_usage_summaries(object(), "elbstg01", ["queries"])

    assert second.state == "fresh"
    assert second.pending is False
    assert second.summaries["queries"]["size_bytes"] == 128
    assert loader_calls == 1


def test_cached_usage_returns_stale_while_refreshing(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    refresh_targets: list[object] = []
    values = iter(
        [
            {
                "queries": {
                    "blob_count": 1,
                    "size_bytes": 10,
                    "usage_error": None,
                    "usage_truncated": False,
                }
            },
            {
                "queries": {
                    "blob_count": 2,
                    "size_bytes": 30,
                    "usage_error": None,
                    "usage_truncated": False,
                }
            },
        ]
    )

    monkeypatch.setattr(storage_usage_cache, "_monotonic", lambda: now)
    monkeypatch.setattr(storage_usage_cache, "_wall_time", lambda: 1_800_000_000.0 + now)
    monkeypatch.setattr(
        storage_usage_cache, "_load_container_usage", lambda *_args, **_kwargs: next(values)
    )
    monkeypatch.setattr(storage_usage_cache, "_start_refresh_thread", lambda target: target())

    first = storage_usage_cache.cached_container_usage_summaries(object(), "elbstg01", ["queries"])
    assert first.state == "fresh"
    assert first.summaries["queries"]["size_bytes"] == 10

    monkeypatch.setattr(
        storage_usage_cache,
        "_start_refresh_thread",
        lambda target: refresh_targets.append(target),
    )
    now = 410.0
    stale = storage_usage_cache.cached_container_usage_summaries(object(), "elbstg01", ["queries"])

    assert stale.state == "stale"
    assert stale.summaries["queries"]["size_bytes"] == 10
    assert len(refresh_targets) == 1

    refresh_targets[0]()  # type: ignore[operator]
    fresh = storage_usage_cache.cached_container_usage_summaries(object(), "elbstg01", ["queries"])
    assert fresh.state == "fresh"
    assert fresh.summaries["queries"]["size_bytes"] == 30


def test_cached_usage_turns_refresh_failure_into_per_container_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_loader(*_args: object, **_kwargs: object) -> dict[str, dict[str, object]]:
        raise RuntimeError("storage blocked")

    monkeypatch.setattr(storage_usage_cache, "_load_container_usage", fake_loader)
    monkeypatch.setattr(storage_usage_cache, "_start_refresh_thread", lambda target: target())

    result = storage_usage_cache.cached_container_usage_summaries(
        object(), "elbstg01", ["queries", "results"]
    )

    assert result.state == "fresh"
    assert result.pending is False
    assert result.summaries["queries"]["usage_error"] == "RuntimeError"
    assert result.summaries["results"]["usage_error"] == "RuntimeError"

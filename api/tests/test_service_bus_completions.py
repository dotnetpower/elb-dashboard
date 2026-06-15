"""Tests for the Redis-backed completion observation ring.

Responsibility: Verify ``record_completion`` / ``list_recent`` round-trip a
    compact projection, cap the ring, degrade to empty when Redis is
    unavailable, and never raise.
Edit boundaries: Uses a fake Redis client injected via ``get_ops_redis_client``.
Key entry points: ``service_bus_completions``.
Risky contracts: Best-effort — a Redis outage yields an empty list, never an
    exception into the consumer/route.
Validation: ``uv run pytest -q api/tests/test_service_bus_completions.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


class _FakePipeline:
    def __init__(self, store: dict[str, list[str]]) -> None:
        self._store = store
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    def lpush(self, key: str, value: str) -> _FakePipeline:
        self._ops.append(("lpush", (key, value)))
        return self

    def ltrim(self, key: str, start: int, end: int) -> _FakePipeline:
        self._ops.append(("ltrim", (key, start, end)))
        return self

    def expire(self, key: str, ttl: int) -> _FakePipeline:
        self._ops.append(("expire", (key, ttl)))
        return self

    def execute(self) -> None:
        for op, args in self._ops:
            if op == "lpush":
                key, value = args
                self._store.setdefault(key, []).insert(0, value)
            elif op == "ltrim":
                key, start, end = args
                self._store[key] = self._store.get(key, [])[start : end + 1]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, list[str]] = {}

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self.store)

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self.store.get(key, [])[start : end + 1]

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda **_k: fake
    )
    return fake


def _event(corr: str, status: str = "succeeded") -> dict[str, Any]:
    return {
        "event_id": f"id-{corr}",
        "external_correlation_id": corr,
        "openapi_job_id": f"job-{corr}",
        "status": status,
        "ts": "2026-06-15T00:00:00Z",
    }


def test_record_and_list_round_trip(fake_redis: _FakeRedis) -> None:
    from api.services.service_bus_completions import list_recent, record_completion

    record_completion(_event("a"))
    record_completion(_event("b"))
    events = list_recent(10)
    assert [e["external_correlation_id"] for e in events] == ["b", "a"]  # newest-first
    assert events[0]["status"] == "succeeded"
    assert "observed_at" in events[0]


def test_record_preserves_request_id(fake_redis: _FakeRedis) -> None:
    """A request_id on the observed completion event round-trips through the
    observer store (end-to-end pass-through visible to the Playground)."""
    from api.services.service_bus_completions import list_recent, record_completion

    event = _event("c")
    event["request_id"] = "req-roundtrip-5"
    record_completion(event)
    events = list_recent(10)
    assert events[0]["request_id"] == "req-roundtrip-5"
    # An event without request_id stores an empty string (no KeyError downstream).
    record_completion(_event("d"))
    assert list_recent(10)[0]["request_id"] == ""


def test_ring_is_capped(fake_redis: _FakeRedis) -> None:
    from api.services import service_bus_completions as obs

    for i in range(obs._MAX_ENTRIES + 25):
        obs.record_completion(_event(str(i)))
    stored = fake_redis.store[obs._KEY]
    assert len(stored) == obs._MAX_ENTRIES


def test_degrades_when_redis_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import service_bus_completions as obs

    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("no redis")),
    )
    # No raise, empty result.
    obs.record_completion(_event("x"))
    assert obs.list_recent(5) == []


def test_malformed_entry_skipped(fake_redis: _FakeRedis) -> None:
    from api.services import service_bus_completions as obs

    fake_redis.store[obs._KEY] = ["not-json", json.dumps({"status": "ok"})]
    events = obs.list_recent(10)
    assert events == [{"status": "ok"}]


def test_duplicate_event_ids_deduped(fake_redis: _FakeRedis) -> None:
    """At-least-once redelivery can store the same event twice — list_recent
    de-dups by event_id so the UI never sees a duplicate (React key collision)."""
    from api.services.service_bus_completions import list_recent, record_completion

    record_completion(_event("a"))
    record_completion(_event("a"))  # same event_id "id-a"
    record_completion(_event("b"))
    events = list_recent(10)
    assert [e["event_id"] for e in events] == ["id-b", "id-a"]


def test_entries_without_event_id_kept(fake_redis: _FakeRedis) -> None:
    """Entries lacking an event_id carry no dedup key and are all kept."""
    from api.services import service_bus_completions as obs

    obs.record_completion({"status": "running"})
    obs.record_completion({"status": "running"})
    assert len(obs.list_recent(10)) == 2


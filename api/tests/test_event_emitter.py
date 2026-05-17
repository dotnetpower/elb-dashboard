"""Tests for the cross-sidecar UI animation event emitter."""

from __future__ import annotations

import importlib

import api.services.event_emitter as em
import api.services.sidecar_metrics as sm
import pytest


class _FakePipeline:
    def __init__(self, store: dict):
        self._store = store
        self._ops: list[tuple[str, tuple]] = []

    def hgetall(self, key):
        self._ops.append(("hgetall", (key,)))
        return self

    def delete(self, key):
        self._ops.append(("delete", (key,)))
        return self

    def execute(self):
        results = []
        for name, args in self._ops:
            if name == "hgetall":
                key = args[0]
                # mimic redis-py: bytes keys/values
                results.append(
                    {k.encode(): str(v).encode() for k, v in self._store.get(key, {}).items()}
                )
            elif name == "delete":
                key = args[0]
                self._store.pop(key, None)
                results.append(1)
        self._ops.clear()
        return results


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}

    def hincrby(self, key, field, amount=1):
        bucket = self.store.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]

    def pipeline(self):
        return _FakePipeline(self.store)


@pytest.fixture(autouse=True)
def _reload_emitter():
    importlib.reload(em)
    yield
    em.reset_for_tests()


def test_emit_increments_hash_field():
    fake = _FakeRedis()
    em._client = fake  # bypass redis.from_url
    em.emit(em.ROW_HTTP)
    em.emit(em.ROW_HTTP, count=2)
    em.emit(em.ROW_ASYNC)
    assert fake.store[em.EVENTS_HASH] == {em.ROW_HTTP: 3, em.ROW_ASYNC: 1}


def test_invalid_tuning_env_values_fall_back(monkeypatch):
    monkeypatch.setenv("EVENT_EMIT_CONNECT_TIMEOUT_SECONDS", "bad")
    monkeypatch.setenv("EVENT_EMIT_SOCKET_TIMEOUT_SECONDS", "bad")
    monkeypatch.setenv("EVENT_EMIT_FAILURE_COOLDOWN_SECONDS", "bad")
    monkeypatch.setenv("EVENT_EMIT_MAX_COUNT", "bad")
    try:
        importlib.reload(em)
        assert em._CONNECT_TIMEOUT_SECONDS == 0.05
        assert em._SOCKET_TIMEOUT_SECONDS == 0.05
        assert em._FAILURE_COOLDOWN_SECONDS == 5.0
        assert em._MAX_COUNT == 1000
    finally:
        monkeypatch.delenv("EVENT_EMIT_CONNECT_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("EVENT_EMIT_SOCKET_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("EVENT_EMIT_FAILURE_COOLDOWN_SECONDS", raising=False)
        monkeypatch.delenv("EVENT_EMIT_MAX_COUNT", raising=False)
        importlib.reload(em)


def test_emit_clamps_large_counts(monkeypatch):
    fake = _FakeRedis()
    em._client = fake
    monkeypatch.setattr(em, "_MAX_COUNT", 3)
    em.emit(em.ROW_HTTP, count=999)
    assert fake.store[em.EVENTS_HASH] == {em.ROW_HTTP: 3}


def test_emit_swallows_unknown_row():
    fake = _FakeRedis()
    em._client = fake
    em.emit("rowZ")
    em.emit(em.ROW_TERM, count=0)
    em.emit(em.ROW_TERM, count=-5)
    assert em.EVENTS_HASH not in fake.store


def test_drain_returns_zero_when_empty():
    fake = _FakeRedis()
    em._client = fake
    out = em.drain()
    assert out == {"row1": 0, "row2": 0, "row3": 0, "row4": 0}


def test_drain_resets_hash_and_returns_counts():
    fake = _FakeRedis()
    em._client = fake
    em.emit(em.ROW_HTTP, 4)
    em.emit(em.ROW_SCHED, 2)
    out = em.drain()
    assert out == {"row1": 4, "row2": 0, "row3": 2, "row4": 0}
    # second drain — counters were atomically deleted
    out2 = em.drain()
    assert out2 == {"row1": 0, "row2": 0, "row3": 0, "row4": 0}


def test_drain_ignores_unknown_fields():
    fake = _FakeRedis()
    em._client = fake
    fake.store[em.EVENTS_HASH] = {"rowX": 99, em.ROW_HTTP: 1}
    out = em.drain()
    assert out == {"row1": 1, "row2": 0, "row3": 0, "row4": 0}


def test_drain_clamps_bad_counter_values(monkeypatch):
    fake = _FakeRedis()
    em._client = fake
    monkeypatch.setattr(em, "_MAX_COUNT", 5)
    fake.store[em.EVENTS_HASH] = {em.ROW_HTTP: 99, em.ROW_ASYNC: -2}
    out = em.drain()
    assert out == {"row1": 5, "row2": 0, "row3": 0, "row4": 0}


def test_drain_zero_on_redis_error():
    class _Boom:
        def pipeline(self):
            class _P:
                def hgetall(self, *_):
                    return self

                def delete(self, *_):
                    return self

                def execute(self_inner):
                    import redis as _r

                    raise _r.RedisError("nope")

            return _P()

    em._client = _Boom()
    out = em.drain()
    assert out == {"row1": 0, "row2": 0, "row3": 0, "row4": 0}


def test_emit_opens_cooldown_after_redis_error(monkeypatch):
    class _Boom:
        def hincrby(self, *_args, **_kwargs):
            import redis as _r

            raise _r.RedisError("down")

    fake = _FakeRedis()
    now = 100.0
    monkeypatch.setattr(em.time, "monotonic", lambda: now)
    monkeypatch.setattr(em, "_FAILURE_COOLDOWN_SECONDS", 10.0)

    em._client = _Boom()
    em.emit(em.ROW_HTTP)
    assert em._disabled_until == 110.0

    em._client = fake
    em.emit(em.ROW_HTTP)
    assert em.EVENTS_HASH not in fake.store

    now = 111.0
    em.emit(em.ROW_HTTP)
    assert fake.store[em.EVENTS_HASH] == {em.ROW_HTTP: 1}


def test_drain_failure_opens_cooldown(monkeypatch):
    """drain() must arm the same circuit breaker as emit() — otherwise the
    next snapshot tick re-pays the timeout while Redis is still down.
    """

    class _Boom:
        def pipeline(self):
            class _P:
                def hgetall(self, *_):
                    return self

                def delete(self, *_):
                    return self

                def execute(self_inner):
                    import redis as _r

                    raise _r.RedisError("nope")

            return _P()

    now = 200.0
    monkeypatch.setattr(em.time, "monotonic", lambda: now)
    monkeypatch.setattr(em, "_FAILURE_COOLDOWN_SECONDS", 7.0)

    em._client = _Boom()
    out = em.drain()
    assert out == {"row1": 0, "row2": 0, "row3": 0, "row4": 0}
    assert em._disabled_until == 207.0


def test_collect_snapshot_includes_events(monkeypatch):
    """The snapshot builder drains counters atomically into the payload."""
    fake = _FakeRedis()
    em._client = fake

    em.emit(em.ROW_HTTP, 3)
    em.emit(em.ROW_TERM, 1)

    # Stub out the reporter MGET + redis self-info paths so we exercise
    # only the events-drain glue code.
    def _no_reporters(_client):
        return ()

    def _empty_redis_self(_client, _now, **_kw):
        return {
            "name": "redis",
            "health": "ok",
            "ts": _now,
            "cpu_pct": 0.0,
            "mem_bytes": 0,
            "mem_max_bytes": None,
            "mem_pct": None,
        }

    monkeypatch.setattr(sm, "_mget_reporters", _no_reporters)
    monkeypatch.setattr(sm, "_redis_self_snapshot", _empty_redis_self)

    snap = sm.collect_snapshot(client=fake)
    assert snap["events"] == {"row1": 3, "row2": 0, "row3": 0, "row4": 1}
    # second call after drain — events go to zero
    snap2 = sm.collect_snapshot(client=fake)
    assert snap2["events"] == {"row1": 0, "row2": 0, "row3": 0, "row4": 0}


def test_collect_snapshot_skips_drain_when_disabled(monkeypatch):
    """drain_events=False must return all-zero events without touching
    the Redis hash, so HTTP poll callers can't steal a tick from the SSE
    stream that is the canonical drainer.
    """
    fake = _FakeRedis()
    em._client = fake

    em.emit(em.ROW_HTTP, 5)
    em.emit(em.ROW_ASYNC, 2)

    def _no_reporters(_client):
        return ()

    def _empty_redis_self(_client, _now, **_kw):
        return {
            "name": "redis",
            "health": "ok",
            "ts": _now,
            "cpu_pct": 0.0,
            "mem_bytes": 0,
            "mem_max_bytes": None,
            "mem_pct": None,
        }

    monkeypatch.setattr(sm, "_mget_reporters", _no_reporters)
    monkeypatch.setattr(sm, "_redis_self_snapshot", _empty_redis_self)

    snap = sm.collect_snapshot(client=fake, drain_events=False)
    # All-zero payload …
    assert snap["events"] == {"row1": 0, "row2": 0, "row3": 0, "row4": 0}
    # … and the underlying hash is intact for the SSE consumer to pick up.
    assert fake.store[em.EVENTS_HASH] == {em.ROW_HTTP: 5, em.ROW_ASYNC: 2}

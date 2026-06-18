"""Tests for the jobs / message-flow cross-sidecar cache invalidation signal.

Responsibility: Verify the local cache-drop trio fires all three resets, the
    best-effort publish is gated by ``JOBS_CACHE_INVALIDATE_DISABLED`` and writes
    to the configured channel, ``notify_jobs_cache_changed`` does both, the
    subscriber is a no-op when disabled, and a published message drives a local
    invalidate through a fake Redis pub/sub.
Edit boundaries: Test-only. Patches ``get_ops_redis_client`` with an in-memory
    fake and the three cache-reset entry points with spies.
Risky contracts: Every entry point is best-effort and must never raise — the
    failure-path tests assert that explicitly.
Validation: ``uv run pytest -q api/tests/test_jobs_cache_signal.py``.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from api.services.blast import jobs_cache_signal as sig


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    sig.reset_jobs_cache_subscriber_state_for_test()
    yield
    sig.stop_jobs_cache_subscriber(timeout=2.0)
    sig.reset_jobs_cache_subscriber_state_for_test()


def test_local_invalidate_drops_all_three_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "api.services.blast.jobs_list_cache.reset_jobs_list_cache",
        lambda: calls.append("jobs_list"),
    )
    monkeypatch.setattr(
        "api.services.monitor_cache.invalidate_monitor_snapshot_prefix",
        lambda prefix: calls.append(f"monitor:{prefix}"),
    )
    monkeypatch.setattr(
        "api.services.blast.external_jobs._reset_external_jobs_cache",
        lambda: calls.append("external"),
    )

    sig.invalidate_jobs_visibility_caches_local()

    assert calls == ["jobs_list", "monitor:monitor:message-flow", "external"]


def test_local_invalidate_isolates_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _boom() -> None:
        raise RuntimeError("cache module broken")

    monkeypatch.setattr(
        "api.services.blast.jobs_list_cache.reset_jobs_list_cache", _boom
    )
    monkeypatch.setattr(
        "api.services.monitor_cache.invalidate_monitor_snapshot_prefix",
        lambda prefix: calls.append("monitor"),
    )
    monkeypatch.setattr(
        "api.services.blast.external_jobs._reset_external_jobs_cache",
        lambda: calls.append("external"),
    )

    # A broken jobs-list reset must not block the other two, and must not raise.
    sig.invalidate_jobs_visibility_caches_local()

    assert calls == ["monitor", "external"]


def test_publish_returns_false_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_CACHE_INVALIDATE_DISABLED", "true")
    # Even if a client were reachable, the disabled gate short-circuits first.
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client",
        lambda **_k: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    assert sig.publish_jobs_cache_invalidate("x") is False


def test_publish_writes_to_channel_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_CACHE_INVALIDATE_DISABLED", "false")
    published: list[tuple[str, str]] = []

    class _FakeRedis:
        def publish(self, channel: str, payload: str) -> None:
            published.append((channel, payload))

    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda **_k: _FakeRedis()
    )

    assert sig.publish_jobs_cache_invalidate("servicebus_drain_submitted") is True
    assert len(published) == 1
    channel, payload = published[0]
    assert channel == sig._CHANNEL
    assert "servicebus_drain_submitted" in payload


def test_publish_swallows_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_CACHE_INVALIDATE_DISABLED", "false")
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("no redis")),
    )
    assert sig.publish_jobs_cache_invalidate("x") is False


def test_notify_does_local_and_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_CACHE_INVALIDATE_DISABLED", "false")
    local: list[bool] = []
    published: list[str] = []
    monkeypatch.setattr(
        sig, "invalidate_jobs_visibility_caches_local", lambda: local.append(True)
    )

    class _FakeRedis:
        def publish(self, channel: str, payload: str) -> None:
            published.append(payload)

    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda **_k: _FakeRedis()
    )

    sig.notify_jobs_cache_changed("playground")

    assert local == [True]
    assert len(published) == 1


def test_subscriber_no_op_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_CACHE_INVALIDATE_DISABLED", "true")
    assert sig.start_jobs_cache_subscriber() is None


def test_subscriber_invalidates_on_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_CACHE_INVALIDATE_DISABLED", "false")
    invalidated = threading.Event()
    monkeypatch.setattr(
        sig, "invalidate_jobs_visibility_caches_local", invalidated.set
    )

    class _FakePubSub:
        def __init__(self) -> None:
            self._sent = False

        def subscribe(self, channel: str) -> None:
            pass

        def get_message(self, timeout: float = 1.0) -> dict[str, Any] | None:
            # Deliver exactly one message, then idle so the loop keeps polling
            # until the test sets the stop event in teardown.
            if not self._sent:
                self._sent = True
                return {"type": "message", "data": b'{"reason":"x"}'}
            time.sleep(0.01)
            return None

        def close(self) -> None:
            pass

    class _FakeRedis:
        def pubsub(self, ignore_subscribe_messages: bool = False) -> _FakePubSub:
            return _FakePubSub()

    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda **_k: _FakeRedis()
    )

    thread = sig.start_jobs_cache_subscriber()
    assert thread is not None
    assert invalidated.wait(timeout=3.0), "subscriber did not invalidate on message"

"""Tests for the optional resident Service Bus consumer (issue #36 Tier 3).

Responsibility: Verify the default-OFF ``SERVICEBUS_RESIDENT_CONSUMER`` gate, the
    bounded/interruptible loop, error backoff, and that the loop drains via the
    same ``_drain_handler`` the beat task uses.
Edit boundaries: Test-only. Service Bus drain is mocked; no real thread sleeps
    beyond tiny backoff waits.
Key entry points: ``test_*``.
Risky contracts: gate requires env AND service_bus_enabled; the loop exits on
    stop_event and on max_iterations; a drain error backs off without raising.
Validation: ``uv run pytest -q api/tests/test_resident_consumer.py``.
"""

from __future__ import annotations

import threading

import pytest
from api.services.blast import resident_consumer as rc


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SERVICEBUS_RESIDENT_CONSUMER", raising=False)
    rc.reset_resident_consumer_state_for_test()
    yield
    rc.stop_resident_consumer(timeout=2.0)
    rc.reset_resident_consumer_state_for_test()


def test_gate_off_by_default() -> None:
    assert rc.resident_consumer_enabled() is False


def test_gate_requires_env_and_sb_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICEBUS_RESIDENT_CONSUMER", "true")
    monkeypatch.setattr("api.services.service_bus_pref.service_bus_enabled", lambda: False)
    assert rc.resident_consumer_enabled() is False
    monkeypatch.setattr("api.services.service_bus_pref.service_bus_enabled", lambda: True)
    assert rc.resident_consumer_enabled() is True


def test_run_loop_drains_and_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import service_bus
    from api.services.service_bus import DrainStats

    calls = {"n": 0}

    def _fake_drain(cfg, handler, *, max_messages, max_wait_seconds):
        calls["n"] += 1
        s = DrainStats(received=2)
        s.completed = 2
        return s

    monkeypatch.setattr(service_bus, "drain_requests", _fake_drain)
    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config", lambda: object()
    )

    stop = threading.Event()
    totals = rc.run_resident_consumer(stop, poll_wait_seconds=1, drain_batch=4, max_iterations=3)
    assert calls["n"] == 3
    assert totals["iterations"] == 3
    assert totals["received"] == 6
    assert totals["completed"] == 6


def test_run_loop_stops_on_event(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import service_bus
    from api.services.service_bus import DrainStats

    stop = threading.Event()

    def _fake_drain(cfg, handler, *, max_messages, max_wait_seconds):
        stop.set()  # ask to stop after the first drain
        return DrainStats()

    monkeypatch.setattr(service_bus, "drain_requests", _fake_drain)
    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config", lambda: object()
    )

    totals = rc.run_resident_consumer(stop, poll_wait_seconds=1, max_iterations=100)
    # Stopped after one iteration despite a high max_iterations.
    assert totals["iterations"] == 1


def test_run_loop_backs_off_on_error_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import service_bus

    def _boom(*_a, **_k):
        raise RuntimeError("sb down")

    monkeypatch.setattr(service_bus, "drain_requests", _boom)
    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config", lambda: object()
    )
    # Speed up the backoff wait so the test is fast.
    monkeypatch.setattr(rc, "_BACKOFF_START_SECONDS", 0.01)
    monkeypatch.setattr(rc, "_BACKOFF_MAX_SECONDS", 0.02)

    stop = threading.Event()
    # Must not raise even though every drain fails.
    totals = rc.run_resident_consumer(stop, poll_wait_seconds=1, max_iterations=3)
    assert totals["iterations"] == 3
    assert totals["received"] == 0


def test_start_returns_false_when_disabled() -> None:
    assert rc.start_resident_consumer() is False


def test_start_and_stop_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import service_bus
    from api.services.service_bus import DrainStats

    monkeypatch.setenv("SERVICEBUS_RESIDENT_CONSUMER", "true")
    monkeypatch.setattr("api.services.service_bus_pref.service_bus_enabled", lambda: True)
    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config", lambda: object()
    )

    drained = threading.Event()

    def _fake_drain(cfg, handler, *, max_messages, max_wait_seconds):
        drained.set()
        return DrainStats()

    monkeypatch.setattr(service_bus, "drain_requests", _fake_drain)

    assert rc.start_resident_consumer() is True
    # A second start is a no-op while the first is running.
    assert rc.start_resident_consumer() is False
    assert drained.wait(timeout=3.0)
    rc.stop_resident_consumer(timeout=3.0)

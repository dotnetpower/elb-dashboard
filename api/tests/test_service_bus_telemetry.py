"""Tests for the Service Bus telemetry rolling-window helpers.

Responsibility: Verify ``record_dlq_sample`` is a no-op for bad inputs,
    ``dlq_delta`` returns the floor (no negative growth on a healed queue),
    process-restart history is empty, the per-(namespace, queue) keying
    isolates two namespaces, and the rolling window trims old samples.
Edit boundaries: Pure aggregation tests; no SDK or HTTP mocks needed.
Key entry points: the ``test_*`` functions.
Risky contracts: Time is monkeypatched via ``_now`` so the window trim
    behaviour is deterministic.
Validation: ``uv run pytest -q api/tests/test_service_bus_telemetry.py``.
"""

from __future__ import annotations

import pytest

from api.services import service_bus_telemetry as svc


@pytest.fixture(autouse=True)
def _reset() -> None:
    svc.reset_for_tests()
    yield
    svc.reset_for_tests()


def test_no_history_returns_none() -> None:
    assert svc.dlq_delta("ns.example", "q1") is None


def test_single_sample_yields_zero_delta() -> None:
    svc.record_dlq_sample("ns.example", "q1", 7)
    snap = svc.dlq_delta("ns.example", "q1")
    assert snap is not None
    assert snap.samples == 1
    assert snap.baseline_dlq == 7
    assert snap.current_dlq == 7
    assert snap.delta == 0


def test_growth_is_positive_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delta = current - baseline, never extrapolated past observed data."""
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(svc, "_now", lambda: clock["t"])

    svc.record_dlq_sample("ns.example", "q1", 2)
    clock["t"] += 60
    svc.record_dlq_sample("ns.example", "q1", 5)
    clock["t"] += 60
    svc.record_dlq_sample("ns.example", "q1", 12)

    snap = svc.dlq_delta("ns.example", "q1")
    assert snap is not None
    assert snap.samples == 3
    assert snap.baseline_dlq == 2
    assert snap.current_dlq == 12
    assert snap.delta == 10
    # ``elapsed_seconds`` reflects how far the window actually spans.
    assert snap.elapsed_seconds == pytest.approx(120.0, abs=0.5)


def test_healing_queue_reports_zero_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    """A purge that drops DLQ below baseline must not surface as negative
    growth — the SPA's alarm should clear, not fire."""
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(svc, "_now", lambda: clock["t"])

    svc.record_dlq_sample("ns.example", "q1", 50)
    clock["t"] += 30
    svc.record_dlq_sample("ns.example", "q1", 0)  # operator-driven purge

    snap = svc.dlq_delta("ns.example", "q1")
    assert snap is not None
    assert snap.baseline_dlq == 50
    assert snap.current_dlq == 0
    assert snap.delta == 0  # clamped, never -50


def test_bad_inputs_are_silently_dropped() -> None:
    svc.record_dlq_sample("ns.example", "q1", -1)  # negative
    svc.record_dlq_sample("ns.example", "q1", True)  # bool (subclass of int)
    svc.record_dlq_sample("", "q1", 1)  # missing namespace
    assert svc.dlq_delta("ns.example", "q1") is None


def test_per_namespace_queue_isolation() -> None:
    svc.record_dlq_sample("ns-a", "q1", 1)
    svc.record_dlq_sample("ns-b", "q1", 100)
    svc.record_dlq_sample("ns-a", "q2", 7)

    a1 = svc.dlq_delta("ns-a", "q1")
    b1 = svc.dlq_delta("ns-b", "q1")
    a2 = svc.dlq_delta("ns-a", "q2")
    assert a1 is not None and a1.current_dlq == 1
    assert b1 is not None and b1.current_dlq == 100
    assert a2 is not None and a2.current_dlq == 7


def test_old_samples_are_trimmed_from_window(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(svc, "_now", lambda: clock["t"])

    svc.record_dlq_sample("ns.example", "q1", 1)
    clock["t"] += svc._WINDOW_SECONDS + 1  # beyond the rolling window
    svc.record_dlq_sample("ns.example", "q1", 9)

    snap = svc.dlq_delta("ns.example", "q1")
    assert snap is not None
    # Only the second sample survives the trim, so baseline == current.
    assert snap.samples == 1
    assert snap.baseline_dlq == 9
    assert snap.current_dlq == 9
    assert snap.delta == 0


def test_namespace_keying_is_case_insensitive() -> None:
    """A casing-only config edit must not start a fresh history."""
    svc.record_dlq_sample("SB-EXAMPLE.servicebus.windows.net", "q1", 3)
    svc.record_dlq_sample("sb-example.servicebus.windows.net", "q1", 8)
    snap = svc.dlq_delta("sb-EXAMPLE.servicebus.windows.net", "q1")
    assert snap is not None
    assert snap.samples == 2
    assert snap.baseline_dlq == 3
    assert snap.current_dlq == 8
    assert snap.delta == 5

"""Unit tests for the Service-Bus-queue-aware auto-stop signal.

Responsibility: Verify ``idle_autostop._sb_pending_signal`` gates correctly
    (power-state, ``AKS_AUTOSTOP_RESPECT_SB_QUEUE`` env, Service Bus enabled)
    and degrades to ``None`` on any failure so the evaluator never strands a
    cluster running forever.
Edit boundaries: Unit-level — Service Bus access is stubbed via monkeypatch,
    no real Azure call. Does not test ``evaluate_cluster`` itself (that lives
    in ``test_auto_stop_evaluator.py``).
Key entry points: the ``test_*`` functions.
Risky contracts: ``_sb_pending_signal`` MUST return ``None`` (not raise) for a
    non-Running cluster, when the gate env is off, when Service Bus is
    disabled, or on any exception; and MUST delegate to
    ``service_bus.pending_request_count`` only when all gates pass.
Validation: ``uv run pytest -q api/tests/test_idle_autostop_sb_queue.py``.
"""

from __future__ import annotations

import pytest
from api.services import service_bus, service_bus_pref
from api.tasks.azure import idle_autostop


def test_skips_non_running_cluster() -> None:
    """A stopped/starting cluster has no work-in-flight question to answer."""
    assert idle_autostop._sb_pending_signal("Stopped") is None
    assert idle_autostop._sb_pending_signal("") is None


def test_gate_env_off_disables_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AKS_AUTOSTOP_RESPECT_SB_QUEUE=false`` turns the signal off without a
    redeploy (default-on, env-disable)."""
    monkeypatch.setenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", "false")
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus, "pending_request_count", lambda _cfg: 5)
    assert idle_autostop._sb_pending_signal("Running") is None


def test_none_when_service_bus_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", raising=False)
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: False)
    assert idle_autostop._sb_pending_signal("Running") is None


def test_delegates_to_pending_request_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", raising=False)
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus_pref, "get_service_bus_config", lambda: object())
    monkeypatch.setattr(service_bus, "pending_request_count", lambda _cfg: 9)
    assert idle_autostop._sb_pending_signal("Running") == 9


def test_none_on_unexpected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising dependency degrades to ``None`` (never fails the tick)."""
    monkeypatch.delenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", raising=False)

    def _boom() -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", _boom)
    assert idle_autostop._sb_pending_signal("Running") is None

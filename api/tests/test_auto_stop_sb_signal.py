"""Unit tests for the shared auto-stop Service Bus queue keep-alive signal.

Responsibility: Verify ``auto_stop_sb_signal.pending_queue_signal`` gates
    correctly (power-state, ``AKS_AUTOSTOP_RESPECT_SB_QUEUE`` env, Service Bus
    enabled), caches the deployment-global request-queue read for the status
    route (one admin call per TTL window), bypasses the cache when
    ``ttl_seconds <= 0`` (beat driver path), and degrades to ``None`` on any
    failure so an unreadable queue never strands a cluster running forever.
Edit boundaries: Unit-level — Service Bus access is stubbed via monkeypatch,
    no real Azure call and no wall-clock sleep (cache reset between cases).
Key entry points: the ``test_*`` functions.
Risky contracts: ``pending_queue_signal`` MUST return ``None`` (not raise) for
    a non-Running cluster, when the env gate is off, when Service Bus is
    disabled, or on any exception; MUST delegate to
    ``service_bus.pending_request_count`` only when all gates pass; and MUST
    serve a cached value within the TTL window while re-reading once the cache
    is bypassed/cleared.
Validation: ``uv run pytest -q api/tests/test_auto_stop_sb_signal.py``.
"""

from __future__ import annotations

import pytest
from api.services import auto_stop_sb_signal, service_bus, service_bus_pref


@pytest.fixture(autouse=True)
def _reset_signal_cache() -> None:
    """Clear the module-global cache so cases never leak state into each other."""
    auto_stop_sb_signal._reset_cache_for_tests()
    yield
    auto_stop_sb_signal._reset_cache_for_tests()


def test_skips_non_running_cluster() -> None:
    assert auto_stop_sb_signal.pending_queue_signal("Stopped") is None
    assert auto_stop_sb_signal.pending_queue_signal("") is None


def test_gate_env_off_disables_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", "false")
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus, "pending_request_count", lambda _cfg: 5)
    assert auto_stop_sb_signal.pending_queue_signal("Running", ttl_seconds=0.0) is None


def test_none_when_service_bus_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", raising=False)
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: False)
    assert auto_stop_sb_signal.pending_queue_signal("Running", ttl_seconds=0.0) is None


def test_delegates_to_pending_request_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", raising=False)
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus_pref, "get_service_bus_config", lambda: object())
    monkeypatch.setattr(service_bus, "pending_request_count", lambda _cfg: 9)
    assert auto_stop_sb_signal.pending_queue_signal("Running", ttl_seconds=0.0) == 9


def test_none_on_unexpected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", raising=False)

    def _boom() -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", _boom)
    assert auto_stop_sb_signal.pending_queue_signal("Running", ttl_seconds=0.0) is None


def test_cache_serves_one_admin_call_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A status-poll fan-in collapses to a single admin read within the TTL."""
    monkeypatch.delenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", raising=False)
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus_pref, "get_service_bus_config", lambda: object())
    calls = {"n": 0}

    def _count(_cfg: object) -> int:
        calls["n"] += 1
        return 7

    monkeypatch.setattr(service_bus, "pending_request_count", _count)

    first = auto_stop_sb_signal.pending_queue_signal("Running", ttl_seconds=60.0)
    second = auto_stop_sb_signal.pending_queue_signal("Running", ttl_seconds=60.0)
    assert first == 7
    assert second == 7
    assert calls["n"] == 1


def test_cache_bypassed_when_ttl_non_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The beat driver path (``ttl_seconds=0``) always reads the live queue."""
    monkeypatch.delenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", raising=False)
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus_pref, "get_service_bus_config", lambda: object())
    calls = {"n": 0}

    def _count(_cfg: object) -> int:
        calls["n"] += 1
        return 3

    monkeypatch.setattr(service_bus, "pending_request_count", _count)

    auto_stop_sb_signal.pending_queue_signal("Running", ttl_seconds=0.0)
    auto_stop_sb_signal.pending_queue_signal("Running", ttl_seconds=0.0)
    assert calls["n"] == 2


def test_cache_re_reads_after_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing the cache (TTL expiry analogue) forces a fresh read."""
    monkeypatch.delenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", raising=False)
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus_pref, "get_service_bus_config", lambda: object())
    seq = iter([4, 8])
    monkeypatch.setattr(service_bus, "pending_request_count", lambda _cfg: next(seq))

    assert auto_stop_sb_signal.pending_queue_signal("Running", ttl_seconds=60.0) == 4
    auto_stop_sb_signal._reset_cache_for_tests()
    assert auto_stop_sb_signal.pending_queue_signal("Running", ttl_seconds=60.0) == 8


# --------------------------------------------------------------------------- #
# read_request_queue_depth — the queue-arrival auto-START read (power-state
# agnostic, NOT gated by AKS_AUTOSTOP_RESPECT_SB_QUEUE).
# --------------------------------------------------------------------------- #


def test_autostart_depth_reads_for_a_stopped_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unlike pending_queue_signal (Running-only), this read is power-state
    # agnostic — a Stopped cluster MUST still see the queued work so it can start.
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus_pref, "get_service_bus_config", lambda: object())
    monkeypatch.setattr(service_bus, "pending_request_count", lambda _cfg: 7)
    assert auto_stop_sb_signal.read_request_queue_depth() == 7


def test_autostart_depth_ignores_respect_queue_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stop-side keep-alive gate must NOT suppress the start-side read.
    monkeypatch.setenv("AKS_AUTOSTOP_RESPECT_SB_QUEUE", "false")
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus_pref, "get_service_bus_config", lambda: object())
    monkeypatch.setattr(service_bus, "pending_request_count", lambda _cfg: 3)
    assert auto_stop_sb_signal.read_request_queue_depth() == 3


def test_autostart_depth_none_when_sb_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: False)
    assert auto_stop_sb_signal.read_request_queue_depth() is None


def test_autostart_depth_none_on_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_bus_pref, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(service_bus_pref, "get_service_bus_config", lambda: object())

    def _boom(_cfg: object) -> int:
        raise RuntimeError("admin read failed")

    monkeypatch.setattr(service_bus, "pending_request_count", _boom)
    # Never raise: a failed read must not trigger a cost-bearing start.
    assert auto_stop_sb_signal.read_request_queue_depth() is None

"""Tests for the opt-in memory diagnostics sampler.

Responsibility: Cover RSS/GC sampling, the malloc_trim mitigation, defensive env
parsing, and the default-OFF / enabled start behaviour of the sampler.
Edit boundaries: Test-only.
Key entry points: pytest test functions.
Risky contracts: The sampler must be a no-op when disabled and must never raise.
Validation: `uv run pytest -q api/tests/test_memory_diagnostics.py`.
"""

from __future__ import annotations

import threading
import time

import pytest
from api.app import memory_diagnostics as md


def test_read_rss_bytes_is_positive_on_linux() -> None:
    rss = md.read_rss_bytes()
    # On the Linux CI/dev host this is a real positive number; the helper
    # returns None only where /proc is unavailable.
    assert rss is None or rss > 0


def test_sample_once_returns_metrics_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="api.app.memory_diagnostics"):
        metrics = md.sample_once()
    assert set(metrics) >= {"rss_bytes", "gc_count", "gc_objects"}
    assert isinstance(metrics["gc_objects"], int)
    assert any("memtrace rss=" in rec.message for rec in caplog.records)


def test_sample_once_with_trim_records_delta() -> None:
    metrics = md.sample_once(trim=True)
    assert "malloc_trimmed" in metrics
    assert "rss_bytes_after_trim" in metrics
    assert isinstance(metrics["malloc_trimmed"], bool)


def test_malloc_trim_never_raises() -> None:
    # Whatever the libc, the helper must return a bool and never raise.
    assert isinstance(md.malloc_trim(), bool)


def test_start_memory_sampler_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_MEMTRACE_INTERVAL_SECONDS", raising=False)
    assert md.start_memory_sampler() is None


def test_start_memory_sampler_invalid_interval_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_MEMTRACE_INTERVAL_SECONDS", "not-a-number")
    assert md.start_memory_sampler() is None


def test_start_memory_sampler_enabled_starts_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_MEMTRACE_INTERVAL_SECONDS", "5")
    stop = md.start_memory_sampler()
    assert isinstance(stop, threading.Event)
    # The sampler waits `interval` before its first sample, so setting the stop
    # event immediately shuts the thread down without any sample firing.
    stop.set()


def test_env_int_clamps_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_MEMTRACE_TOPN", "9999")
    assert md._env_int("API_MEMTRACE_TOPN", 5, minimum=0, maximum=50) == 50
    monkeypatch.setenv("API_MEMTRACE_TOPN", "bad")
    assert md._env_int("API_MEMTRACE_TOPN", 5, minimum=0, maximum=50) == 5


# --------------------------------------------------------------------------- #
# Arena reclaimer — the production mitigation for the api sidecar OOM (exit 137)
# --------------------------------------------------------------------------- #


def test_arena_reclaimer_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-ON: this is a mitigation, not an opt-in diagnostic.

    Regression guard for the api sidecar OOM loop — glibc retained 40-47% of RSS
    as freed-but-unreturned arenas until the 2 GiB limit SIGKILL'd the container.
    """
    monkeypatch.delenv("API_ARENA_RECLAIM_INTERVAL_SECONDS", raising=False)
    calls: list[int] = []
    monkeypatch.setattr(md, "malloc_trim", lambda: calls.append(1) or True)

    stop = md.start_arena_reclaimer()
    try:
        assert stop is not None
        # Probed once up front so an unusable libc never starts the thread.
        assert calls == [1]
    finally:
        if stop is not None:
            stop.set()


def test_arena_reclaimer_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_ARENA_RECLAIM_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(md, "malloc_trim", lambda: True)

    assert md.start_arena_reclaimer() is None


def test_arena_reclaimer_skips_when_malloc_trim_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """musl / any libc without malloc_trim: do not start a thread that no-ops."""
    monkeypatch.delenv("API_ARENA_RECLAIM_INTERVAL_SECONDS", raising=False)
    monkeypatch.setattr(md, "malloc_trim", lambda: False)

    assert md.start_arena_reclaimer() is None


def test_arena_reclaimer_interval_is_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A too-small override must not turn the reclaimer into a busy loop."""
    monkeypatch.setenv("API_ARENA_RECLAIM_INTERVAL_SECONDS", "0.01")
    monkeypatch.setattr(md, "malloc_trim", lambda: True)
    waits: list[float] = []

    class _Event:
        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            return True  # stop immediately

        def set(self) -> None:
            return None

    stop = md.start_arena_reclaimer(stop_event=_Event())  # type: ignore[arg-type]
    assert stop is not None
    for _ in range(50):
        if waits:
            break
        time.sleep(0.01)
    assert waits and waits[0] >= md._MIN_TRIM_INTERVAL_SECONDS

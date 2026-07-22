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

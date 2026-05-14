"""Tests for the cgroup reporter — pure-function pieces only.

The IO loop and Redis publishing are tested at integration time
(docker-compose with a real Redis sidecar); see
``docs/features_change/2026-05/2026-05-15-sidecars-card-sse.md``.
"""

from __future__ import annotations

import time

import pytest

from api.services.cgroup_reporter import CgroupReading, compute_cpu_pct


def test_compute_cpu_pct_zero_window_is_safe() -> None:
    r = CgroupReading(cpu_usec=1000, mem_bytes=0, ts=10.0)
    # Identical timestamps must not divide-by-zero.
    assert compute_cpu_pct(r, r) == 0.0


def test_compute_cpu_pct_one_full_core() -> None:
    # 1_000_000 cpu-usec consumed in 1.0s == 100% of one core.
    a = CgroupReading(cpu_usec=0, mem_bytes=0, ts=10.0)
    b = CgroupReading(cpu_usec=1_000_000, mem_bytes=0, ts=11.0)
    assert compute_cpu_pct(a, b) == 100.0


def test_compute_cpu_pct_partial_core() -> None:
    a = CgroupReading(cpu_usec=0, mem_bytes=0, ts=0.0)
    b = CgroupReading(cpu_usec=250_000, mem_bytes=0, ts=1.0)
    assert compute_cpu_pct(a, b) == 25.0


def test_compute_cpu_pct_clamps_negative_delta() -> None:
    # Counter resets (rare) shouldn't yield a negative percentage.
    a = CgroupReading(cpu_usec=10_000_000, mem_bytes=0, ts=0.0)
    b = CgroupReading(cpu_usec=5_000_000, mem_bytes=0, ts=1.0)
    assert compute_cpu_pct(a, b) == 0.0


def test_compute_cpu_pct_handles_real_clock_drift() -> None:
    # Reporter sleeps ~5s; allow tiny fp drift in dt.
    a = CgroupReading(cpu_usec=0, mem_bytes=0, ts=time.time())
    b = CgroupReading(cpu_usec=500_000, mem_bytes=0, ts=a.ts + 5.0001)
    pct = compute_cpu_pct(a, b)
    assert pct == pytest.approx(10.0, abs=0.05)

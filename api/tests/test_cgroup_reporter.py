"""Tests for the cgroup reporter - pure-function pieces only.

Responsibility: Tests for the cgroup reporter - pure-function pieces only
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_compute_cpu_pct_zero_window_is_safe`,
`test_compute_cpu_pct_one_full_core`, `test_compute_cpu_pct_partial_core`,
`test_compute_cpu_pct_clamps_negative_delta`, `test_compute_cpu_pct_handles_real_clock_drift`,
`test_read_procfs_self_returns_positive_reading`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_cgroup_reporter.py`.
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


def test_read_procfs_self_returns_positive_reading() -> None:
    """procfs fallback must work on any Linux host (including WSL2)."""
    from api.services.cgroup_reporter import read_procfs_self

    r = read_procfs_self()
    assert r.cpu_usec >= 0
    assert r.mem_bytes > 0  # any running Python process has VmRSS > 0
    assert r.ts > 0


def test_procfs_fallback_compatible_with_compute_cpu_pct() -> None:
    """Two procfs readings must be safe to feed into compute_cpu_pct."""
    import time

    from api.services.cgroup_reporter import compute_cpu_pct, read_procfs_self

    a = read_procfs_self()
    # Burn CPU for a fixed ~50 ms window. The window MUST be comfortably
    # larger than one scheduler clock tick (typically 10 ms): with a sub-tick
    # window a single 10 ms utime/stime increment lands against a ~1 ms wall
    # delta and the ratio spikes past 1000 % (a flake under the `-n auto`
    # parallel load CI runs). A single thread can't exceed one core, so over a
    # 50 ms window pct converges near 100 % with only modest tick quantization.
    s = 0
    i = 0
    end = time.perf_counter() + 0.05
    while time.perf_counter() < end:
        s += i
        i += 1
    b = read_procfs_self()
    pct = compute_cpu_pct(a, b)
    assert pct >= 0.0
    assert pct < 1000.0  # sanity bound — single Python loop can't peg 10 cores

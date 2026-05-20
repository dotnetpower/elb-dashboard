"""Unit tests for `api.services.request_metrics`.

Responsibility: Unit tests for `api.services.request_metrics`
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `setup_function`, `test_normalise_path_collapses_known_patterns`,
`test_normalise_path_strips_query`, `test_summarise_no_samples_returns_degraded`,
`test_record_and_summarise_percentiles`, `test_error_count_includes_5xx_and_dispatch_failure`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_request_metrics.py`.
"""

from __future__ import annotations

import time

import pytest
from api.services import request_metrics as rm


def setup_function() -> None:
    rm.reset_for_tests()


def test_normalise_path_collapses_known_patterns() -> None:
    assert rm.normalise_path("/api/blast/jobs/abc123") == "/api/blast/jobs/{id}"
    assert rm.normalise_path("/api/blast/databases/16S/shard") == "/api/blast/databases/{db}/shard"
    assert rm.normalise_path("/api/blast/databases/16S") == "/api/blast/databases/{db}"
    assert rm.normalise_path("/api/tasks/uuid-1234") == "/api/tasks/{id}"
    assert rm.normalise_path("/api/health") == "/api/health"
    assert rm.normalise_path("/api/blast/jobs/") == "/api/blast/jobs"


def test_normalise_path_strips_query() -> None:
    assert rm.normalise_path("/api/monitor/aks?subscription_id=x") == "/api/monitor/aks"


def test_summarise_no_samples_returns_degraded() -> None:
    out = rm.metrics().summarise(window_seconds=60)
    assert out["degraded"] is True
    assert out["degraded_reason"] == "no_samples"
    assert out["total"] == 0
    assert out["p95_ms"] is None
    assert len(out["rpm"]) == 1  # 60s window -> 1 bucket
    assert out["rpm"][0]["count"] == 0


def test_record_and_summarise_percentiles() -> None:
    now = time.time()
    # 10 samples, durations 10..100 ms, all within last 60s.
    for i in range(10):
        rm.metrics().record(
            path="/api/blast/jobs",
            status=200,
            duration_ms=(i + 1) * 10.0,
            ts=now - 5,
        )
    out = rm.metrics().summarise(window_seconds=300, rpm_buckets=5)
    assert out["total"] == 10
    assert out["errors"] == 0
    assert out["error_rate"] == 0.0
    assert out["p50_ms"] == 50.0
    assert out["p95_ms"] == 100.0
    assert out["p99_ms"] == 100.0
    assert len(out["rpm"]) == 5


def test_error_count_includes_5xx_and_dispatch_failure() -> None:
    now = time.time()
    rm.metrics().record(path="/api/blast/jobs", status=200, duration_ms=10, ts=now)
    rm.metrics().record(path="/api/blast/jobs", status=500, duration_ms=20, ts=now)
    rm.metrics().record(path="/api/blast/jobs", status=503, duration_ms=30, ts=now)
    rm.metrics().record(path="/api/blast/jobs", status=0, duration_ms=40, ts=now)  # dispatch error
    rm.metrics().record(path="/api/blast/jobs", status=404, duration_ms=5, ts=now)  # not an error
    out = rm.metrics().summarise(window_seconds=120)
    assert out["total"] == 5
    assert out["errors"] == 3
    assert out["error_rate"] == pytest.approx(0.6, abs=0.001)


def test_path_prefix_filter() -> None:
    now = time.time()
    rm.metrics().record(path="/api/blast/jobs", status=200, duration_ms=10, ts=now)
    rm.metrics().record(path="/api/monitor/aks", status=200, duration_ms=20, ts=now)
    out = rm.metrics().summarise(window_seconds=120, path_prefix="/api/blast")
    assert out["total"] == 1
    assert out["by_path"][0]["path"].startswith("/api/blast")


def test_window_excludes_old_samples() -> None:
    now = time.time()
    rm.metrics().record(path="/api/blast/jobs", status=200, duration_ms=10, ts=now - 1000)
    rm.metrics().record(path="/api/blast/jobs", status=200, duration_ms=20, ts=now)
    out = rm.metrics().summarise(window_seconds=60)
    assert out["total"] == 1


def test_rpm_buckets_are_minute_aligned_with_window() -> None:
    now = time.time()
    rm.metrics().record(path="/api/blast/jobs", status=200, duration_ms=10, ts=now - 30)
    rm.metrics().record(path="/api/blast/jobs", status=200, duration_ms=10, ts=now - 90)
    out = rm.metrics().summarise(window_seconds=300, rpm_buckets=5)
    counts = [b["count"] for b in out["rpm"]]
    # Newest two buckets carry our two samples; older 3 are zero.
    assert sum(counts) == 2
    assert counts[-1] >= 1


def test_invalid_window_raises() -> None:
    with pytest.raises(ValueError):
        rm.metrics().summarise(window_seconds=0)
    with pytest.raises(ValueError):
        rm.metrics().summarise(window_seconds=rm.MAX_WINDOW_SECONDS + 1)


def test_buffer_capacity_evicts_oldest() -> None:
    cap = rm.metrics().capacity
    now = time.time()
    # Push capacity+50 samples; oldest 50 should be evicted.
    for _ in range(cap + 50):
        rm.metrics().record(path="/api/blast/jobs", status=200, duration_ms=1, ts=now)
    out = rm.metrics().summarise(window_seconds=120)
    assert out["total"] <= cap


def test_normalise_path_caps_long_paths() -> None:
    # Fuzz-style very long path that doesn't match any rule.
    huge = "/api/" + ("x" * (rm.MAX_PATH_LEN * 4))
    out = rm.normalise_path(huge)
    assert len(out) <= rm.MAX_PATH_LEN
    assert out.endswith("…")


def test_normalise_path_rule_match_is_immune_to_length() -> None:
    # A genuine but very long job id still collapses to the canonical form,
    # so length capping never widens cardinality.
    huge_id = "x" * 4000
    assert rm.normalise_path(f"/api/blast/jobs/{huge_id}") == "/api/blast/jobs/{id}"

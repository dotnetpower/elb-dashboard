"""Tests for measured cluster-lifecycle timings + the start-stats route.

Responsibility: Lock in `api.services.cluster_timings` record/aggregate
    behaviour on the local file backend (median, sample cap, range guard,
    unknown-phase reject) and the `/api/monitor/aks/start-stats` response shape
    the SPA `StartEstimatePanel` depends on (phase dict + `api_ready_seconds`,
    default fallback when no samples).
Edit boundaries: Pure unit tests. The file backend is exercised by pointing
    `ELB_LOCAL_STATE_DIR` at a tmp dir; the Table backend is never touched.
Key entry points: see per-test docstrings.
Risky contracts: `PhaseStat.to_dict` field names and the route payload keys are
    consumed by the frontend; changing them breaks the start panel.
Validation: `uv run pytest -q api/tests/test_cluster_timings.py`.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def timings(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Reload the module with the file backend pointed at a tmp dir."""
    # Force the file backend (no CONTAINER_APP_NAME ⇒ _use_table_backend False).
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    import api.services.cluster_timings as mod

    importlib.reload(mod)
    return mod


def test_record_then_median(timings: Any) -> None:
    """Median of recorded durations is reported with source=measured."""
    for seconds in (200.0, 240.0, 260.0):
        assert timings.record_timing("aks_start", seconds) is True
    stats = timings.get_timing_stats()
    aks = stats["aks_start"]
    assert aks.source == "measured"
    assert aks.samples == 3
    assert aks.seconds == 240.0  # median of 200/240/260
    assert aks.last_observed_at is not None


def test_default_when_no_samples(timings: Any) -> None:
    """A phase with no samples falls back to the built-in default."""
    stats = timings.get_timing_stats()
    aks = stats["aks_start"]
    assert aks.source == "default"
    assert aks.samples == 0
    assert aks.seconds == timings.DEFAULT_SECONDS["aks_start"]


def test_unknown_phase_rejected(timings: Any) -> None:
    """Recording an unknown phase is a no-op returning False."""
    assert timings.record_timing("not_a_phase", 10.0) is False


def test_aks_scale_phase_is_known(timings: Any) -> None:
    """`aks_scale` is a recordable phase so scale_aks's lifecycle timing is
    persisted instead of being dropped as 'unknown phase' (live E2E 2026-06-08)."""
    assert "aks_scale" in timings.DEFAULT_SECONDS
    assert timings.record_timing("aks_scale", 75.0) is True
    stats = timings.get_timing_stats()
    assert stats["aks_scale"].source == "measured"
    assert stats["aks_scale"].seconds == 75.0


def test_out_of_range_dropped(timings: Any) -> None:
    """Zero / negative / absurdly large durations are dropped."""
    assert timings.record_timing("aks_start", 0.0) is False
    assert timings.record_timing("aks_start", -5.0) is False
    assert timings.record_timing("aks_start", 10_000.0) is False
    assert timings.get_timing_stats()["aks_start"].source == "default"


def test_sample_limit_uses_recent(timings: Any) -> None:
    """Only the most-recent sample_limit observations feed the median."""
    # Old slow samples first, then many fast recent ones.
    for _ in range(5):
        timings.record_timing("aks_start", 600.0)
    for _ in range(20):
        timings.record_timing("aks_start", 100.0)
    aks = timings.get_timing_stats(sample_limit=20)["aks_start"]
    assert aks.samples == 20
    assert aks.seconds == 100.0


def test_phasestat_to_dict_shape(timings: Any) -> None:
    """to_dict carries the exact keys the frontend reads."""
    timings.record_timing("openapi_deploy", 31.0)
    payload = timings.get_timing_stats()["openapi_deploy"].to_dict()
    assert set(payload) == {
        "phase",
        "seconds",
        "samples",
        "last_observed_at",
        "source",
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> TestClient:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    import api.services.cluster_timings as mod

    importlib.reload(mod)
    from api.main import app

    return TestClient(app)


def test_start_stats_route_defaults(client: TestClient) -> None:
    """With no samples the route returns all-default phases and a sum."""
    resp = client.get("/api/monitor/aks/start-stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "phases" in body and "api_ready_seconds" in body
    assert body["phases"]["aks_start"]["source"] == "default"
    # default 235 + 31 = 266
    assert body["api_ready_seconds"] == pytest.approx(266.0)


def test_start_stats_route_reflects_measurements(
    client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded sample is surfaced as source=measured via the route."""
    import api.services.cluster_timings as mod

    mod.record_timing("aks_start", 300.0)
    resp = client.get("/api/monitor/aks/start-stats")
    assert resp.status_code == 200
    aks = resp.json()["phases"]["aks_start"]
    assert aks["source"] == "measured"
    assert aks["seconds"] == 300.0

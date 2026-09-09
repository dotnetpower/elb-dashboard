"""Tests for evidence-gated BLAST runtime and cost estimation.

Responsibility: Verify comparable-sample extraction, robust normalized prediction, explicit
    insufficient evidence, and the read-only HTTP route.
Edit boundaries: Pure estimator and TestClient tests; no Azure, billing, Storage, or broker I/O.
Key entry points: ``test_*``.
Risky contracts: Fewer than three comparable samples must never produce a number, and route
    failure must degrade without affecting submission.
Validation: ``uv run pytest -q api/tests/test_blast_runtime_estimate.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.services.blast.runtime_estimate import RuntimeEstimateRequest, estimate_runtime_cost
from fastapi.testclient import TestClient


def _request() -> RuntimeEstimateRequest:
    return RuntimeEstimateRequest(
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        program="blastn",
        database="core_nt",
        query_letters=1_000,
        database_letters=1_000_000,
        node_count=2,
        node_sku="Standard_E16s_v5",
        region="",
    )


def _state(duration_seconds: float, *, query_letters: int = 1_000) -> SimpleNamespace:
    return SimpleNamespace(
        status="completed",
        program="blastn",
        db="core_nt",
        payload={
            "machine_type": "Standard_E16s_v5",
            "num_nodes": 2,
            "db_total_letters": 1_000_000,
            "canonical_request": {
                "program": "blastn",
                "database": "core_nt",
                "query": {"total_letters": query_letters},
                "options": {},
            },
            "_progress": {
                "steps": {
                    "running": {"k8s": {"blast_container_duration_ms": duration_seconds * 1000}}
                }
            },
        },
    )


def test_estimate_requires_three_comparable_samples() -> None:
    result = estimate_runtime_cost(_request(), [_state(100), _state(110)])

    assert result.available is False
    assert result.reason == "insufficient_samples"
    assert result.sample_count == 2
    assert result.estimate_seconds is None
    assert result.estimated_cost_usd is None


def test_estimate_normalizes_work_and_excludes_extreme_outlier() -> None:
    result = estimate_runtime_cost(
        _request(),
        [_state(100), _state(110), _state(90), _state(10_000)],
    )

    assert result.available is True
    assert result.sample_count == 3
    assert 90 <= int(result.estimate_seconds or 0) <= 110
    assert result.low_seconds is not None
    assert result.high_seconds is not None
    assert result.confidence == "low"
    assert result.estimated_cost_usd is not None
    assert result.pricing and result.pricing["priced"] is True


def test_estimate_filters_program_database_and_sku_mismatch() -> None:
    good = _state(100)
    wrong_program = _state(100)
    wrong_program.program = "blastp"
    wrong_db = _state(100)
    wrong_db.db = "nr"
    wrong_sku = _state(100)
    wrong_sku.payload["machine_type"] = "Standard_D2s_v5"

    result = estimate_runtime_cost(_request(), [good, wrong_program, wrong_db, wrong_sku])

    assert result.available is False
    assert result.sample_count == 1


def test_estimate_ignores_corrupt_oversized_numeric_evidence() -> None:
    corrupt = _state(100)
    corrupt.payload["db_total_letters"] = 10**10_000

    result = estimate_runtime_cost(
        _request(),
        [_state(90), _state(100), _state(110), corrupt],
    )

    assert result.available is True
    assert result.sample_count == 3


def test_runtime_estimate_route_is_read_only_and_degrades(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    class Repo:
        def list_for_scope(self, **kwargs):
            assert kwargs["include_payload"] is True
            return [_state(100), _state(110), _state(90)]

    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: Repo())

    from api.main import app

    response = TestClient(app).post("/api/blast/runtime-estimate", json=_request().model_dump())

    assert response.status_code == 200
    assert response.json()["available"] is True

    class BrokenRepo:
        def list_for_scope(self, **_kwargs):
            raise RuntimeError("table down")

    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: BrokenRepo())
    degraded = TestClient(app).post("/api/blast/runtime-estimate", json=_request().model_dump())
    assert degraded.status_code == 200
    assert degraded.json()["reason"] == "estimate_unavailable"

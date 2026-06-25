"""Tests for the approximate cluster cost estimator.

Responsibility: Lock the pure cost math (priced/unpriced SKU, running/stopped,
uptime-driven accrual, monthly projection).
Edit boundaries: Test-only; no Azure.
Key entry points: pytest test functions.
Risky contracts: unknown SKU => priced False + zero cost (never a guessed number).
Validation: ``uv run pytest -q api/tests/test_cost_estimate.py``.
"""

from __future__ import annotations

from api.services.cost.estimate import estimate_cluster_cost, hourly_price_usd


def test_priced_sku_hourly_and_projection() -> None:
    est = estimate_cluster_cost(node_sku="Standard_E16s_v5", node_count=10, running=True)
    assert est.priced is True
    assert est.hourly_usd == hourly_price_usd("Standard_E16s_v5") * 10
    assert round(est.projected_monthly_usd, 2) == round(est.hourly_usd * 730.0, 2)
    assert est.accrued_usd is None  # no uptime given


def test_accrued_from_uptime() -> None:
    est = estimate_cluster_cost(
        node_sku="Standard_E16s_v5", node_count=1, uptime_seconds=3600, running=True
    )
    assert est.accrued_usd is not None
    assert round(est.accrued_usd, 4) == round(est.hourly_usd, 4)  # 1 hour


def test_unknown_sku_is_unpriced() -> None:
    est = estimate_cluster_cost(node_sku="Standard_Imaginary_v9", node_count=5, running=True)
    assert est.priced is False
    assert est.hourly_usd == 0.0
    assert est.projected_monthly_usd == 0.0


def test_stopped_cluster_zero_hourly() -> None:
    est = estimate_cluster_cost(
        node_sku="Standard_E16s_v5", node_count=10, uptime_seconds=7200, running=False
    )
    assert est.hourly_usd == 0.0
    assert est.accrued_usd is None


def test_is_estimate_flag_and_priced_as_of() -> None:
    d = estimate_cluster_cost(node_sku="Standard_E16s_v5", node_count=1).as_dict()
    assert d["is_estimate"] is True
    assert d["priced_as_of"]

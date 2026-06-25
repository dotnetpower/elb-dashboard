"""Tests for the live Azure Retail Prices pricing helper.

Responsibility: Cover the on-demand line-item filter, the gate, malformed-input
rejection, and the hit/negative cache — all without a real HTTP call.
Edit boundaries: Test-only; monkeypatches ``_fetch`` and the env gate.
Key entry points: pytest test functions.
Risky contracts: only Linux on-demand hourly items are priced; misses are cached.
Validation: ``uv run pytest -q api/tests/test_cost_pricing.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.cost import pricing


def _item(**over: Any) -> dict[str, Any]:
    base = {
        "type": "Consumption",
        "unitOfMeasure": "1 Hour",
        "productName": "Virtual Machines Es v5 Series",
        "skuName": "E16s v5",
        "meterName": "E16s v5",
        "retailPrice": 1.2,
    }
    base.update(over)
    return base


def test_pick_lowest_linux_on_demand() -> None:
    items = [
        _item(retailPrice=1.2),
        _item(productName="Virtual Machines Es v5 Series Windows", retailPrice=2.5),
        _item(skuName="E16s v5 Spot", retailPrice=0.3),
        _item(retailPrice=1.0, reservationTerm="1 Year"),
        _item(skuName="E16s v5 Low Priority", retailPrice=0.2),
        _item(retailPrice=1.1),
    ]
    assert pricing._pick_linux_on_demand_price(items) == 1.1


def test_pick_none_when_no_match() -> None:
    assert pricing._pick_linux_on_demand_price([{"type": "Reservation"}]) is None
    assert pricing._pick_linux_on_demand_price([_item(unitOfMeasure="1 Month")]) is None


def test_gate_off_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COST_PRICING_LIVE", raising=False)
    assert pricing.live_hourly_price_usd("Standard_E16s_v5", "koreacentral") is None


def test_malformed_inputs_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_PRICING_LIVE", "true")
    pricing.reset_cache()
    assert pricing.live_hourly_price_usd("bad sku!", "koreacentral") is None
    assert pricing.live_hourly_price_usd("Standard_E16s_v5", "bad region!") is None


def test_hit_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_PRICING_LIVE", "true")
    pricing.reset_cache()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(pricing, "_fetch", lambda s, r: (calls.append((s, r)), 1.5)[1])
    assert pricing.live_hourly_price_usd("Standard_E16s_v5", "koreacentral") == 1.5
    assert pricing.live_hourly_price_usd("Standard_E16s_v5", "koreacentral") == 1.5
    assert len(calls) == 1  # second call served from cache


def test_negative_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_PRICING_LIVE", "true")
    pricing.reset_cache()
    calls: list[int] = []
    monkeypatch.setattr(pricing, "_fetch", lambda s, r: (calls.append(1), None)[1])
    assert pricing.live_hourly_price_usd("Standard_E16s_v5", "koreacentral") is None
    assert pricing.live_hourly_price_usd("Standard_E16s_v5", "koreacentral") is None
    assert len(calls) == 1  # miss is cached

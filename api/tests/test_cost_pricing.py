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


def test_cache_is_size_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-process cache must never grow past its size cap, so a caller
    that enumerates many region/SKU pairs cannot leak memory."""
    monkeypatch.setenv("COST_PRICING_LIVE", "true")
    monkeypatch.setattr(pricing, "_CACHE_MAX_ENTRIES", 16)
    pricing.reset_cache()
    monkeypatch.setattr(pricing, "_fetch", lambda s, r: 1.0)
    for i in range(200):
        pricing.live_hourly_price_usd(f"Standard_E{i}s_v5", "koreacentral")
    assert len(pricing._CACHE) <= 16


def test_cache_reclaims_expired_before_evicting_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When over cap, expired entries are reclaimed first (respecting each
    entry's own TTL) before any still-valid entry is dropped."""
    monkeypatch.setenv("COST_PRICING_LIVE", "true")
    monkeypatch.setattr(pricing, "_CACHE_MAX_ENTRIES", 4)
    pricing.reset_cache()
    now = pricing._now()
    # Seed 4 stale entries whose 1h negative TTL has long passed.
    with pricing._CACHE_LOCK:
        for i in range(4):
            pricing._CACHE[(f"stale{i}", "r")] = (None, now - 10_000)
    # Insert a fresh entry -> over cap -> stale ones reclaimed, fresh one kept.
    pricing._cache_put(("fresh", "r"), 2.0)
    assert ("fresh", "r") in pricing._CACHE
    assert len(pricing._CACHE) <= 4


def test_int_env_falls_back_on_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed override must not crash import — it falls back to default."""
    monkeypatch.setenv("COST_PRICING_CACHE_MAX_ENTRIES", "not-a-number")
    assert pricing._int_env(
        "COST_PRICING_CACHE_MAX_ENTRIES", 512, minimum=64, maximum=100_000
    ) == 512
    monkeypatch.delenv("COST_PRICING_CACHE_MAX_ENTRIES", raising=False)
    assert pricing._int_env(
        "COST_PRICING_CACHE_MAX_ENTRIES", 512, minimum=64, maximum=100_000
    ) == 512


def test_int_env_clamps_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over/under-range overrides are clamped, so the cap can never be defeated."""
    monkeypatch.setenv("COST_PRICING_CACHE_MAX_ENTRIES", "5")
    assert (
        pricing._int_env("COST_PRICING_CACHE_MAX_ENTRIES", 512, minimum=64, maximum=100_000)
        == 64
    )
    monkeypatch.setenv("COST_PRICING_CACHE_MAX_ENTRIES", "9999999")
    assert (
        pricing._int_env("COST_PRICING_CACHE_MAX_ENTRIES", 512, minimum=64, maximum=100_000)
        == 100_000
    )


def test_cache_batch_eviction_keeps_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single put that pushes the cache far over cap drops the oldest-fetched
    entries in one pass and keeps the newest, including the just-inserted key."""
    monkeypatch.setenv("COST_PRICING_LIVE", "true")
    monkeypatch.setattr(pricing, "_CACHE_MAX_ENTRIES", 10)
    pricing.reset_cache()
    now = pricing._now()
    # Seed 50 still-valid (non-expired) entries with strictly increasing age.
    with pricing._CACHE_LOCK:
        for i in range(50):
            pricing._CACHE[(f"k{i}", "r")] = (1.0, now - (50 - i))  # k0 oldest, k49 newest
    pricing._cache_put(("newest", "r"), 3.0)
    assert len(pricing._CACHE) == 10
    assert ("newest", "r") in pricing._CACHE  # just-inserted survives
    assert ("k0", "r") not in pricing._CACHE  # oldest dropped
    assert ("k49", "r") in pricing._CACHE  # newest seed survives

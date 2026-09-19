"""Tests for the live Azure Retail Prices pricing helper.

Responsibility: Cover the on-demand line-item filter, the gate, malformed-input
rejection, and the hit/negative cache — all without a real HTTP call.
Edit boundaries: Test-only; monkeypatches ``_fetch`` and the env gate.
Key entry points: pytest test functions.
Risky contracts: only Linux on-demand hourly items are priced; misses are cached.
Validation: ``uv run pytest -q api/tests/test_cost_pricing.py``.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
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


def test_shared_cache_hit_avoids_fetch_and_hydrates_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COST_PRICING_LIVE", "true")
    pricing.reset_cache()
    key = ("Standard_E16s_v5", "koreacentral")
    monkeypatch.setattr(pricing, "_shared_cache_get", lambda _key: (True, 1.25))
    monkeypatch.setattr(
        pricing,
        "_fetch",
        lambda *_args: pytest.fail("shared cache hit must not call Retail API"),
    )

    assert pricing.live_hourly_price_usd(*key) == 1.25
    assert pricing._cache_get(key) == (True, 1.25)


def test_shared_cache_uses_hit_and_negative_ttls(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.writes: list[tuple[str, int, str]] = []

        def setex(self, key: str, ttl: int, value: str) -> None:
            self.writes.append((key, ttl, value))

    fake = FakeRedis()
    pricing.reset_cache()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client",
        lambda **_kwargs: fake,
    )

    pricing._shared_cache_put(("Standard_E16s_v5", "koreacentral"), 1.5)
    pricing._shared_cache_put(("Standard_E32s_v5", "koreacentral"), None)

    assert fake.writes[0][1] == 86_400
    assert json.loads(fake.writes[0][2]) == {"price": 1.5}
    assert fake.writes[1][1] == 900
    assert json.loads(fake.writes[1][2]) == {"price": None}


def test_shared_cache_failure_preserves_fetch_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COST_PRICING_LIVE", "true")
    pricing.reset_cache()
    monkeypatch.setattr(pricing, "_shared_cache_get", lambda _key: (False, None))
    monkeypatch.setattr(pricing, "_shared_cache_put", lambda _key, _value: None)
    monkeypatch.setattr(pricing, "_fetch", lambda _sku, _region: 2.0)

    assert pricing.live_hourly_price_usd("Standard_E16s_v5", "koreacentral") == 2.0


def test_shared_cache_failure_enters_bounded_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExplodingRedis:
        def get(self, _key: str) -> None:
            raise ConnectionError("redis unavailable")

    pricing.reset_cache()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client",
        lambda **_kwargs: ExplodingRedis(),
    )

    assert pricing._shared_cache_get(("Standard_E16s_v5", "koreacentral")) == (
        False,
        None,
    )
    assert pricing._shared_cache_available() is False


def test_shared_cache_cooldown_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(pricing, "_now", lambda: clock[0])
    pricing.reset_cache()

    assert pricing._mark_shared_cache_unavailable() is True
    assert pricing._mark_shared_cache_unavailable() is False
    assert pricing._shared_cache_available() is False
    clock[0] += pricing._REDIS_FAILURE_COOLDOWN_SECONDS
    assert pricing._shared_cache_available() is True


def test_shared_cache_rejects_oversized_or_invalid_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRedis:
        def __init__(self, value: object) -> None:
            self.value = value

        def get(self, _key: str) -> object:
            return self.value

    pricing.reset_cache()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client",
        lambda **_kwargs: FakeRedis(b"x" * 129),
    )
    assert pricing._shared_cache_get(("Standard_E16s_v5", "koreacentral")) == (
        False,
        None,
    )

    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client",
        lambda **_kwargs: FakeRedis(b'{"price":true}'),
    )
    assert pricing._shared_cache_get(("Standard_E16s_v5", "koreacentral")) == (
        False,
        None,
    )


def test_same_key_concurrent_miss_fetches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_PRICING_LIVE", "true")
    pricing.reset_cache()
    calls: list[int] = []
    monkeypatch.setattr(pricing, "_shared_cache_get", lambda _key: (False, None))
    monkeypatch.setattr(pricing, "_shared_cache_put", lambda _key, _value: None)

    def fetch(_sku: str, _region: str) -> float:
        calls.append(1)
        time.sleep(0.05)
        return 1.5

    monkeypatch.setattr(pricing, "_fetch", fetch)
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(
            executor.map(
                lambda _index: pricing.live_hourly_price_usd("Standard_E16s_v5", "koreacentral"),
                range(8),
            )
        )

    assert values == [1.5] * 8
    assert len(calls) == 1


def test_fetch_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    peak = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"Items": [_item(retailPrice=1.0)]}

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                time.sleep(0.05)
                return FakeResponse()
            finally:
                active -= 1

    monkeypatch.setattr("httpx.Client", FakeClient)
    with ThreadPoolExecutor(max_workers=12) as executor:
        values = list(
            executor.map(
                lambda index: pricing._fetch(f"Standard_E{index}s_v5", "koreacentral"),
                range(12),
            )
        )

    assert values == [1.0] * 12
    assert peak == pricing._FETCH_MAX_CONCURRENCY


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
    assert (
        pricing._int_env("COST_PRICING_CACHE_MAX_ENTRIES", 512, minimum=64, maximum=100_000) == 512
    )
    monkeypatch.delenv("COST_PRICING_CACHE_MAX_ENTRIES", raising=False)
    assert (
        pricing._int_env("COST_PRICING_CACHE_MAX_ENTRIES", 512, minimum=64, maximum=100_000) == 512
    )


def test_int_env_clamps_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over/under-range overrides are clamped, so the cap can never be defeated."""
    monkeypatch.setenv("COST_PRICING_CACHE_MAX_ENTRIES", "5")
    assert (
        pricing._int_env("COST_PRICING_CACHE_MAX_ENTRIES", 512, minimum=64, maximum=100_000) == 64
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

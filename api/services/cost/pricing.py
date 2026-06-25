"""Live VM hourly pricing via the public Azure Retail Prices API (opt-in).

Responsibility: Fetch the Linux on-demand hourly USD price for a VM SKU in a
region from the public, no-auth Azure Retail Prices API, with an in-process TTL
cache and a short negative cache. Gated behind ``COST_PRICING_LIVE`` (default OFF)
so the dashboard makes no external call unless an operator opts in.
Edit boundaries: This is the only module that talks to ``prices.azure.com``. It
returns ``None`` on any fault / miss; the caller (``estimate.py``) falls back to
the static price map. No Azure SDK, no Storage.
Key entry points: ``pricing_live_enabled``, ``live_hourly_price_usd``.
Risky contracts: Only Consumption (on-demand), "1 Hour", non-Windows, non-Spot,
non-reserved line items are considered — picking the wrong item would yield a
wrong price, so the filter is strict and the lowest matching price is used. The
cache is per-process (single api replica); a None result is cached briefly so a
transient outage does not hammer the API.
Validation: ``uv run pytest -q api/tests/test_cost_pricing.py``.
"""

from __future__ import annotations

import logging
import os
import re
import time
from threading import Lock

LOGGER = logging.getLogger(__name__)

_RETAIL_URL = "https://prices.azure.com/api/retail/prices"
_TIMEOUT_SECONDS = 5.0
_CACHE_TTL_SECONDS = 86_400  # 24h for a real price
_NEG_CACHE_TTL_SECONDS = 3_600  # 1h for a miss/fault, so we retry sooner
_HTTP_PAGE_CAP = 200  # never read more than this many line items

# ARM SKU / region identifiers are alphanumerics + a few separators. Validate
# before interpolating into the OData filter so a malformed value cannot inject.
_IDENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_CACHE: dict[tuple[str, str], tuple[float | None, float]] = {}
_CACHE_LOCK = Lock()


def pricing_live_enabled() -> bool:
    """Master gate. Default OFF — no external call unless explicitly enabled."""
    return os.environ.get("COST_PRICING_LIVE", "").strip().lower() in {"1", "true", "yes", "on"}


def _now() -> float:
    return time.time()


def _cache_get(key: tuple[str, str]) -> tuple[bool, float | None]:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if entry is None:
        return False, None
    value, fetched_at = entry
    ttl = _CACHE_TTL_SECONDS if value is not None else _NEG_CACHE_TTL_SECONDS
    if _now() - fetched_at >= ttl:
        return False, None
    return True, value


def _cache_put(key: tuple[str, str], value: float | None) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (value, _now())


def reset_cache() -> None:
    """Test hook."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _pick_linux_on_demand_price(items: list[dict[str, object]]) -> float | None:
    """Return the lowest Linux on-demand hourly USD price among the line items."""
    candidates: list[float] = []
    for item in items[:_HTTP_PAGE_CAP]:
        if str(item.get("type") or "") != "Consumption":
            continue
        if str(item.get("unitOfMeasure") or "") != "1 Hour":
            continue
        if item.get("reservationTerm"):
            continue
        product = str(item.get("productName") or "")
        sku_name = str(item.get("skuName") or "")
        meter = str(item.get("meterName") or "")
        if "Windows" in product:
            continue
        if "Spot" in sku_name or "Spot" in meter or "Low Priority" in sku_name:
            continue
        price = item.get("retailPrice")
        if not isinstance(price, (int, float)):
            price = item.get("unitPrice")
        if isinstance(price, (int, float)) and price > 0:
            candidates.append(float(price))
    if not candidates:
        return None
    return min(candidates)


def _fetch(sku: str, region: str) -> float | None:
    odata = (
        "serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{region}' "
        f"and armSkuName eq '{sku}' "
        "and priceType eq 'Consumption'"
    )
    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.get(
                _RETAIL_URL,
                params={"$filter": odata, "currencyCode": "USD"},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        LOGGER.info(
            "retail pricing fetch failed sku=%s region=%s: %s",
            sku,
            region,
            type(exc).__name__,
        )
        return None
    items = payload.get("Items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    return _pick_linux_on_demand_price(items)


def live_hourly_price_usd(sku: str, region: str) -> float | None:
    """Return the cached/live Linux on-demand hourly USD price, or ``None``.

    No-op (returns ``None``) when the gate is off or the SKU/region is malformed.
    Caches both hits and misses so a transient API outage does not hammer the
    endpoint on every dashboard poll.
    """
    if not pricing_live_enabled():
        return None
    sku = (sku or "").strip()
    region = (region or "").strip()
    if not _IDENT_RE.match(sku) or not _IDENT_RE.match(region):
        return None
    key = (sku, region)
    hit, value = _cache_get(key)
    if hit:
        return value
    price = _fetch(sku, region)
    _cache_put(key, price)
    return price

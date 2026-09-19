"""Live VM hourly pricing via the public Azure Retail Prices API (opt-in).

Responsibility: Fetch the Linux on-demand hourly USD price for a VM SKU in a
region from the public, no-auth Azure Retail Prices API, with process-local and
best-effort shared Redis caches. Gated behind ``COST_PRICING_LIVE`` (default
OFF) so the dashboard makes no external call unless an operator opts in.
Edit boundaries: This is the only module that talks to ``prices.azure.com``. It
returns ``None`` on any fault / miss; the caller (``estimate.py``) falls back to
the static price map. No Azure SDK, no Storage.
Key entry points: ``pricing_live_enabled``, ``live_hourly_price_usd``.
Risky contracts: Only Consumption (on-demand), "1 Hour", non-Windows, non-Spot,
non-reserved line items are considered — picking the wrong item would yield a
wrong price, so the filter is strict and the lowest matching price is used.
Redis is never authoritative: malformed data or an unavailable sidecar falls
back to the process cache and Retail API. A None result is cached briefly so a
transient outage does not hammer the API.
Validation: ``uv run pytest -q api/tests/test_cost_pricing.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from threading import BoundedSemaphore, Lock

LOGGER = logging.getLogger(__name__)

_RETAIL_URL = "https://prices.azure.com/api/retail/prices"
_TIMEOUT_SECONDS = 5.0
_CACHE_TTL_SECONDS = 86_400  # 24h for a real price
_NEG_CACHE_TTL_SECONDS = 900  # 15m for a miss/fault, so new SKUs recover promptly
_HTTP_PAGE_CAP = 200  # never read more than this many line items
_REDIS_KEY_PREFIX = "elb:cost:retail-price:v1:"
_REDIS_TIMEOUT_SECONDS = 0.25
_REDIS_FAILURE_COOLDOWN_SECONDS = 30.0
_REDIS_VALUE_MAX_BYTES = 128
_FETCH_LOCK_STRIPES = 64
_FETCH_MAX_CONCURRENCY = 4
_FETCH_SLOT_WAIT_SECONDS = 0.25

# ARM SKU / region identifiers are alphanumerics + a few separators. Validate
# before interpolating into the OData filter so a malformed value cannot inject.
_IDENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Parse a positive-int env override, clamped to ``[minimum, maximum]``.

    Falls back to ``default`` on a missing or malformed value (and clamps an
    out-of-range one) so a bad operator override can never crash the api
    sidecar at import time or re-open unbounded growth.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        LOGGER.warning("invalid %s=%r; using default %d", name, raw, default)
        return default
    return max(minimum, min(maximum, value))


# Hard size cap so the per-process cache can never grow without bound. The key
# cardinality (region × SKU) is finite and small in practice, but this is the
# only unbounded module-level cache in the api sidecar; the cap makes a leak
# structurally impossible even if a future caller enumerates many regions/SKUs.
# Clamped so a malformed/huge override cannot crash import or defeat the cap.
_CACHE_MAX_ENTRIES = _int_env("COST_PRICING_CACHE_MAX_ENTRIES", 512, minimum=64, maximum=100_000)

_CACHE: dict[tuple[str, str], tuple[float | None, float]] = {}
_CACHE_LOCK = Lock()
_REDIS_STATE_LOCK = Lock()
_REDIS_DISABLED_UNTIL = 0.0
_FETCH_LOCKS = tuple(Lock() for _ in range(_FETCH_LOCK_STRIPES))
_FETCH_SEMAPHORE = BoundedSemaphore(_FETCH_MAX_CONCURRENCY)


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


def _evict_over_cap_locked() -> None:
    """Keep ``_CACHE`` at or below the size cap. Caller holds ``_CACHE_LOCK``.

    Reclaim already-expired entries first (respecting each entry's own TTL —
    24h for a real price, 1h for a miss), which is free and usually enough. If
    still over cap, drop the oldest-fetched entries in a single sorted pass
    (O(n log n), not the O(n^2) of a repeated min-scan). A dropped-but-still-
    valid entry is benign: the next lookup simply re-fetches.
    """
    if len(_CACHE) <= _CACHE_MAX_ENTRIES:
        return
    now = _now()
    expired = [
        key
        for key, (value, fetched_at) in _CACHE.items()
        if now - fetched_at >= (_CACHE_TTL_SECONDS if value is not None else _NEG_CACHE_TTL_SECONDS)
    ]
    for key in expired:
        _CACHE.pop(key, None)
    overflow = len(_CACHE) - _CACHE_MAX_ENTRIES
    if overflow > 0:
        for key, _entry in sorted(_CACHE.items(), key=lambda kv: kv[1][1])[:overflow]:
            _CACHE.pop(key, None)


def _cache_put(key: tuple[str, str], value: float | None) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (value, _now())
        _evict_over_cap_locked()


def _shared_cache_key(key: tuple[str, str]) -> str:
    sku, region = key
    return f"{_REDIS_KEY_PREFIX}{region.casefold()}:{sku.casefold()}"


def _shared_cache_available() -> bool:
    with _REDIS_STATE_LOCK:
        return _now() >= _REDIS_DISABLED_UNTIL


def _mark_shared_cache_unavailable() -> bool:
    """Open the Redis cooldown and return True only for the first failure."""
    global _REDIS_DISABLED_UNTIL
    now = _now()
    with _REDIS_STATE_LOCK:
        if now < _REDIS_DISABLED_UNTIL:
            return False
        _REDIS_DISABLED_UNTIL = now + _REDIS_FAILURE_COOLDOWN_SECONDS
        return True


def _shared_cache_get(key: tuple[str, str]) -> tuple[bool, float | None]:
    if not _shared_cache_available():
        return False, None
    try:
        from api.services.redis_clients import get_ops_redis_client

        client = get_ops_redis_client(
            socket_timeout=_REDIS_TIMEOUT_SECONDS,
            socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
        )
        raw = client.get(_shared_cache_key(key))
        if raw is None:
            return False, None
        if len(raw) > _REDIS_VALUE_MAX_BYTES:
            return False, None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or "price" not in payload:
            return False, None
        price = payload["price"]
        if price is None:
            return True, None
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
            return False, None
        return True, float(price)
    except Exception as exc:
        if _mark_shared_cache_unavailable():
            LOGGER.warning("retail pricing redis unavailable: %s", type(exc).__name__)
        return False, None


def _shared_cache_put(key: tuple[str, str], value: float | None) -> None:
    if not _shared_cache_available():
        return
    try:
        from api.services.redis_clients import get_ops_redis_client

        client = get_ops_redis_client(
            socket_timeout=_REDIS_TIMEOUT_SECONDS,
            socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
        )
        ttl = _CACHE_TTL_SECONDS if value is not None else _NEG_CACHE_TTL_SECONDS
        client.setex(
            _shared_cache_key(key),
            ttl,
            json.dumps({"price": value}, separators=(",", ":")),
        )
    except Exception as exc:
        if _mark_shared_cache_unavailable():
            LOGGER.warning("retail pricing redis unavailable: %s", type(exc).__name__)


def reset_cache() -> None:
    """Test hook."""
    global _REDIS_DISABLED_UNTIL
    with _CACHE_LOCK:
        _CACHE.clear()
    with _REDIS_STATE_LOCK:
        _REDIS_DISABLED_UNTIL = 0.0


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
    if not _FETCH_SEMAPHORE.acquire(timeout=_FETCH_SLOT_WAIT_SECONDS):
        LOGGER.info("retail pricing fetch concurrency limit reached")
        return None
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
    finally:
        _FETCH_SEMAPHORE.release()
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
    fetch_lock = _FETCH_LOCKS[hash(key) % len(_FETCH_LOCKS)]
    if not fetch_lock.acquire(timeout=_TIMEOUT_SECONDS):
        LOGGER.info(
            "retail pricing single-flight wait timed out sku=%s region=%s",
            sku,
            region,
        )
        return None
    try:
        hit, value = _cache_get(key)
        if hit:
            return value
        hit, value = _shared_cache_get(key)
        if hit:
            _cache_put(key, value)
            return value
        price = _fetch(sku, region)
        _cache_put(key, price)
        _shared_cache_put(key, price)
        return price
    finally:
        fetch_lock.release()

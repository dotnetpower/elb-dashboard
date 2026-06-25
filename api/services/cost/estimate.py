"""Approximate compute-cost estimation for an AKS cluster (no Azure billing API).

Responsibility: Turn a node SKU + node count + uptime into a coarse USD cost
estimate using a hardcoded, dated hourly price map. This is an APPROXIMATION for
operator situational awareness only — it deliberately ignores Spot/reserved
pricing, the system node pool, storage, egress, and regional variation. Authoritative
spend lives in Azure Cost Management, never here.
Edit boundaries: Pure calculation + a static price map. No Azure SDK, no HTTP, no
Storage. When the price map is refreshed, bump ``PRICED_AS_OF`` in the same change.
Key entry points: ``estimate_cluster_cost``, ``hourly_price_usd``.
Risky contracts: ``priced`` is False for an unknown SKU so the UI can show "price
unavailable" instead of a wrong number. Prices are pay-as-you-go Linux on-demand,
rough koreacentral-ish values — never present them as exact.
Validation: ``uv run pytest -q api/tests/test_cost_estimate.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

# Date the price map below was last eyeballed. Bump on every refresh. Surfaced to
# the UI so a stale estimate is honest about its vintage.
PRICED_AS_OF = "2026-06"

# Rough pay-as-you-go Linux on-demand hourly USD per VM SKU. These are coarse,
# region-agnostic approximations for a situational-awareness estimate — NOT a
# billing source. Unknown SKUs return ``priced=False`` rather than a guess.
_SKU_HOURLY_USD: dict[str, float] = {
    # D-series (general purpose)
    "Standard_D2s_v5": 0.096,
    "Standard_D4s_v5": 0.192,
    "Standard_D8s_v5": 0.384,
    "Standard_D16s_v5": 0.768,
    "Standard_D32s_v5": 1.536,
    "Standard_D64s_v3": 3.072,
    "Standard_D2as_v7": 0.096,
    "Standard_D4as_v7": 0.192,
    # E-series (memory optimised — typical BLAST node pool)
    "Standard_E8s_v5": 0.504,
    "Standard_E16s_v5": 1.008,
    "Standard_E32s_v5": 2.016,
    "Standard_E48s_v5": 3.024,
    "Standard_E64s_v5": 4.032,
    "Standard_E96s_v5": 6.048,
    "Standard_E16s_v3": 1.008,
    "Standard_E16as_v7": 1.008,
    "Standard_E32as_v7": 2.016,
    "Standard_E48as_v7": 3.024,
}

_HOURS_PER_MONTH = 730.0  # Azure's billing convention (365*24/12)


def hourly_price_usd(node_sku: str) -> float | None:
    """Return the hourly USD price for a SKU, or ``None`` when unknown."""
    return _SKU_HOURLY_USD.get((node_sku or "").strip())


@dataclass(frozen=True)
class CostEstimate:
    sku: str
    node_count: int
    priced: bool
    hourly_usd: float
    uptime_seconds: int | None
    accrued_usd: float | None
    projected_monthly_usd: float
    priced_as_of: str
    is_estimate: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "node_count": self.node_count,
            "priced": self.priced,
            "hourly_usd": round(self.hourly_usd, 4),
            "uptime_seconds": self.uptime_seconds,
            "accrued_usd": None if self.accrued_usd is None else round(self.accrued_usd, 4),
            "projected_monthly_usd": round(self.projected_monthly_usd, 2),
            "priced_as_of": self.priced_as_of,
            "is_estimate": self.is_estimate,
        }


def estimate_cluster_cost(
    *,
    node_sku: str,
    node_count: int,
    uptime_seconds: int | None = None,
    running: bool = True,
) -> CostEstimate:
    """Estimate the running compute cost of a cluster's workload node pool.

    ``hourly_usd`` = node_count × SKU price when running and priced, else 0.
    ``accrued_usd`` is computed only when ``uptime_seconds`` is known. The
    estimate is intentionally coarse and labelled ``is_estimate`` so the UI never
    presents it as a bill.
    """
    sku = (node_sku or "").strip()
    count = max(0, int(node_count or 0))
    unit = hourly_price_usd(sku)
    priced = unit is not None
    effective_unit = unit if priced else 0.0

    hourly = effective_unit * count if running else 0.0
    accrued: float | None = None
    if uptime_seconds is not None and uptime_seconds >= 0 and running:
        accrued = hourly * (uptime_seconds / 3600.0)
    projected_monthly = hourly * _HOURS_PER_MONTH

    return CostEstimate(
        sku=sku,
        node_count=count,
        priced=priced,
        hourly_usd=hourly,
        uptime_seconds=uptime_seconds,
        accrued_usd=accrued,
        projected_monthly_usd=projected_monthly,
        priced_as_of=PRICED_AS_OF,
    )

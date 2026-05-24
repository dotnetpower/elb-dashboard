"""Allowed AKS node SKUs for ElasticBLAST on Azure.

Responsibility: Allowed AKS node SKUs for ElasticBLAST on Azure
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `SkuSpec`, `SkuListResponse`, `SkuCatalogEntry`, `is_allowed`, `list_skus`,
`sku_list_response`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

SkuRole = Literal["system", "blast", "both"]
"""Which AKS node pool the SKU is intended for.

* ``system`` — only suitable for the small ``systempool`` (≤2 vCPU class).
* ``blast``  — only suitable for the workload ``blastpool`` (HPC / large
  memory / large storage SKUs).
* ``both``   — general-purpose SKUs that can run either pool.
"""


class SkuSpec(TypedDict):
    """Public shape returned by ``/api/aks/skus``."""

    name: str
    vCPUs: int
    memoryGiB: int
    category: str
    series: str
    hourlyUsd: float
    role: SkuRole
    group: str


class SkuListResponse(TypedDict):
    """HTTP response shape for ``GET /api/aks/skus``."""

    skus: list[SkuSpec]
    default: str
    default_sku: str
    default_system_sku: str
    group_labels: dict[str, str]
    group_order: list[str]
    degraded: bool
    degraded_reason: str


# Display label per SKU group. Stable strings — the SPA dropdown uses
# them as <optgroup label=...> so they are user-visible.
SKU_GROUP_LABELS: dict[str, str] = {
    "system": "System pool (D-series, 2-4 vCPU)",
    "system-as-v7": "System pool (D as v7, 2-4 vCPU)",
    "hpc": "HPC — InfiniBand (HB / HC)",
    "memory-v5": "Memory-optimised — E v5",
    "memory-as-v7": "Memory-optimised — E as v7",
    "memory-bs-v5": "Memory-optimised + NVMe — E bs v5",
    "memory-v3": "Memory-optimised — E v3",
    "general": "General purpose — D v3",
    "storage-v3": "Storage-optimised — L v3",
    "storage-as-v3": "Storage-optimised — L as v3",
}

# Stable ordering for the SPA dropdown <optgroup>s. Anything not listed
# here is appended in catalog order.
SKU_GROUP_ORDER: tuple[str, ...] = (
    "system",
    "system-as-v7",
    "hpc",
    "memory-v5",
    "memory-as-v7",
    "memory-bs-v5",
    "memory-v3",
    "general",
    "storage-v3",
    "storage-as-v3",
)


@dataclass(frozen=True, slots=True)
class SkuCatalogEntry:
    """Internal catalog row. Everything else is derived from this."""

    name: str
    vcpus: int
    memory_gib: int
    category: str
    series: str
    hourly_usd: float
    role: SkuRole
    group: str

    def to_public(self) -> SkuSpec:
        return {
            "name": self.name,
            "vCPUs": self.vcpus,
            "memoryGiB": self.memory_gib,
            "category": self.category,
            "series": self.series,
            "hourlyUsd": self.hourly_usd,
            "role": self.role,
            "group": self.group,
        }


def _sku(
    name: str,
    vcpus: int,
    memory_gib: int,
    category: str,
    series: str,
    hourly_usd: float,
    *,
    role: SkuRole = "blast",
    group: str = "",
) -> SkuCatalogEntry:
    return SkuCatalogEntry(
        name=name,
        vcpus=vcpus,
        memory_gib=memory_gib,
        category=category,
        series=series,
        hourly_usd=hourly_usd,
        role=role,
        group=group or category,
    )


# Sibling defaults: constants.py.
DEFAULT_SKU: str = "Standard_E32s_v5"  # ELB_DFLT_AZURE_MACHINE_TYPE
DEFAULT_SYSTEM_SKU: str = "Standard_D2s_v3"  # ELB_DFLT_AZURE_SYSTEM_VM_SIZE


# Mirror of sibling azure_traits.py::AZURE_HPC_MACHINES plus matching
# azure_traits.py::AZURE_VM_HOURLY_PRICES, with system-pool SKUs from
# sibling constants.py. Ordering is UI dropdown ordering.
SKU_CATALOG: tuple[SkuCatalogEntry, ...] = (
    # --- System pool SKUs (small, low-cost, CriticalAddonsOnly) -----------
    _sku("Standard_D2s_v3", 2, 8, "general", "D-v3", 0.096, role="system", group="system"),
    _sku("Standard_D4s_v3", 4, 16, "general", "D-v3", 0.192, role="system", group="system"),
    _sku(
        "Standard_D2as_v7",
        2,
        8,
        "general",
        "D-as-v7",
        0.096,
        role="system",
        group="system-as-v7",
    ),
    _sku(
        "Standard_D4as_v7",
        4,
        16,
        "general",
        "D-as-v7",
        0.192,
        role="system",
        group="system-as-v7",
    ),
    # --- Blast pool SKUs ---------------------------------------------------
    _sku("Standard_HB120rs_v3", 120, 480, "hpc", "HB-v3", 3.600, group="hpc"),
    _sku("Standard_HC44rs", 44, 352, "hpc", "HC", 3.168, group="hpc"),
    _sku("Standard_HB60rs", 60, 240, "hpc", "HB-v2", 2.280, group="hpc"),
    _sku("Standard_D8s_v3", 8, 32, "general", "D-v3", 0.384, group="general"),
    _sku("Standard_D16s_v3", 16, 64, "general", "D-v3", 0.768, group="general"),
    _sku("Standard_D32s_v3", 32, 128, "general", "D-v3", 1.536, group="general"),
    _sku("Standard_D64s_v3", 64, 256, "general", "D-v3", 3.072, group="general"),
    _sku("Standard_E64s_v3", 64, 432, "memory", "E-v3", 3.629, group="memory-v3"),
    _sku(
        "Standard_E64is_v3", 64, 504, "memory-isolated", "E-v3-isolated", 3.629, group="memory-v3"
    ),
    _sku("Standard_E16s_v5", 16, 128, "memory", "E-v5", 1.008, group="memory-v5"),
    _sku("Standard_E32s_v5", 32, 256, "memory", "E-v5", 2.016, group="memory-v5"),
    _sku("Standard_E48s_v5", 48, 384, "memory", "E-v5", 3.024, group="memory-v5"),
    _sku("Standard_E64s_v5", 64, 512, "memory", "E-v5", 4.032, group="memory-v5"),
    _sku("Standard_E96s_v5", 96, 672, "memory", "E-v5", 6.048, group="memory-v5"),
    _sku("Standard_E16as_v7", 16, 128, "memory", "E-as-v7", 1.008, group="memory-as-v7"),
    _sku("Standard_E32as_v7", 32, 256, "memory", "E-as-v7", 2.016, group="memory-as-v7"),
    _sku("Standard_E48as_v7", 48, 384, "memory", "E-as-v7", 3.024, group="memory-as-v7"),
    _sku("Standard_E16bs_v5", 16, 128, "memory-nvme", "E-v5-bs", 1.192, group="memory-bs-v5"),
    _sku("Standard_E32bs_v5", 32, 256, "memory-nvme", "E-v5-bs", 2.432, group="memory-bs-v5"),
    _sku("Standard_E48bs_v5", 48, 384, "memory-nvme", "E-v5-bs", 3.576, group="memory-bs-v5"),
    _sku("Standard_E64bs_v5", 64, 512, "memory-nvme", "E-v5-bs", 4.864, group="memory-bs-v5"),
    _sku("Standard_E96bs_v5", 96, 672, "memory-nvme", "E-v5-bs", 7.296, group="memory-bs-v5"),
    _sku("Standard_L8s_v3", 8, 64, "storage", "L-v3", 0.624, group="storage-v3"),
    _sku("Standard_L16s_v3", 16, 128, "storage", "L-v3", 1.248, group="storage-v3"),
    _sku("Standard_L32s_v3", 32, 256, "storage", "L-v3", 2.496, group="storage-v3"),
    _sku("Standard_L48s_v3", 48, 384, "storage", "L-v3", 3.744, group="storage-v3"),
    _sku("Standard_L64s_v3", 64, 512, "storage", "L-v3", 4.992, group="storage-v3"),
    _sku("Standard_L80s_v3", 80, 640, "storage", "L-v3", 6.240, group="storage-v3"),
    _sku("Standard_L8as_v3", 8, 64, "storage", "L-v3-as", 0.624, group="storage-as-v3"),
    _sku("Standard_L16as_v3", 16, 128, "storage", "L-v3-as", 1.248, group="storage-as-v3"),
    _sku("Standard_L32as_v3", 32, 256, "storage", "L-v3-as", 2.496, group="storage-as-v3"),
    _sku("Standard_L48as_v3", 48, 384, "storage", "L-v3-as", 3.744, group="storage-as-v3"),
    _sku("Standard_L64as_v3", 64, 512, "storage", "L-v3-as", 4.992, group="storage-as-v3"),
    _sku("Standard_L80as_v3", 80, 640, "storage", "L-v3-as", 6.240, group="storage-as-v3"),
)

SKU_BY_NAME: dict[str, SkuCatalogEntry] = {sku.name: sku for sku in SKU_CATALOG}
_SKU_BY_CASEFOLD_NAME: dict[str, str] = {sku.name.casefold(): sku.name for sku in SKU_CATALOG}

# Backwards-compatible public module constants.
ALLOWED_SKUS: dict[str, SkuSpec] = {sku.name: sku.to_public() for sku in SKU_CATALOG}
AZURE_VM_HOURLY_USD: dict[str, float] = {sku.name: sku.hourly_usd for sku in SKU_CATALOG}


def normalize_sku_name(sku: str | None) -> str:
    """Return the catalog spelling for a SKU when it is recognisable."""

    raw = (sku or "").strip()
    if not raw:
        return ""
    if raw in SKU_BY_NAME:
        return raw

    folded = raw.casefold()
    canonical = _SKU_BY_CASEFOLD_NAME.get(folded)
    if canonical is not None:
        return canonical

    if not folded.startswith("standard_"):
        with_prefix = f"Standard_{raw}"
        canonical = _SKU_BY_CASEFOLD_NAME.get(with_prefix.casefold())
        if canonical is not None:
            return canonical

    return raw


def is_allowed(sku: str) -> bool:
    """Return True if ``sku`` is in the elastic-blast allow-list."""

    return normalize_sku_name(sku) in SKU_BY_NAME


def list_skus() -> list[SkuSpec]:
    """Return the allow-list as a list, suitable for JSON serialisation."""

    return [sku.to_public() for sku in SKU_CATALOG]


def sku_list_response(
    *,
    degraded: bool = True,
    degraded_reason: str = "static_skus_celery_task_pending",
) -> SkuListResponse:
    """Build the stable ``GET /api/aks/skus`` response payload.

    ``group_labels`` / ``group_order`` mirror the SPA dropdown grouping so
    the frontend never has to hardcode the per-series labels (see
    ``web/src/hooks/useAksSkus.ts``).
    """

    used_groups = {sku.group for sku in SKU_CATALOG}
    ordered = [g for g in SKU_GROUP_ORDER if g in used_groups]
    # Tail: any group not in the canonical order, in catalog order.
    seen = set(ordered)
    for sku in SKU_CATALOG:
        if sku.group not in seen:
            ordered.append(sku.group)
            seen.add(sku.group)

    return {
        "skus": list_skus(),
        "default": DEFAULT_SKU,
        "default_sku": DEFAULT_SKU,
        "default_system_sku": DEFAULT_SYSTEM_SKU,
        "group_labels": {g: SKU_GROUP_LABELS[g] for g in ordered},
        "group_order": ordered,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
    }


def _assert_catalog_consistency() -> None:
    names = [sku.name for sku in SKU_CATALOG]
    if len(names) != len(set(names)):
        raise AssertionError("SKU_CATALOG contains duplicate names")
    if DEFAULT_SKU not in SKU_BY_NAME:
        raise AssertionError(f"DEFAULT_SKU {DEFAULT_SKU!r} missing from SKU_CATALOG")
    if DEFAULT_SYSTEM_SKU not in SKU_BY_NAME:
        raise AssertionError(f"DEFAULT_SYSTEM_SKU {DEFAULT_SYSTEM_SKU!r} missing from SKU_CATALOG")
    if SKU_BY_NAME[DEFAULT_SYSTEM_SKU].role not in ("system", "both"):
        raise AssertionError(
            f"DEFAULT_SYSTEM_SKU {DEFAULT_SYSTEM_SKU!r} is not flagged as a system SKU"
        )
    if SKU_BY_NAME[DEFAULT_SKU].role not in ("blast", "both"):
        raise AssertionError(f"DEFAULT_SKU {DEFAULT_SKU!r} is not flagged as a blast SKU")
    if any(sku.hourly_usd <= 0 for sku in SKU_CATALOG):
        raise AssertionError("SKU_CATALOG contains a non-positive price")
    if set(ALLOWED_SKUS) != set(AZURE_VM_HOURLY_USD):
        raise AssertionError("SKU allow-list and pricing table drifted apart")
    # Every group used in the catalog must have a display label so the SPA
    # never renders a bare optgroup id.
    used_groups = {sku.group for sku in SKU_CATALOG}
    missing = used_groups - set(SKU_GROUP_LABELS)
    if missing:
        raise AssertionError(f"SKU groups missing display labels: {sorted(missing)}")


_assert_catalog_consistency()

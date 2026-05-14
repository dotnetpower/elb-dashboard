"""Allowed AKS node SKUs for ElasticBLAST on Azure.

Single source of truth in this repo. **Mirror of**
``src/elastic_blast/azure_traits.py::AZURE_HPC_MACHINES`` and
``ELB_DFLT_AZURE_MACHINE_TYPE`` in the sibling `elastic-blast-azure` repo.

If a user picks a SKU that is **not** in ``ALLOWED_SKUS`` the BLAST submit
will fail in the cluster with::

    NotImplementedError: Cannot get properties for <sku>

raised by ``elastic_blast.azure_traits.get_machine_properties``. So:

- Every dropdown / picker in the SPA must source its options from
  ``ALLOWED_SKUS`` (or the ``/api/aks/skus`` endpoint, which reads from here).
- Every default in the backend (job templates, cost estimator, etc.) must
  use ``DEFAULT_SKU``.
- When the sibling repo bumps ``AZURE_HPC_MACHINES`` or
  ``ELB_DFLT_AZURE_MACHINE_TYPE``, update this module in the same PR (just
  like ``image_tags.py``).
"""

from __future__ import annotations

from typing import TypedDict


class SkuSpec(TypedDict):
    """Public shape returned by ``/api/aks/skus``."""

    name: str
    vCPUs: int
    memoryGiB: int
    category: str
    series: str  # e.g. "E-v3", "E-v5", "E-v5-bs", "L-v3", "L-v3-as"


# Sibling default — keep in sync with
# ``src/elastic_blast/constants.py::ELB_DFLT_AZURE_MACHINE_TYPE``.
DEFAULT_SKU: str = "Standard_E32s_v5"

# Mirror of ``AZURE_HPC_MACHINES`` from
# ``src/elastic_blast/azure_traits.py``. Keep ordering grouped by series so
# the SPA dropdown reads naturally.
ALLOWED_SKUS: dict[str, SkuSpec] = {
    # E-series v3 (memory-optimized, prior generation)
    "Standard_E16s_v3": {
        "name": "Standard_E16s_v3",
        "vCPUs": 16,
        "memoryGiB": 128,
        "category": "memory",
        "series": "E-v3",
    },
    "Standard_E32s_v3": {
        "name": "Standard_E32s_v3",
        "vCPUs": 32,
        "memoryGiB": 256,
        "category": "memory",
        "series": "E-v3",
    },
    "Standard_E48s_v3": {
        "name": "Standard_E48s_v3",
        "vCPUs": 48,
        "memoryGiB": 384,
        "category": "memory",
        "series": "E-v3",
    },
    "Standard_E64s_v3": {
        "name": "Standard_E64s_v3",
        "vCPUs": 64,
        "memoryGiB": 432,
        "category": "memory",
        "series": "E-v3",
    },
    # E-series v5 (memory-optimized, current default — Ice Lake)
    "Standard_E16s_v5": {
        "name": "Standard_E16s_v5",
        "vCPUs": 16,
        "memoryGiB": 128,
        "category": "memory",
        "series": "E-v5",
    },
    "Standard_E32s_v5": {
        "name": "Standard_E32s_v5",
        "vCPUs": 32,
        "memoryGiB": 256,
        "category": "memory",
        "series": "E-v5",
    },
    "Standard_E48s_v5": {
        "name": "Standard_E48s_v5",
        "vCPUs": 48,
        "memoryGiB": 384,
        "category": "memory",
        "series": "E-v5",
    },
    "Standard_E64s_v5": {
        "name": "Standard_E64s_v5",
        "vCPUs": 64,
        "memoryGiB": 512,
        "category": "memory",
        "series": "E-v5",
    },
    "Standard_E96s_v5": {
        "name": "Standard_E96s_v5",
        "vCPUs": 96,
        "memoryGiB": 672,
        "category": "memory",
        "series": "E-v5",
    },
    # E-series v5 with NVMe (best for warmup/local-SSD mode)
    "Standard_E16bs_v5": {
        "name": "Standard_E16bs_v5",
        "vCPUs": 16,
        "memoryGiB": 128,
        "category": "memory-nvme",
        "series": "E-v5-bs",
    },
    "Standard_E32bs_v5": {
        "name": "Standard_E32bs_v5",
        "vCPUs": 32,
        "memoryGiB": 256,
        "category": "memory-nvme",
        "series": "E-v5-bs",
    },
    "Standard_E48bs_v5": {
        "name": "Standard_E48bs_v5",
        "vCPUs": 48,
        "memoryGiB": 384,
        "category": "memory-nvme",
        "series": "E-v5-bs",
    },
    "Standard_E64bs_v5": {
        "name": "Standard_E64bs_v5",
        "vCPUs": 64,
        "memoryGiB": 512,
        "category": "memory-nvme",
        "series": "E-v5-bs",
    },
    "Standard_E96bs_v5": {
        "name": "Standard_E96bs_v5",
        "vCPUs": 96,
        "memoryGiB": 672,
        "category": "memory-nvme",
        "series": "E-v5-bs",
    },
    # D-series v3 (general purpose) — only the sibling-allowed sizes
    "Standard_D8s_v3": {
        "name": "Standard_D8s_v3",
        "vCPUs": 8,
        "memoryGiB": 32,
        "category": "general",
        "series": "D-v3",
    },
    "Standard_D16s_v3": {
        "name": "Standard_D16s_v3",
        "vCPUs": 16,
        "memoryGiB": 64,
        "category": "general",
        "series": "D-v3",
    },
    "Standard_D32s_v3": {
        "name": "Standard_D32s_v3",
        "vCPUs": 32,
        "memoryGiB": 128,
        "category": "general",
        "series": "D-v3",
    },
    "Standard_D64s_v3": {
        "name": "Standard_D64s_v3",
        "vCPUs": 64,
        "memoryGiB": 256,
        "category": "general",
        "series": "D-v3",
    },
    # L-series v3 (storage-optimized, large NVMe — for TB-scale BLAST DB)
    "Standard_L8s_v3": {
        "name": "Standard_L8s_v3",
        "vCPUs": 8,
        "memoryGiB": 64,
        "category": "storage",
        "series": "L-v3",
    },
    "Standard_L16s_v3": {
        "name": "Standard_L16s_v3",
        "vCPUs": 16,
        "memoryGiB": 128,
        "category": "storage",
        "series": "L-v3",
    },
    "Standard_L32s_v3": {
        "name": "Standard_L32s_v3",
        "vCPUs": 32,
        "memoryGiB": 256,
        "category": "storage",
        "series": "L-v3",
    },
    "Standard_L64s_v3": {
        "name": "Standard_L64s_v3",
        "vCPUs": 64,
        "memoryGiB": 512,
        "category": "storage",
        "series": "L-v3",
    },
}


# Approximate Pay-As-You-Go hourly USD prices (koreacentral). Values come
# from sibling ``azure_traits.py::AZURE_VM_HOURLY_PRICES``. Only SKUs that
# also appear in ``ALLOWED_SKUS`` are listed; the cost estimator must reject
# anything outside this dict.
AZURE_VM_HOURLY_USD: dict[str, float] = {
    # D-v3
    "Standard_D8s_v3": 0.384,
    "Standard_D16s_v3": 0.768,
    "Standard_D32s_v3": 1.536,
    "Standard_D64s_v3": 3.072,
    # E-v3
    "Standard_E16s_v3": 1.008,
    "Standard_E32s_v3": 2.016,
    "Standard_E48s_v3": 3.024,
    "Standard_E64s_v3": 3.629,
    # E-v5
    "Standard_E16s_v5": 1.008,
    "Standard_E32s_v5": 2.016,
    "Standard_E48s_v5": 3.024,
    "Standard_E64s_v5": 4.032,
    "Standard_E96s_v5": 6.048,
    # E-v5 with NVMe
    "Standard_E16bs_v5": 1.192,
    "Standard_E32bs_v5": 2.432,
    "Standard_E48bs_v5": 3.648,
    "Standard_E64bs_v5": 4.864,
    "Standard_E96bs_v5": 7.296,
    # L-v3
    "Standard_L8s_v3": 0.624,
    "Standard_L16s_v3": 1.248,
    "Standard_L32s_v3": 2.496,
    "Standard_L64s_v3": 4.992,
}


def is_allowed(sku: str) -> bool:
    """Return True if ``sku`` is in the elastic-blast allow-list."""

    return sku in ALLOWED_SKUS


def list_skus() -> list[SkuSpec]:
    """Return the allow-list as a list, suitable for JSON serialisation."""

    return list(ALLOWED_SKUS.values())


# Self-check: the default we hand back to the SPA must be in the allow-list
# and have a price entry. A drift here means a sibling-repo bump was missed.
assert DEFAULT_SKU in ALLOWED_SKUS, (
    f"DEFAULT_SKU {DEFAULT_SKU!r} missing from ALLOWED_SKUS — "
    "out of sync with sibling azure_traits.py::AZURE_HPC_MACHINES"
)
assert DEFAULT_SKU in AZURE_VM_HOURLY_USD, (
    f"DEFAULT_SKU {DEFAULT_SKU!r} missing from AZURE_VM_HOURLY_USD"
)

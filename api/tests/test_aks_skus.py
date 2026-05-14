"""Tests for the AKS SKU allow-list source-of-truth.

These are guard-rails: if the sibling repo bumps its
``AZURE_HPC_MACHINES`` allow-list, the assertions below catch a stale mirror
in CI before users hit ``NotImplementedError`` at submit time.
"""

from __future__ import annotations

from api.services import aks_skus
from api.services.blast_config import AZURE_VM_HOURLY_USD


def test_default_sku_in_allow_list() -> None:
    assert aks_skus.DEFAULT_SKU in aks_skus.ALLOWED_SKUS


def test_default_sku_priced() -> None:
    assert aks_skus.DEFAULT_SKU in aks_skus.AZURE_VM_HOURLY_USD


def test_default_sku_matches_sibling_default() -> None:
    # Sibling: src/elastic_blast/constants.py::ELB_DFLT_AZURE_MACHINE_TYPE
    # Update both repos in lockstep.
    assert aks_skus.DEFAULT_SKU == "Standard_E32s_v5"


def test_blast_config_pricing_is_re_export() -> None:
    # blast_config.AZURE_VM_HOURLY_USD must be a copy of the allow-list
    # pricing so the cost estimator can never quote a SKU that elastic-blast
    # then refuses.
    assert AZURE_VM_HOURLY_USD == aks_skus.AZURE_VM_HOURLY_USD


def test_pricing_subset_of_allow_list() -> None:
    # No phantom prices for SKUs the user can't actually pick.
    extra = set(aks_skus.AZURE_VM_HOURLY_USD) - set(aks_skus.ALLOWED_SKUS)
    assert not extra, f"priced SKUs not in allow-list: {sorted(extra)}"


def test_list_skus_returns_all() -> None:
    assert len(aks_skus.list_skus()) == len(aks_skus.ALLOWED_SKUS)


def test_is_allowed_rejects_unknown() -> None:
    # SKUs the SPA used to expose but elastic-blast rejects.
    for bad in (
        "Standard_D2s_v5",
        "Standard_D8s_v5",
        "Standard_E4s_v5",
        "Standard_E8s_v5",
        "Standard_E20s_v5",
    ):
        assert not aks_skus.is_allowed(bad), bad


def test_is_allowed_accepts_default() -> None:
    assert aks_skus.is_allowed(aks_skus.DEFAULT_SKU)

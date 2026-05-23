"""Tests for the AKS SKU allow-list source-of-truth.

Responsibility: Tests for the AKS SKU allow-list source-of-truth
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_default_sku_in_allow_list`, `test_default_sku_priced`,
`test_default_sku_matches_sibling_default`,
`test_allowed_skus_match_sibling_azure_hpc_machines`, `test_blast_config_pricing_is_re_export`,
`test_pricing_subset_of_allow_list`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_aks_skus.py`.
"""

from __future__ import annotations

from api.services import aks_skus
from api.services.blast.config import AZURE_VM_HOURLY_USD
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

SIBLING_AZURE_HPC_MACHINES = (
    "Standard_HB120rs_v3",
    "Standard_HC44rs",
    "Standard_HB60rs",
    "Standard_D16s_v3",
    "Standard_D32s_v3",
    "Standard_D64s_v3",
    "Standard_E64s_v3",
    "Standard_E64is_v3",
    "Standard_D8s_v3",
    "Standard_E16s_v5",
    "Standard_E32s_v5",
    "Standard_E48s_v5",
    "Standard_E64s_v5",
    "Standard_E96s_v5",
    "Standard_E16bs_v5",
    "Standard_E32bs_v5",
    "Standard_E48bs_v5",
    "Standard_E64bs_v5",
    "Standard_E96bs_v5",
    "Standard_L8s_v3",
    "Standard_L16s_v3",
    "Standard_L32s_v3",
    "Standard_L48s_v3",
    "Standard_L64s_v3",
    "Standard_L80s_v3",
    "Standard_L8as_v3",
    "Standard_L16as_v3",
    "Standard_L32as_v3",
    "Standard_L48as_v3",
    "Standard_L64as_v3",
    "Standard_L80as_v3",
)


def test_default_sku_in_allow_list() -> None:
    assert aks_skus.DEFAULT_SKU in aks_skus.ALLOWED_SKUS


def test_default_sku_priced() -> None:
    assert aks_skus.DEFAULT_SKU in aks_skus.AZURE_VM_HOURLY_USD


def test_default_sku_matches_sibling_default() -> None:
    # Sibling: src/elastic_blast/constants.py::ELB_DFLT_AZURE_MACHINE_TYPE
    # Update both repos in lockstep.
    assert aks_skus.DEFAULT_SKU == "Standard_E32s_v5"


def test_allowed_skus_match_sibling_azure_hpc_machines() -> None:
    # Sibling: src/elastic_blast/azure_traits.py::AZURE_HPC_MACHINES is the
    # *blast pool* allow-list. The dashboard additionally exposes a small
    # set of system-pool SKUs (sibling constants.py::ELB_DFLT_AZURE_SYSTEM_VM_SIZE
    # and reasonable upgrades) that elastic-blast itself never schedules on,
    # so they are intentionally extra.
    blast_only = {sku for sku, entry in aks_skus.SKU_BY_NAME.items() if entry.role != "system"}
    assert blast_only == set(SIBLING_AZURE_HPC_MACHINES)
    # Every system-only SKU must be allowed too.
    system_only = {sku for sku, entry in aks_skus.SKU_BY_NAME.items() if entry.role == "system"}
    assert system_only.issubset(set(aks_skus.ALLOWED_SKUS))
    assert aks_skus.DEFAULT_SYSTEM_SKU in system_only


def test_blast_config_pricing_is_re_export() -> None:
    # blast_config.AZURE_VM_HOURLY_USD must be a copy of the allow-list
    # pricing so the cost estimator can never quote a SKU that elastic-blast
    # then refuses.
    assert AZURE_VM_HOURLY_USD == aks_skus.AZURE_VM_HOURLY_USD


def test_pricing_subset_of_allow_list() -> None:
    # No phantom prices for SKUs the user can't actually pick.
    extra = set(aks_skus.AZURE_VM_HOURLY_USD) - set(aks_skus.ALLOWED_SKUS)
    assert not extra, f"priced SKUs not in allow-list: {sorted(extra)}"


def test_every_allowed_sku_is_priced() -> None:
    missing = set(aks_skus.ALLOWED_SKUS) - set(aks_skus.AZURE_VM_HOURLY_USD)
    assert not missing, f"allowed SKUs without prices: {sorted(missing)}"


def test_sibling_pricing_values() -> None:
    # Sibling: src/elastic_blast/azure_traits.py::AZURE_VM_HOURLY_PRICES
    assert aks_skus.AZURE_VM_HOURLY_USD["Standard_E48bs_v5"] == 3.576
    assert aks_skus.AZURE_VM_HOURLY_USD["Standard_HB120rs_v3"] == 3.600


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
        "Standard_E16s_v3",
        "Standard_E32s_v3",
        "Standard_E48s_v3",
    ):
        assert not aks_skus.is_allowed(bad), bad


def test_is_allowed_accepts_default() -> None:
    assert aks_skus.is_allowed(aks_skus.DEFAULT_SKU)


def test_aks_skus_route_returns_compatible_default_fields(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    from api.main import app

    response = TestClient(app).get("/api/aks/skus")
    assert response.status_code == 200
    body = response.json()
    assert body["default"] == aks_skus.DEFAULT_SKU
    assert body["default_sku"] == aks_skus.DEFAULT_SKU
    assert body["default_system_sku"] == aks_skus.DEFAULT_SYSTEM_SKU
    assert len(body["skus"]) == len(aks_skus.ALLOWED_SKUS)
    assert body["skus"][0]["hourlyUsd"] == aks_skus.AZURE_VM_HOURLY_USD[body["skus"][0]["name"]]
    # Every SKU carries role + group so the SPA can split blast / system
    # pools and render <optgroup>s without hardcoding the catalog.
    for sku in body["skus"]:
        assert sku["role"] in ("system", "blast", "both")
        assert sku["group"] in body["group_labels"]
    # group_order covers every used group exactly once.
    used = {sku["group"] for sku in body["skus"]}
    assert set(body["group_order"]) == used
    assert len(body["group_order"]) == len(set(body["group_order"]))
    # System pool default is reachable from a system-flagged group.
    sys_entry = next(s for s in body["skus"] if s["name"] == aks_skus.DEFAULT_SYSTEM_SKU)
    assert sys_entry["role"] in ("system", "both")

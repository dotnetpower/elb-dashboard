"""Tests for the warmup feasibility planner.

Responsibility: Tests for the warmup feasibility planner
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_core_nt_on_three_e32s_v5_is_feasible`,
`test_tiny_db_is_trivially_feasible`, `test_core_nt_on_one_node_is_cluster_too_small`,
`test_cluster_too_small_recommendations_never_downgrade_sku`,
`test_huge_db_on_e32s_v5_is_node_sku_too_small`,
`test_huge_db_on_more_nodes_still_node_sku_too_small`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_warmup_planner.py`.
"""

from __future__ import annotations

import pytest
from api.services.aks_skus import DEFAULT_SKU
from api.services.db_sharding import (
    PRESET_SHARD_SETS,
    SAFE_SHARD_FRACTION_OF_NODE_RAM,
)
from api.services.warmup_planner import (
    _MAX_PRESET_SHARDS,
    WarmupPlan,
    compute_warmup_feasibility,
)

GIB = 1024**3


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
def test_core_nt_on_three_e32s_v5_is_feasible() -> None:
    """Live-environment scenario: 283 GiB DB on 3 × E32s_v5 (256 GiB)."""
    plan = compute_warmup_feasibility(
        db_total_bytes=int(283.62 * GIB),
        num_nodes=3,
        machine_type="Standard_E32s_v5",
    )
    assert plan.feasible is True
    assert plan.status == "ok"
    # by_nodes=3, by_memory=ceil(283.62/128)=3 -> chosen=3
    assert plan.chosen_shards == 3
    assert plan.target_shards == 3
    # per_shard = 283.62/3 ≈ 94.5 GiB
    assert 94.0 <= plan.per_shard_gib <= 95.0
    # per_node = db/nodes = 283.62/3
    assert 94.0 <= plan.per_node_gib <= 95.0
    # both below the 128 GiB safe budget
    assert plan.per_shard_gib <= plan.safe_node_budget_gib
    assert plan.per_node_gib <= plan.safe_node_budget_gib
    assert plan.recommendations == ()


def test_tiny_db_is_trivially_feasible() -> None:
    """20 MB 16S-style DB on any cluster: chosen = num_nodes."""
    plan = compute_warmup_feasibility(
        db_total_bytes=20 * 1024 * 1024,
        num_nodes=3,
        machine_type="Standard_E32s_v5",
    )
    assert plan.feasible is True
    assert plan.status == "ok"
    assert plan.chosen_shards == 3  # bound by num_nodes
    assert plan.per_shard_gib < 1.0


# ---------------------------------------------------------------------------
# cluster_too_small — DB does not fit per-node, but a SKU exists or
# adding nodes works
# ---------------------------------------------------------------------------
def test_core_nt_on_one_node_is_cluster_too_small() -> None:
    plan = compute_warmup_feasibility(
        db_total_bytes=int(283.62 * GIB),
        num_nodes=1,
        machine_type="Standard_E32s_v5",
    )
    assert plan.feasible is False
    assert plan.status == "cluster_too_small"
    # per_node = 283.62 (one node holds the whole DB)
    assert plan.per_node_gib > plan.safe_node_budget_gib
    # First recommendation must be the cheapest fix: add nodes.
    assert plan.recommendations[0].startswith("Increase blastpool node count")
    # Required nodes = ceil(283.62 / 128) = 3
    assert "to at least 3" in plan.recommendations[0]


def test_cluster_too_small_recommendations_never_downgrade_sku() -> None:
    """SKU upgrade suggestions must have strictly more RAM than the current SKU.

    Regression: an earlier draft suggested L8s_v3 (64 GiB) when the user was
    already on E32s_v5 (256 GiB) — nonsensical because downgrading worsens
    the per-node failure mode.
    """
    plan = compute_warmup_feasibility(
        db_total_bytes=int(283.62 * GIB),
        num_nodes=1,
        machine_type="Standard_E32s_v5",
    )
    sku_recs = [r for r in plan.recommendations if "Upgrade blastpool SKU" in r]
    for rec in sku_recs:
        assert "L8s_v3" not in rec
        assert "L8as_v3" not in rec
        # All upgrade suggestions must mention a RAM number larger than 256
        # (the current SKU's RAM). We just check no smaller-RAM SKU sneaks in.
        for smaller in ("64 GiB", "128 GiB", "256 GiB"):
            assert smaller not in rec, f"downgrade suggestion leaked: {rec}"


# ---------------------------------------------------------------------------
# node_sku_too_small — even at the largest preset the per-shard size
# exceeds the safe budget, so adding nodes cannot fix it
# ---------------------------------------------------------------------------
def test_huge_db_on_e32s_v5_is_node_sku_too_small() -> None:
    """1.5 TiB DB on E32s_v5: chosen clamps to MAX preset, per_shard still > budget."""
    plan = compute_warmup_feasibility(
        db_total_bytes=1500 * GIB,
        num_nodes=3,
        machine_type="Standard_E32s_v5",
    )
    assert plan.feasible is False
    assert plan.status == "node_sku_too_small"
    assert plan.chosen_shards == _MAX_PRESET_SHARDS  # clamped
    assert plan.target_shards > _MAX_PRESET_SHARDS  # ideal exceeds clamp
    # per_shard = 1500/10 = 150 GiB > 128 GiB safe budget
    assert plan.per_shard_gib > plan.safe_node_budget_gib
    # SKU recommendation must include something with safe budget >= 150 GiB
    sku_recs = [r for r in plan.recommendations if "Upgrade blastpool SKU" in r]
    assert sku_recs, "expected at least one SKU upgrade recommendation"


def test_huge_db_on_more_nodes_still_node_sku_too_small() -> None:
    """Adding nodes alone does not help when shard size itself exceeds budget."""
    plan = compute_warmup_feasibility(
        db_total_bytes=1500 * GIB,
        num_nodes=20,
        machine_type="Standard_E32s_v5",
    )
    assert plan.feasible is False
    assert plan.status == "node_sku_too_small"  # NOT cluster_too_small
    # 20 nodes is already plenty; the failure is shard size, not per-node pressure
    assert plan.per_shard_gib > plan.safe_node_budget_gib


def test_node_sku_too_small_recommendations_have_required_capacity() -> None:
    plan = compute_warmup_feasibility(
        db_total_bytes=1500 * GIB,
        num_nodes=3,
        machine_type="Standard_E32s_v5",
    )
    # Every SKU upgrade must satisfy: safe_budget >= db_gib / max_preset
    # = 1500 / 10 = 150 GiB. So node_ram >= 300 GiB.
    sku_recs = [r for r in plan.recommendations if "Upgrade blastpool SKU" in r]
    for rec in sku_recs:
        # The recommendation embeds the RAM as "(N GiB RAM per node)" — parse it.
        import re

        m = re.search(r"\((\d+) GiB RAM per node\)", rec)
        assert m, f"could not parse RAM from rec: {rec}"
        ram = int(m.group(1))
        assert ram * SAFE_SHARD_FRACTION_OF_NODE_RAM >= 150.0, (
            f"recommended SKU has insufficient RAM: {rec}"
        )


# ---------------------------------------------------------------------------
# Degenerate inputs — refusal codes
# ---------------------------------------------------------------------------
def test_zero_db_size_returns_no_db_size() -> None:
    plan = compute_warmup_feasibility(
        db_total_bytes=0,
        num_nodes=3,
        machine_type="Standard_E32s_v5",
    )
    assert plan.feasible is False
    assert plan.status == "no_db_size"
    assert plan.chosen_shards == 0
    assert plan.recommendations[0].startswith("Wait for the download")


def test_zero_nodes_returns_no_nodes() -> None:
    plan = compute_warmup_feasibility(
        db_total_bytes=int(283 * GIB),
        num_nodes=0,
        machine_type="Standard_E32s_v5",
    )
    assert plan.feasible is False
    assert plan.status == "no_nodes"
    assert plan.recommendations[0].startswith("Provision an AKS cluster")


def test_negative_db_size_raises() -> None:
    with pytest.raises(ValueError, match="db_total_bytes must be non-negative"):
        compute_warmup_feasibility(db_total_bytes=-1, num_nodes=3, machine_type="Standard_E32s_v5")


def test_negative_node_count_raises() -> None:
    with pytest.raises(ValueError, match="num_nodes must be non-negative"):
        compute_warmup_feasibility(
            db_total_bytes=GIB, num_nodes=-1, machine_type="Standard_E32s_v5"
        )


# ---------------------------------------------------------------------------
# Unknown SKU — warning, not failure
# ---------------------------------------------------------------------------
def test_unknown_sku_returns_ok_unknown_sku_with_fallback_ram() -> None:
    plan = compute_warmup_feasibility(
        db_total_bytes=10 * GIB,
        num_nodes=3,
        machine_type="Standard_NotASku_99",
    )
    assert plan.feasible is True
    assert plan.status == "ok_unknown_sku"
    # Fallback RAM is 64 GiB → safe budget 32 GiB.
    assert plan.node_ram_gib == 64.0
    assert plan.safe_node_budget_gib == 32.0
    assert "not in the catalog" in plan.message


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def test_to_dict_is_json_serialisable() -> None:
    import json

    plan = compute_warmup_feasibility(
        db_total_bytes=int(283.62 * GIB),
        num_nodes=3,
        machine_type="Standard_E32s_v5",
    )
    d = plan.to_dict()
    # Required fields the SPA depends on
    assert {
        "feasible",
        "status",
        "message",
        "num_nodes",
        "machine_type",
        "node_ram_gib",
        "safe_node_budget_gib",
        "db_total_bytes",
        "db_gib",
        "chosen_shards",
        "target_shards",
        "per_shard_gib",
        "per_node_gib",
        "shards_per_node",
        "recommendations",
    }.issubset(d.keys())
    assert isinstance(d["recommendations"], list)
    # Round-trip through json — no Decimal, no tuple, no enum.
    encoded = json.dumps(d)
    assert json.loads(encoded) == d


# ---------------------------------------------------------------------------
# Internal invariants
# ---------------------------------------------------------------------------
def test_chosen_shards_always_within_presets_when_feasible() -> None:
    plan = compute_warmup_feasibility(
        db_total_bytes=int(283.62 * GIB),
        num_nodes=3,
        machine_type="Standard_E32s_v5",
    )
    assert plan.chosen_shards in PRESET_SHARD_SETS


def test_default_sku_argument_uses_e32s_v5() -> None:
    # Calling without machine_type should not crash and should pick the
    # blastpool default.
    plan = compute_warmup_feasibility(
        db_total_bytes=int(50 * GIB),
        num_nodes=3,
    )
    assert plan.machine_type == DEFAULT_SKU
    assert plan.feasible is True


def test_target_shards_can_exceed_max_preset_when_clamped() -> None:
    plan = compute_warmup_feasibility(
        db_total_bytes=2000 * GIB,
        num_nodes=3,
        machine_type="Standard_E32s_v5",
    )
    assert plan.target_shards > plan.chosen_shards
    assert plan.chosen_shards == _MAX_PRESET_SHARDS
    assert plan.feasible is False


def test_plan_is_immutable() -> None:
    plan = compute_warmup_feasibility(
        db_total_bytes=int(283.62 * GIB),
        num_nodes=3,
        machine_type="Standard_E32s_v5",
    )
    assert isinstance(plan, WarmupPlan)
    # frozen dataclass
    with pytest.raises((AttributeError, Exception)):
        plan.feasible = False  # type: ignore[misc]

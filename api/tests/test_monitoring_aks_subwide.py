"""Subscription-wide AKS list filter tests.

Responsibility: Lock the ElasticBLAST identification surface used by
    `list_aks_clusters_in_subscription` so a future tag rename or filter
    relaxation cannot silently start surfacing unrelated workload clusters
    on the dashboard.
Edit boundaries: Fakes only; never reach Azure. Edit when the tag set or
    legacy fingerprint changes.
Key entry points: `_make_cluster`, `test_subscription_list_filters_unmanaged_by_default`,
    `test_subscription_list_keeps_legacy_blastpool_with_taint`,
    `test_subscription_list_include_unmanaged_returns_everything`,
    `test_subscription_list_parses_resource_group_from_arm_id`,
    `test_subscription_list_surfaces_tier_tag`.
Risky contracts: The filter is load-bearing security — any change to
    `_is_elb_managed_cluster` must keep these cases passing.
Validation: `uv run pytest -q api/tests/test_monitoring_aks_subwide.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from api.services import monitoring


def _pool(
    *,
    name: str,
    mode: str = "User",
    vm_size: str = "Standard_E32s_v5",
    count: int = 3,
    taints: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        mode=mode,
        vm_size=vm_size,
        count=count,
        min_count=None,
        max_count=None,
        os_type="Linux",
        power_state=SimpleNamespace(code="Running"),
        enable_auto_scaling=False,
        node_taints=taints or [],
    )


def _make_cluster(
    *,
    name: str,
    rg: str,
    tags: dict[str, str] | None = None,
    pools: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    arm_id = (
        f"/subscriptions/00000000-0000-0000-0000-000000000000/"
        f"resourceGroups/{rg}/providers/Microsoft.ContainerService/"
        f"managedClusters/{name}"
    )
    return SimpleNamespace(
        id=arm_id,
        name=name,
        location="koreacentral",
        kubernetes_version="1.30.0",
        provisioning_state="Succeeded",
        power_state=SimpleNamespace(code="Running"),
        agent_pool_profiles=pools
        or [_pool(name="blastpool", taints=["workload=blast:NoSchedule"])],
        identity_profile=None,
        network_profile=None,
        fqdn=f"{name}.example.local",
        tags=tags or {},
    )


def _install_fake_subwide(monkeypatch, clusters: list[Any]) -> None:
    fake_client = SimpleNamespace(
        managed_clusters=SimpleNamespace(list=lambda: list(clusters))
    )
    monkeypatch.setattr(
        monitoring, "aks_client", lambda _credential, _subscription_id: fake_client
    )


def test_subscription_list_filters_unmanaged_by_default(monkeypatch) -> None:
    managed = _make_cluster(
        name="aks-elb",
        rg="rg-elb",
        tags={"managedBy": "elb-dashboard", "app": "elastic-blast"},
    )
    foreign = _make_cluster(
        name="aks-other-team",
        rg="rg-other",
        tags={"team": "platform"},
        pools=[_pool(name="agentpool")],
    )
    _install_fake_subwide(monkeypatch, [managed, foreign])

    result = monitoring.list_aks_clusters_in_subscription(object(), "sub")

    names = {c["name"] for c in result}
    assert names == {"aks-elb"}
    elb_row = result[0]
    assert elb_row["resource_group"] == "rg-elb"
    assert elb_row["managed_by_elb"] is True
    assert elb_row["tags"]["managedBy"] == "elb-dashboard"


def test_subscription_list_keeps_legacy_blastpool_with_taint(monkeypatch) -> None:
    """Legacy clusters created before the tag surface still surface as long as
    they carry the `blastpool` pool plus the `workload=blast` taint."""

    legacy = _make_cluster(
        name="aks-legacy",
        rg="rg-legacy",
        tags={},  # no managedBy / app tag at all
        pools=[
            _pool(name="systempool", mode="System", count=1),
            _pool(name="blastpool", taints=["workload=blast:NoSchedule"]),
        ],
    )
    _install_fake_subwide(monkeypatch, [legacy])

    result = monitoring.list_aks_clusters_in_subscription(object(), "sub")

    assert len(result) == 1
    assert result[0]["managed_by_elb"] is True


def test_subscription_list_rejects_blastpool_name_without_taint(monkeypatch) -> None:
    """A user-created pool literally named `blastpool` but missing the
    `workload=blast` taint is not enough — pool name alone is too weak."""

    impostor = _make_cluster(
        name="aks-impostor",
        rg="rg-impostor",
        tags={"team": "neighbour"},
        pools=[
            _pool(name="systempool", mode="System", count=1),
            _pool(name="blastpool", taints=[]),  # name matches, no taint
        ],
    )
    _install_fake_subwide(monkeypatch, [impostor])

    result = monitoring.list_aks_clusters_in_subscription(object(), "sub")

    assert result == []


def test_subscription_list_include_unmanaged_returns_everything(monkeypatch) -> None:
    """`include_unmanaged=True` is the diagnostics escape hatch — the
    dashboard does not set it in normal use, but the route exposes it so an
    operator can confirm whether a cluster they expected to see was filtered
    out vs missing entirely."""

    managed = _make_cluster(name="aks-elb", rg="rg-elb", tags={"app": "elastic-blast"})
    foreign = _make_cluster(
        name="aks-foreign",
        rg="rg-foreign",
        tags={},
        pools=[_pool(name="agentpool")],
    )
    _install_fake_subwide(monkeypatch, [managed, foreign])

    result = monitoring.list_aks_clusters_in_subscription(
        object(), "sub", include_unmanaged=True
    )

    names = {c["name"] for c in result}
    assert names == {"aks-elb", "aks-foreign"}
    foreign_row = next(c for c in result if c["name"] == "aks-foreign")
    assert foreign_row["managed_by_elb"] is False


def test_subscription_list_parses_resource_group_from_arm_id(monkeypatch) -> None:
    """Sub-wide responses must carry per-row RG so downstream actions
    (delete / start / stop / autoWarmup) target the right scope."""

    a = _make_cluster(name="aks-a", rg="rg-blast-heavy", tags={"app": "elastic-blast"})
    b = _make_cluster(name="aks-b", rg="rg-blast-light", tags={"app": "elastic-blast"})
    _install_fake_subwide(monkeypatch, [a, b])

    rows = monitoring.list_aks_clusters_in_subscription(object(), "sub")
    by_name = {row["name"]: row["resource_group"] for row in rows}

    assert by_name == {"aks-a": "rg-blast-heavy", "aks-b": "rg-blast-light"}


def test_subscription_list_surfaces_tier_tag(monkeypatch) -> None:
    heavy = _make_cluster(
        name="aks-heavy",
        rg="rg-heavy",
        tags={"app": "elastic-blast", "elb-tier": "heavy"},
    )
    light = _make_cluster(
        name="aks-light",
        rg="rg-light",
        tags={"app": "elastic-blast", "elb-tier": "light"},
    )
    untagged = _make_cluster(
        name="aks-mystery",
        rg="rg-mystery",
        tags={"app": "elastic-blast"},
    )
    _install_fake_subwide(monkeypatch, [heavy, light, untagged])

    rows = monitoring.list_aks_clusters_in_subscription(object(), "sub")
    by_name = {row["name"]: row.get("tier") for row in rows}

    assert by_name == {"aks-heavy": "heavy", "aks-light": "light", "aks-mystery": None}


def test_subscription_list_handles_malformed_arm_id(monkeypatch) -> None:
    """A cluster whose ARM id is missing the `resourceGroups/<rg>` segment
    still surfaces, but with empty RG — the route still includes it so an
    operator can see the row, and downstream actions stay disabled because
    the per-row RG is "". Defensive parser correctness, not a real Azure
    state."""

    weird = _make_cluster(name="aks-weird", rg="rg-x", tags={"app": "elastic-blast"})
    weird.id = "weird-id-no-rg-segment"
    _install_fake_subwide(monkeypatch, [weird])

    rows = monitoring.list_aks_clusters_in_subscription(object(), "sub")
    assert rows[0]["resource_group"] == ""


# A focused unit test on the tier tag — verifies `build_cluster_params`
# only writes `elb-tier` when the caller passes a non-empty value, so we
# never end up with `elb-tier=""` on the cluster.
def test_build_cluster_params_writes_tier_only_when_non_empty() -> None:
    pytest.importorskip("azure.mgmt.containerservice.models")
    from api.tasks.azure.cluster_params import build_cluster_params

    with_tier = build_cluster_params(
        region="koreacentral",
        cluster_name="aks-heavy",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_E32s_v5",
        blast_count=3,
        caller_oid="oid-1",
        tier="heavy",
    )
    assert with_tier.tags["elb-tier"] == "heavy"

    without_tier = build_cluster_params(
        region="koreacentral",
        cluster_name="aks-anon",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_E32s_v5",
        blast_count=3,
        caller_oid="oid-2",
    )
    assert "elb-tier" not in without_tier.tags

    blank_tier = build_cluster_params(
        region="koreacentral",
        cluster_name="aks-blank",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_E32s_v5",
        blast_count=3,
        caller_oid="oid-3",
        tier="   ",
    )
    assert "elb-tier" not in blank_tier.tags

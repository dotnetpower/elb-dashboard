from __future__ import annotations

from api.tasks.azure import _build_cluster_params


def test_build_cluster_params_enables_blob_csi_driver() -> None:
    cluster = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=1,
        caller_oid="caller-1",
    )

    assert cluster.storage_profile.blob_csi_driver.enabled is True


def test_build_cluster_params_keeps_expected_pools_and_taints() -> None:
    cluster = _build_cluster_params(
        region="koreacentral",
        cluster_name="elb-smoke-aks",
        sys_sku="Standard_D2s_v3",
        sys_count=1,
        blast_sku="Standard_D8s_v3",
        blast_count=2,
        caller_oid="caller-1",
    )

    pools = {pool.name: pool for pool in cluster.agent_pool_profiles}
    assert pools["systempool"].node_taints == ["CriticalAddonsOnly=true:NoSchedule"]
    assert pools["blastpool"].node_labels == {"workload": "blast"}
    assert pools["blastpool"].node_taints == ["workload=blast:NoSchedule"]

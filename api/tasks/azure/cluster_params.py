"""AKS managed-cluster ARM model builder.

Responsibility: Construct the `ManagedCluster` ARM payload that the provision task
    submits via `begin_create_or_update`, including the two-pool layout (systempool +
    blastpool with the exact label/taint pair the sibling repo expects).
Edit boundaries: Pure model assembly. No Azure SDK I/O; no state writes.
Key entry points: `build_cluster_params`.
Risky contracts: The pool names (`systempool`, `blastpool`), the
    `workload=blast` label, the `workload=blast:NoSchedule` taint, and the
    `CriticalAddonsOnly=true:NoSchedule` system taint must stay byte-identical to
    `elastic-blast-azure` `src/elastic_blast/constants.py` — kubectl manifests rendered
    by other parts of the system reference these strings.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py`.
"""

from __future__ import annotations

from typing import Any


def build_cluster_params(
    *,
    region: str,
    cluster_name: str,
    sys_sku: str,
    sys_count: int,
    blast_sku: str,
    blast_count: int,
    caller_oid: str,
) -> Any:
    """Build the AKS managed cluster model used by the provision task."""
    from azure.mgmt.containerservice.models import (
        ManagedCluster,
        ManagedClusterAgentPoolProfile,
        ManagedClusterIdentity,
        ManagedClusterStorageProfile,
        ManagedClusterStorageProfileBlobCSIDriver,
    )

    # Mirror the sibling constants exactly so kubectl manifests that
    # reference the pool name/label/taint stay valid.
    SYSTEM_POOL_NAME = "systempool"
    BLAST_POOL_NAME = "blastpool"
    BLAST_LABEL_KEY = "workload"
    BLAST_LABEL_VALUE = "blast"
    BLAST_TAINT = f"{BLAST_LABEL_KEY}={BLAST_LABEL_VALUE}:NoSchedule"
    SYSTEM_TAINT = "CriticalAddonsOnly=true:NoSchedule"

    return ManagedCluster(
        location=region,
        identity=ManagedClusterIdentity(type="SystemAssigned"),
        dns_prefix=cluster_name,
        storage_profile=ManagedClusterStorageProfile(
            blob_csi_driver=ManagedClusterStorageProfileBlobCSIDriver(enabled=True)
        ),
        agent_pool_profiles=[
            ManagedClusterAgentPoolProfile(
                name=SYSTEM_POOL_NAME,
                count=sys_count,
                vm_size=sys_sku,
                os_type="Linux",
                mode="System",
                type="VirtualMachineScaleSets",
                enable_auto_scaling=False,
                node_taints=[SYSTEM_TAINT],
            ),
            ManagedClusterAgentPoolProfile(
                name=BLAST_POOL_NAME,
                count=blast_count,
                vm_size=blast_sku,
                os_type="Linux",
                mode="User",
                type="VirtualMachineScaleSets",
                enable_auto_scaling=False,
                node_labels={BLAST_LABEL_KEY: BLAST_LABEL_VALUE},
                node_taints=[BLAST_TAINT],
            ),
        ],
        tags={
            "app": "elastic-blast",
            "managedBy": "elb-dashboard",
            "owner": caller_oid or "unknown",
            "elb-system-pool": SYSTEM_POOL_NAME,
            "elb-blast-pool": BLAST_POOL_NAME,
        },
    )

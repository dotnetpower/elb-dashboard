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
    by other parts of the system reference these strings. The base tag set
    (`app=elastic-blast`, `managedBy=elb-dashboard`) is the ground-truth filter used
    by the subscription-wide cluster list — drop or rename either tag and the
    dashboard will stop recognising clusters it provisioned. When `vnet_subnet_id`
    is supplied the cluster is created in BYO-subnet mode (nodes land in the hub
    `snet-aks` subnet) and the network profile is pinned to Azure CNI Overlay so
    pods stay on the overlay pod CIDR and do not consume subnet IPs — the
    overlay CIDRs (`10.244.0.0/16` pod, `10.0.0.0/16` service, `10.0.0.10` DNS)
    must not collide with the hub VNet address space (`10.20.0.0/20`). The
    `warm_cache_mode` parameter governs the blastpool OS disk: the default
    `ephemeral` keeps the historical payload byte-identical (Azure picks an
    ephemeral OS disk when the SKU cache allows), while `node_disk` pins a
    Managed OS disk (survives `az aks stop` deallocation) sized to hold the
    staged BLAST database so a stop/start cycle re-touches RAM instead of
    re-downloading. `data_disk` is a no-op here — it is realised by a PVC in
    the warmup task, not the cluster model.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py`.
"""

from __future__ import annotations

from typing import Any

# Warm-cache persistence modes. Keep in lock-step with
# `api.services.performance_pref.WARM_CACHE_MODES` and the SPA WarmCacheMode
# union — adding a value requires updating all three.
WARM_CACHE_MODE_EPHEMERAL = "ephemeral"
WARM_CACHE_MODE_NODE_DISK = "node_disk"
WARM_CACHE_MODE_DATA_DISK = "data_disk"

# Managed OS disk size for `node_disk` mode. The default 128 GB AKS OS disk is
# too small for a staged BLAST database (the `nt` set alone expands past
# ~200 GB), so node_disk provisions a 512 GB managed OS disk to hold the warm
# cache across a stop/start cycle.
_NODE_DISK_OS_DISK_SIZE_GB = 512


def build_cluster_params(
    *,
    region: str,
    cluster_name: str,
    sys_sku: str,
    sys_count: int,
    blast_sku: str,
    blast_count: int,
    caller_oid: str,
    tier: str | None = None,
    vnet_subnet_id: str | None = None,
    warm_cache_mode: str = WARM_CACHE_MODE_EPHEMERAL,
) -> Any:
    """Build the AKS managed cluster model used by the provision task.

    `tier` is a free-form classification label (e.g. "heavy", "light", "gpu")
    written to the `elb-tier` ARM tag so the dashboard can group multi-cluster
    deployments. Empty / whitespace tier values are dropped so we never store
    `elb-tier=""` on the cluster.

    `vnet_subnet_id` selects the cluster networking mode:

      * Truthy → BYO-subnet mode. Both agent pools set `vnet_subnet_id` so the
        nodes land in the supplied subnet (the hub VNet's `snet-aks`), and the
        network profile is pinned to Azure CNI Overlay. This is what lets the
        nodes resolve the workload Storage private-endpoint FQDN (the hub VNet
        is linked to the `privatelink.*` zones) and route to it intra-VNet —
        without it the cluster gets a fresh managed VNet that the dashboard MI
        cannot peer/link, so warmup azcopy 403s against the locked-down
        Storage account.
      * Falsy / None → managed-VNet mode (Azure picks the node subnet). Kept
        for local/legacy callers and tests that do not inject a subnet id.
    """
    from azure.mgmt.containerservice.models import (
        ContainerServiceNetworkProfile,
        ManagedCluster,
        ManagedClusterAgentPoolProfile,
        ManagedClusterIdentity,
        ManagedClusterOIDCIssuerProfile,
        ManagedClusterSecurityProfile,
        ManagedClusterSecurityProfileWorkloadIdentity,
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

    tags: dict[str, str] = {
        "app": "elastic-blast",
        "managedBy": "elb-dashboard",
        "owner": caller_oid or "unknown",
        "elb-system-pool": SYSTEM_POOL_NAME,
        "elb-blast-pool": BLAST_POOL_NAME,
    }
    tier_clean = (tier or "").strip()
    if tier_clean:
        tags["elb-tier"] = tier_clean

    # Resolve the blastpool OS-disk overrides for the warm-cache mode. Only
    # `node_disk` changes the payload; `ephemeral` (default) and `data_disk`
    # leave the disk fields unset so the model stays byte-identical to the
    # historical default.
    mode_clean = (warm_cache_mode or "").strip() or WARM_CACHE_MODE_EPHEMERAL
    blast_os_disk_type: str | None = None
    blast_os_disk_size_gb: int | None = None
    if mode_clean == WARM_CACHE_MODE_NODE_DISK:
        blast_os_disk_type = "Managed"
        blast_os_disk_size_gb = _NODE_DISK_OS_DISK_SIZE_GB
        tags["elb-warm-cache"] = WARM_CACHE_MODE_NODE_DISK
    elif mode_clean == WARM_CACHE_MODE_DATA_DISK:
        # Realised by a PVC in the warmup task; tag the cluster so the
        # dashboard can surface the intended mode without re-reading the pref.
        tags["elb-warm-cache"] = WARM_CACHE_MODE_DATA_DISK

    subnet_id = (vnet_subnet_id or "").strip()
    pool_subnet_id = subnet_id or None
    # Pin Azure CNI Overlay when running in BYO-subnet mode so only nodes (not
    # every pod) draw IPs from `snet-aks`. The CIDRs mirror the AKS defaults the
    # managed-VNet clusters already use; they must stay outside the hub VNet
    # space (10.20.0.0/20) to avoid overlap.
    network_profile = (
        ContainerServiceNetworkProfile(
            network_plugin="azure",
            network_plugin_mode="overlay",
            pod_cidr="10.244.0.0/16",
            service_cidr="10.0.0.0/16",
            dns_service_ip="10.0.0.10",
        )
        if pool_subnet_id
        else None
    )

    return ManagedCluster(
        location=region,
        identity=ManagedClusterIdentity(type="SystemAssigned"),
        dns_prefix=cluster_name,
        # OIDC issuer + Microsoft Entra Workload ID. Both are required for
        # the dashboard's "Deploy elb-openapi" flow:
        #
        #   * oidc_issuer_profile.enabled gives the cluster an OIDC issuer
        #     URL so `api.tasks.openapi.rbac.setup_workload_identity` can
        #     create a Federated Identity Credential bound to that issuer.
        #   * security_profile.workload_identity.enabled installs the
        #     workload-identity mutating webhook in the cluster so the
        #     OpenAPI pod actually gets its projected SA token at
        #     /var/run/secrets/azure/tokens/azure-identity-token.
        #
        # Verified gap: a cluster created with only `oidc=true` and
        # `wi=null` (Azure default for OIDC in some API versions) still
        # produces a federated credential but the pod hangs on the token
        # swap with "WorkloadIdentityCredential: failed to read token
        # file". Pinning both flags up-front avoids the
        # `az aks update --enable-workload-identity` follow-up.
        oidc_issuer_profile=ManagedClusterOIDCIssuerProfile(enabled=True),
        security_profile=ManagedClusterSecurityProfile(
            workload_identity=ManagedClusterSecurityProfileWorkloadIdentity(enabled=True)
        ),
        storage_profile=ManagedClusterStorageProfile(
            blob_csi_driver=ManagedClusterStorageProfileBlobCSIDriver(enabled=True)
        ),
        network_profile=network_profile,
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
                vnet_subnet_id=pool_subnet_id,
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
                vnet_subnet_id=pool_subnet_id,
                os_disk_type=blast_os_disk_type,
                os_disk_size_gb=blast_os_disk_size_gb,
            ),
        ],
        tags=tags,
    )

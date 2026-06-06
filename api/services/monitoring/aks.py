"""AKS cluster ARM helpers (list + pool selection).

Responsibility: AKS cluster ARM helpers (list + pool selection).
Edit boundaries: Keep reusable domain logic here; routes and tasks call this layer.
Key entry points: list_aks_clusters, list_aks_clusters_in_subscription,
    _select_workload_agent_pool, _kubelet_object_id, _is_elb_managed_cluster
Risky contracts: Keep Azure credentials centralized and sanitise data at HTTP/log boundaries.
    Subscription-wide list filters to ELB-managed clusters by ARM tag
    (`managedBy=elb-dashboard` or `app=elastic-blast`) with a `blastpool` legacy
    fallback — changing this contract risks pulling unrelated workload clusters
    into the dashboard.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from azure.core.credentials import TokenCredential

from api.services.azure_clients import aks_client

LOGGER = logging.getLogger(__name__)

BLAST_POOL_NAME = "blastpool"
BLAST_TAINT_PREFIX = "workload=blast"


def _serialise_cluster(cluster: Any, *, resource_group: str) -> dict[str, Any]:
    pools = cluster.agent_pool_profiles or []
    agent_pool = _select_workload_agent_pool(pools)
    pool_details = [
        {
            "name": pool.name,
            "vm_size": pool.vm_size,
            "count": pool.count,
            "min_count": pool.min_count,
            "max_count": pool.max_count,
            "os_type": pool.os_type,
            "mode": pool.mode,
            "power_state": pool.power_state.code if pool.power_state else None,
            "enable_auto_scaling": pool.enable_auto_scaling,
        }
        for pool in pools
    ]
    tags = dict(getattr(cluster, "tags", None) or {})
    return {
        "name": cluster.name,
        "resource_group": resource_group,
        "region": cluster.location,
        "k8s_version": cluster.kubernetes_version,
        "provisioning_state": cluster.provisioning_state,
        "power_state": cluster.power_state.code if cluster.power_state else None,
        "node_count": agent_pool.count if agent_pool else None,
        "node_sku": agent_pool.vm_size if agent_pool else None,
        "kubelet_object_id": _kubelet_object_id(cluster),
        "agent_pools": pool_details,
        "network_plugin": (
            cluster.network_profile.network_plugin if cluster.network_profile else None
        ),
        "fqdn": cluster.fqdn,
        # Sub-wide identification surface.
        "tags": tags,
        "tier": tags.get("elb-tier") or None,
        "managed_by_elb": _is_elb_managed_cluster(cluster),
    }


def _parse_rg_from_arm_id(arm_id: str | None) -> str:
    """Extract the resourceGroups segment from an ARM cluster id.

    Example id:
        /subscriptions/<sub>/resourceGroups/<rg>/providers/
        Microsoft.ContainerService/managedClusters/<name>
    Returns "" if the id is missing or malformed (caller treats that as
    unknown RG — sub-wide list endpoint then surfaces the row but actions
    that need an RG remain disabled).
    """
    if not arm_id:
        return ""
    parts = arm_id.split("/")
    for idx in range(len(parts) - 1):
        if parts[idx].lower() == "resourcegroups":
            return parts[idx + 1]
    return ""


def _is_elb_managed_cluster(cluster: Any) -> bool:
    """True when the cluster carries the ElasticBLAST identification surface.

    Primary signal: ARM tags written by `provision_aks` (`managedBy=elb-dashboard`
    or `app=elastic-blast`). Legacy fallback: a `blastpool` agent pool with a
    `workload=blast` taint — same shape `elastic-blast-azure/constants.py` uses,
    chosen because the pool *name* alone is too weak (a user could create an
    unrelated pool called `blastpool`), but pairing it with the taint matches
    only clusters that were configured for BLAST workloads.
    """
    tags = dict(getattr(cluster, "tags", None) or {})
    if tags.get("managedBy") == "elb-dashboard":
        return True
    if tags.get("app") == "elastic-blast":
        return True
    pools = cluster.agent_pool_profiles or []
    for pool in pools:
        if str(getattr(pool, "name", "") or "").lower() != BLAST_POOL_NAME:
            continue
        taints = getattr(pool, "node_taints", None) or []
        for taint in taints:
            if BLAST_TAINT_PREFIX in str(taint):
                return True
    return False


def list_aks_clusters(
    credential: TokenCredential, subscription_id: str, resource_group: str
) -> list[dict[str, Any]]:
    client = aks_client(credential, subscription_id)
    clusters: list[dict[str, Any]] = []
    for cluster in client.managed_clusters.list_by_resource_group(resource_group):
        clusters.append(_serialise_cluster(cluster, resource_group=resource_group))
    return clusters


def list_aks_clusters_in_subscription(
    credential: TokenCredential,
    subscription_id: str,
    *,
    include_unmanaged: bool = False,
) -> list[dict[str, Any]]:
    """Subscription-wide list of AKS clusters, filtered to ElasticBLAST clusters.

    Why this exists: the dashboard supports multi-cluster workloads (heavy /
    light / GPU clusters typically live in *different* resource groups so RG
    is a natural classification key). A subscription-wide list lets the
    user manage them all from one card without retoggling Workload RG.

    Filter contract: by default only ElasticBLAST clusters are returned
    (`_is_elb_managed_cluster`). Set `include_unmanaged=True` to return every
    AKS cluster in the subscription — intended for diagnostics, not normal
    dashboard rendering. Foreign clusters that pass the filter would
    accidentally surface BLAST controls on unrelated workloads, so the
    default is intentionally strict.

    Returns: list of cluster dicts identical in shape to `list_aks_clusters`,
    with the addition that `resource_group` is parsed from each cluster's
    ARM id (clusters can live in different RGs).
    """
    client = aks_client(credential, subscription_id)
    clusters: list[dict[str, Any]] = []
    for cluster in client.managed_clusters.list():
        if not include_unmanaged and not _is_elb_managed_cluster(cluster):
            continue
        rg = _parse_rg_from_arm_id(getattr(cluster, "id", None))
        clusters.append(_serialise_cluster(cluster, resource_group=rg))
    return clusters


def _select_workload_agent_pool(pools: list[Any]) -> Any | None:
    if not pools:
        return None
    for pool in pools:
        if str(getattr(pool, "name", "") or "").lower() == BLAST_POOL_NAME:
            return pool
    for pool in pools:
        if str(getattr(pool, "mode", "") or "").lower() == "user":
            return pool
    return pools[0]


def _kubelet_object_id(cluster: Any) -> str | None:
    if not cluster.identity_profile or "kubeletidentity" not in cluster.identity_profile:
        return None
    return cast(str | None, cluster.identity_profile["kubeletidentity"].object_id)


# ---------------------------------------------------------------------------
# Rich detail serialisation for the diagnostics engine.
#
# `serialise_cluster_detail` is a SUPERSET of `_serialise_cluster`: it keeps
# every key the monitor card relies on and adds the Well-Architected /
# Cloud-Adoption-Framework configuration fields the diagnostics rule catalog
# inspects (SKU tier, AAD/RBAC, private cluster, addons, auto-upgrade, OIDC,
# security profile, per-pool zones/disk). Kept separate from `_serialise_cluster`
# so the monitor `AksClusterSummary` contract stays frozen.
# ---------------------------------------------------------------------------


def _pool_detail(pool: Any) -> dict[str, Any]:
    return {
        "name": getattr(pool, "name", None),
        "mode": getattr(pool, "mode", None),
        "count": getattr(pool, "count", None),
        "min_count": getattr(pool, "min_count", None),
        "max_count": getattr(pool, "max_count", None),
        "enable_auto_scaling": getattr(pool, "enable_auto_scaling", None),
        "vm_size": getattr(pool, "vm_size", None),
        "os_type": getattr(pool, "os_type", None),
        "os_disk_type": getattr(pool, "os_disk_type", None),
        "availability_zones": list(getattr(pool, "availability_zones", None) or []),
        "max_pods": getattr(pool, "max_pods", None),
        "orchestrator_version": getattr(pool, "orchestrator_version", None),
    }


def _addon_enabled(cluster: Any, addon_name: str) -> bool | None:
    """Return True/False if the addon profile is present, else None (unknown).

    Addon profile keys are case-insensitive in practice across API versions
    (`omsagent` / `omsAgent`), so match case-folded.
    """
    profiles = getattr(cluster, "addon_profiles", None) or {}
    if not profiles:
        return None
    target = addon_name.casefold()
    for key, value in profiles.items():
        if str(key).casefold() == target:
            return bool(getattr(value, "enabled", False))
    return False


def serialise_cluster_detail(cluster: Any, *, resource_group: str) -> dict[str, Any]:
    base = _serialise_cluster(cluster, resource_group=resource_group)
    sku = getattr(cluster, "sku", None)
    network = getattr(cluster, "network_profile", None)
    api_access = getattr(cluster, "api_server_access_profile", None)
    aad = getattr(cluster, "aad_profile", None)
    auto_upgrade = getattr(cluster, "auto_upgrade_profile", None)
    oidc = getattr(cluster, "oidc_issuer_profile", None)
    security = getattr(cluster, "security_profile", None)
    identity = getattr(cluster, "identity", None)
    pools = cluster.agent_pool_profiles or []

    workload_identity = None
    defender = None
    image_cleaner = None
    if security is not None:
        wi = getattr(security, "workload_identity", None)
        workload_identity = bool(getattr(wi, "enabled", False)) if wi is not None else None
        df = getattr(security, "defender", None)
        # Defender is enabled when a Log Analytics workspace is wired in.
        defender = bool(getattr(df, "security_monitoring", None)) if df is not None else None
        ic = getattr(security, "image_cleaner", None)
        image_cleaner = bool(getattr(ic, "enabled", False)) if ic is not None else None

    base.update(
        {
            "sku_tier": getattr(sku, "tier", None) if sku is not None else None,
            "pool_details": [_pool_detail(p) for p in pools],
            "network_policy": getattr(network, "network_policy", None) if network else None,
            "network_plugin_mode": (
                getattr(network, "network_plugin_mode", None) if network else None
            ),
            "load_balancer_sku": getattr(network, "load_balancer_sku", None) if network else None,
            "outbound_type": getattr(network, "outbound_type", None) if network else None,
            "private_cluster": (
                bool(getattr(api_access, "enable_private_cluster", False))
                if api_access is not None
                else None
            ),
            "authorized_ip_ranges": list(getattr(api_access, "authorized_ip_ranges", None) or [])
            if api_access is not None
            else [],
            "disable_run_command": (
                bool(getattr(api_access, "disable_run_command", False))
                if api_access is not None
                else None
            ),
            "aad_managed": bool(getattr(aad, "managed", False)) if aad is not None else None,
            "azure_rbac": (
                bool(getattr(aad, "enable_azure_rbac", False)) if aad is not None else None
            ),
            "disable_local_accounts": getattr(cluster, "disable_local_accounts", None),
            "identity_type": getattr(identity, "type", None) if identity is not None else None,
            "upgrade_channel": (
                getattr(auto_upgrade, "upgrade_channel", None) if auto_upgrade else None
            ),
            "node_os_upgrade_channel": (
                getattr(auto_upgrade, "node_os_upgrade_channel", None) if auto_upgrade else None
            ),
            "oidc_issuer_enabled": (
                bool(getattr(oidc, "enabled", False)) if oidc is not None else None
            ),
            "workload_identity": workload_identity,
            "defender_enabled": defender,
            "image_cleaner_enabled": image_cleaner,
            "addon_monitoring": _addon_enabled(cluster, "omsagent"),
            "addon_azure_policy": _addon_enabled(cluster, "azurepolicy"),
            "addon_keyvault_secrets": _addon_enabled(cluster, "azureKeyvaultSecretsProvider"),
        }
    )
    return base


def list_aks_clusters_detail_in_subscription(
    credential: TokenCredential, subscription_id: str
) -> list[dict[str, Any]]:
    """Subscription-wide list of ELB-managed clusters with rich WAF/CAF detail.

    Same managed-cluster filter as `list_aks_clusters_in_subscription`, but each
    entry carries the configuration surface the diagnostics rules inspect.
    """
    client = aks_client(credential, subscription_id)
    clusters: list[dict[str, Any]] = []
    for cluster in client.managed_clusters.list():
        if not _is_elb_managed_cluster(cluster):
            continue
        rg = _parse_rg_from_arm_id(getattr(cluster, "id", None))
        clusters.append(serialise_cluster_detail(cluster, resource_group=rg))
    return clusters


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

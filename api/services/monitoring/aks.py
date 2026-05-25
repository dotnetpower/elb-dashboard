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
# Storage
# ---------------------------------------------------------------------------

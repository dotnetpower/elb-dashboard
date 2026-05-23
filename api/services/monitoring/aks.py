"""AKS cluster ARM helpers (list + pool selection).

Responsibility: AKS cluster ARM helpers (list + pool selection).
Edit boundaries: Keep reusable domain logic here; routes and tasks call this layer.
Key entry points: list_aks_clusters, _select_workload_agent_pool, _kubelet_object_id
Risky contracts: Keep Azure credentials centralized and sanitise data at HTTP/log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from azure.core.credentials import TokenCredential

from api.services.azure_clients import aks_client

LOGGER = logging.getLogger(__name__)

BLAST_POOL_NAME = "blastpool"


def list_aks_clusters(
    credential: TokenCredential, subscription_id: str, resource_group: str
) -> list[dict[str, Any]]:
    client = aks_client(credential, subscription_id)
    clusters: list[dict[str, Any]] = []
    for cluster in client.managed_clusters.list_by_resource_group(resource_group):
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
        clusters.append(
            {
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
            }
        )
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

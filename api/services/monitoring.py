"""Read-only monitoring helpers for AKS, Storage, ACR, and the Remote Terminal."""

from __future__ import annotations

import logging
from typing import Any

from azure.core.credentials import TokenCredential

from services.azure_clients import (
    acr_client,
    aks_client,
    compute_client,
    storage_client,
)
from services.image_tags import IMAGE_TAGS

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AKS
# ---------------------------------------------------------------------------
def list_aks_clusters(
    credential: TokenCredential, subscription_id: str, resource_group: str
) -> list[dict[str, Any]]:
    client = aks_client(credential, subscription_id)
    out: list[dict[str, Any]] = []
    for cluster in client.managed_clusters.list_by_resource_group(resource_group):
        agent_pool = (cluster.agent_pool_profiles or [None])[0]
        out.append(
            {
                "name": cluster.name,
                "resource_group": resource_group,
                "region": cluster.location,
                "k8s_version": cluster.kubernetes_version,
                "provisioning_state": cluster.provisioning_state,
                "power_state": cluster.power_state.code if cluster.power_state else None,
                "node_count": agent_pool.count if agent_pool else None,
                "node_sku": agent_pool.vm_size if agent_pool else None,
                "kubelet_object_id": (
                    cluster.identity_profile.get("kubeletidentity").object_id
                    if cluster.identity_profile and "kubeletidentity" in cluster.identity_profile
                    else None
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def get_storage_summary(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
) -> dict[str, Any]:
    client = storage_client(credential, subscription_id)
    account = client.storage_accounts.get_properties(resource_group, account_name)
    containers = list(client.blob_containers.list(resource_group, account_name))
    return {
        "name": account.name,
        "region": account.location,
        "sku": account.sku.name if account.sku else None,
        "kind": account.kind,
        "public_network_access": account.public_network_access,
        "is_hns_enabled": account.is_hns_enabled,
        "containers": [
            {
                "name": c.name,
                "public_access": c.public_access,
                "last_modified_time": c.last_modified_time,
            }
            for c in containers
        ],
    }


def set_storage_public_access(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    enabled: bool,
) -> dict[str, Any]:
    """Toggle storage account public network access. Caller must propagate the wait."""
    client = storage_client(credential, subscription_id)
    LOGGER.info("set_storage_public_access account=%s enabled=%s", account_name, enabled)
    update = client.storage_accounts.update(
        resource_group,
        account_name,
        {"public_network_access": "Enabled" if enabled else "Disabled"},
    )
    return {"public_network_access": update.public_network_access}


# ---------------------------------------------------------------------------
# ACR
# ---------------------------------------------------------------------------
def list_acr_repositories(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
) -> dict[str, Any]:
    """Returns registry metadata and repositories with current vs expected tag info."""
    client = acr_client(credential, subscription_id)
    registry = client.registries.get(resource_group, registry_name)
    # The mgmt SDK does not list repositories — they live behind the data-plane API.
    # We surface only what mgmt knows; the SPA can call ACR data-plane separately if needed.
    return {
        "name": registry.name,
        "login_server": registry.login_server,
        "sku": registry.sku.name if registry.sku else None,
        "expected_image_tags": IMAGE_TAGS,
    }


# ---------------------------------------------------------------------------
# Remote Terminal VM
# ---------------------------------------------------------------------------
def get_vm_status(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
) -> dict[str, Any]:
    client = compute_client(credential, subscription_id)
    vm = client.virtual_machines.get(resource_group, vm_name, expand="instanceView")
    statuses = vm.instance_view.statuses if vm.instance_view else []
    power_state = next(
        (s.display_status for s in statuses if s.code and s.code.startswith("PowerState/")),
        None,
    )
    return {
        "name": vm.name,
        "region": vm.location,
        "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else None,
        "provisioning_state": vm.provisioning_state,
        "power_state": power_state,
    }

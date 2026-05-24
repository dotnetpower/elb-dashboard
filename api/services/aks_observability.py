"""AKS Container Insights addon helpers.

Responsibility: Read the AKS cluster's `omsagent` addon profile and toggle it
on/off by patching `addon_profiles.omsagent`.
Edit boundaries: ARM-only wrapper. HTTP shaping in
`api.routes.settings.aks_observability`. Long-running enablement runs through
`api.tasks.azure.aks_observability` because the addon patch is a
`begin_create_or_update` that can block 30-90 s.
Key entry points: `get_container_insights_status`,
`enable_container_insights`, `disable_container_insights`.
Risky contracts: The patch is *additive* — never read-modify-write the entire
cluster body. Always pass only `addon_profiles.omsagent` to avoid clobbering
other addons (azurepolicy, ingress-app-routing, etc.) the cluster may carry.
Validation: `uv run pytest -q api/tests/test_aks_observability_service.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError

from api.services.azure_clients import aks_client

LOGGER = logging.getLogger(__name__)


def get_container_insights_status(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Return the current Container Insights enablement state for the cluster.

    Response shape::

        {
          "enabled": bool,
          "workspace_resource_id": str | None,
          "cluster_provisioning_state": str | None,
        }

    Returns ``{"enabled": False, "workspace_resource_id": None, ...}`` when
    the cluster exists but has no `omsagent` addon. Raises ``ResourceNotFoundError``
    when the cluster itself is missing — the caller distinguishes that from
    "exists but observability off".
    """
    client = aks_client(credential, subscription_id)
    cluster = client.managed_clusters.get(resource_group, cluster_name)
    addons = cluster.addon_profiles or {}
    oms = addons.get("omsagent") or addons.get("omsAgent")

    enabled = bool(getattr(oms, "enabled", False)) if oms else False
    workspace_id: str | None = None
    if oms and oms.config:
        # Azure returns the config keys in PascalCase from the wire and the
        # SDK normalizes to either case depending on version; tolerate both.
        workspace_id = (
            oms.config.get("logAnalyticsWorkspaceResourceID")
            or oms.config.get("logAnalyticsWorkspaceResourceId")
        )
    return {
        "enabled": enabled,
        "workspace_resource_id": workspace_id,
        "cluster_provisioning_state": cluster.provisioning_state,
    }


def enable_container_insights(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    workspace_resource_id: str,
) -> dict[str, Any]:
    """Enable the omsagent addon for the cluster with the given LA workspace.

    Idempotent: if the addon is already enabled with the same workspace, the
    method returns the current state without touching ARM. If enabled with a
    *different* workspace, the workspace id is updated (this is the
    documented Azure operation).
    """
    current = get_container_insights_status(
        credential, subscription_id, resource_group, cluster_name
    )
    if current["enabled"] and current["workspace_resource_id"] == workspace_resource_id:
        return current

    client = aks_client(credential, subscription_id)
    # Fetch then mutate the existing cluster body so we keep all other
    # addon profiles intact (azurepolicy, ingress-app-routing, etc.).
    cluster = client.managed_clusters.get(resource_group, cluster_name)
    addons = cluster.addon_profiles or {}
    from azure.mgmt.containerservice.models import ManagedClusterAddonProfile

    addons["omsagent"] = ManagedClusterAddonProfile(
        enabled=True,
        config={"logAnalyticsWorkspaceResourceID": workspace_resource_id},
    )
    cluster.addon_profiles = addons

    poller = client.managed_clusters.begin_create_or_update(
        resource_group, cluster_name, cluster
    )
    updated = poller.result()
    LOGGER.info(
        "container insights enabled cluster=%s workspace=%s",
        cluster_name,
        workspace_resource_id.rsplit("/", 1)[-1],
    )
    addons_after = updated.addon_profiles or {}
    oms_after = addons_after.get("omsagent") or addons_after.get("omsAgent")
    config_after = (oms_after.config or {}) if oms_after else {}
    return {
        "enabled": bool(getattr(oms_after, "enabled", False)) if oms_after else False,
        "workspace_resource_id": (
            config_after.get("logAnalyticsWorkspaceResourceID")
            or config_after.get("logAnalyticsWorkspaceResourceId")
        ),
        "cluster_provisioning_state": updated.provisioning_state,
    }


def disable_container_insights(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Disable the omsagent addon while preserving the cluster's other addons.

    Idempotent: if the cluster has no omsagent addon, or the addon is already
    disabled, the current state is returned without a write.
    """
    current = get_container_insights_status(
        credential, subscription_id, resource_group, cluster_name
    )
    if not current["enabled"]:
        return current

    client = aks_client(credential, subscription_id)
    cluster = client.managed_clusters.get(resource_group, cluster_name)
    addons = cluster.addon_profiles or {}
    addon_key = "omsagent" if "omsagent" in addons else "omsAgent"
    oms = addons.get(addon_key)
    if oms is None:
        return current

    oms.enabled = False
    addons[addon_key] = oms
    cluster.addon_profiles = addons

    poller = client.managed_clusters.begin_create_or_update(
        resource_group, cluster_name, cluster
    )
    updated = poller.result()
    addons_after = updated.addon_profiles or {}
    oms_after = addons_after.get("omsagent") or addons_after.get("omsAgent")
    config_after = (oms_after.config or {}) if oms_after else {}
    LOGGER.info("container insights disabled cluster=%s", cluster_name)
    return {
        "enabled": bool(getattr(oms_after, "enabled", False)) if oms_after else False,
        "workspace_resource_id": (
            config_after.get("logAnalyticsWorkspaceResourceID")
            or config_after.get("logAnalyticsWorkspaceResourceId")
        ),
        "cluster_provisioning_state": updated.provisioning_state,
    }


def cluster_exists(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> bool:
    """Cheap probe used by routes before queueing the enable task."""
    client = aks_client(credential, subscription_id)
    try:
        client.managed_clusters.get(resource_group, cluster_name)
        return True
    except ResourceNotFoundError:
        return False

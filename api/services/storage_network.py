"""Network helpers for private workload Storage access.

Responsibility: Network helpers for private workload Storage access
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_resource_group_from_id`, `_private_dns_zone_id`,
`ensure_workload_storage_private_endpoints`
Risky contracts: Validate Storage account/blob inputs and preserve the no-browser-SAS policy.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations

import logging
import re

from azure.core.credentials import TokenCredential

from api.services.azure_clients import network_client, storage_client

LOGGER = logging.getLogger(__name__)

_STORAGE_GROUPS = ("blob", "dfs")
_RG_RE = re.compile(r"/resourceGroups/([^/]+)/", re.IGNORECASE)


def _resource_group_from_id(resource_id: str) -> str:
    match = _RG_RE.search(resource_id)
    if not match:
        raise ValueError(f"cannot parse resource group from resource id: {resource_id!r}")
    return match.group(1)


def _private_dns_zone_id(subscription_id: str, resource_group: str, group: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Network/privateDnsZones/privatelink.{group}.core.windows.net"
    )


def ensure_workload_storage_private_endpoints(
    credential: TokenCredential,
    subscription_id: str,
    storage_resource_group: str,
    account_name: str,
    location: str,
    private_endpoint_subnet_id: str,
    private_dns_zone_resource_group: str,
) -> list[dict[str, str]]:
    """Ensure blob + dfs private endpoints for a workload Storage account.

    The private endpoints are created in the platform VNet's private endpoint
    subnet, not in the workload AKS VNet. This lets the api, worker, and
    terminal sidecars reach Storage while public network access remains disabled.
    """
    if not private_endpoint_subnet_id or not private_dns_zone_resource_group:
        LOGGER.info("workload storage private endpoint config is unset; skipping")
        return []

    storage = storage_client(credential, subscription_id).storage_accounts.get_properties(
        storage_resource_group,
        account_name,
    )
    storage_id = storage.id or (
        f"/subscriptions/{subscription_id}/resourceGroups/{storage_resource_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{account_name}"
    )
    endpoint_resource_group = _resource_group_from_id(private_endpoint_subnet_id)
    client = network_client(credential, subscription_id)
    ensured: list[dict[str, str]] = []

    for group in _STORAGE_GROUPS:
        endpoint_name = f"pe-{account_name}-{group}"
        zone_id = _private_dns_zone_id(subscription_id, private_dns_zone_resource_group, group)
        LOGGER.info(
            "ensuring workload storage private endpoint account=%s group=%s endpoint=%s rg=%s",
            account_name,
            group,
            endpoint_name,
            endpoint_resource_group,
        )
        endpoint = client.private_endpoints.begin_create_or_update(
            endpoint_resource_group,
            endpoint_name,
            {
                "location": location,
                "subnet": {"id": private_endpoint_subnet_id},
                "private_link_service_connections": [
                    {
                        "name": f"{group}-link",
                        "private_link_service_id": storage_id,
                        "group_ids": [group],
                    }
                ],
            },
        ).result()
        client.private_dns_zone_groups.begin_create_or_update(
            endpoint_resource_group,
            endpoint_name,
            "default",
            {
                "private_dns_zone_configs": [
                    {
                        "name": group,
                        "private_dns_zone_id": zone_id,
                    }
                ]
            },
        ).result()
        ensured.append(
            {
                "name": endpoint_name,
                "group": group,
                "resource_group": endpoint_resource_group,
                "id": str(getattr(endpoint, "id", "") or ""),
            }
        )

    return ensured

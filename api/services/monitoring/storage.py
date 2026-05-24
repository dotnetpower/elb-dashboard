"""Storage account ARM summary + public-access flip.

Responsibility: Storage account ARM summary + public-access flip.
Edit boundaries: Keep reusable domain logic here; routes and tasks call this layer.
Key entry points: `get_storage_summary`, `set_storage_public_access` (+ helpers).
Risky contracts: Keep Azure credentials centralized and sanitise data at HTTP/log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.azure_clients import storage_client
from api.services.storage import usage_cache as storage_usage_cache

LOGGER = logging.getLogger(__name__)

_STORAGE_USAGE_DEFAULT_MAX_BLOBS = 10_000
_STORAGE_USAGE_HARD_MAX_BLOBS = 500_000


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _storage_usage_max_blobs_per_container() -> int | None:
    raw = os.environ.get(
        "STORAGE_USAGE_MAX_BLOBS_PER_CONTAINER",
        str(_STORAGE_USAGE_DEFAULT_MAX_BLOBS),
    ).strip()
    if raw.lower() in {"", "0", "none", "unlimited"}:
        return None
    try:
        value = int(raw)
    except ValueError:
        return _STORAGE_USAGE_DEFAULT_MAX_BLOBS
    return max(1, min(value, _STORAGE_USAGE_HARD_MAX_BLOBS))


def get_storage_summary(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
) -> dict[str, Any]:
    client = storage_client(credential, subscription_id)
    account = client.storage_accounts.get_properties(resource_group, account_name)
    out = {
        "name": account.name,
        "region": account.location,
        "sku": account.sku.name if account.sku else None,
        "kind": account.kind,
        "public_network_access": account.public_network_access,
        "is_hns_enabled": account.is_hns_enabled,
        "containers": [],
    }
    try:
        containers = list(client.blob_containers.list(resource_group, account_name))
    except Exception as exc:
        LOGGER.warning(
            "storage container list failed account=%s rg=%s: %s",
            account_name,
            resource_group,
            type(exc).__name__,
            exc_info=True,
        )
        out["containers_degraded"] = True
        out["containers_degraded_reason"] = type(exc).__name__
        return out

    container_rows = [
        {
            "name": container.name,
            "public_access": container.public_access,
            "last_modified_time": _iso_or_none(container.last_modified_time),
            "blob_count": None,
            "size_bytes": None,
            "usage_pending": False,
            "usage_truncated": False,
            "usage_error": None,
            "usage_cache_state": None,
            "usage_refreshed_at": None,
        }
        for container in containers
    ]
    try:
        usage_result = storage_usage_cache.cached_container_usage_summaries(
            credential,
            account_name,
            [str(container["name"]) for container in container_rows],
            max_blobs_per_container=_storage_usage_max_blobs_per_container(),
        )
    except Exception as exc:
        LOGGER.warning(
            "storage container usage failed account=%s rg=%s: %s",
            account_name,
            resource_group,
            type(exc).__name__,
            exc_info=True,
        )
        out["containers_usage_degraded"] = True
        out["containers_usage_degraded_reason"] = type(exc).__name__
    else:
        for container in container_rows:
            usage = usage_result.summaries.get(str(container["name"]))
            if usage is None:
                continue
            container["blob_count"] = usage.get("blob_count")
            container["size_bytes"] = usage.get("size_bytes")
            container["usage_pending"] = usage_result.pending
            container["usage_truncated"] = bool(usage.get("usage_truncated"))
            container["usage_error"] = usage.get("usage_error")
            container["usage_cache_state"] = usage_result.state
            container["usage_refreshed_at"] = usage_result.refreshed_at
        out["containers_usage_cache"] = {
            "state": usage_result.state,
            "hit": usage_result.hit,
            "pending": usage_result.pending,
            "age_seconds": usage_result.age_seconds,
            "refreshed_at": usage_result.refreshed_at,
        }

    out["containers"] = container_rows
    return out


def set_storage_public_access(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    enabled: bool,
) -> dict[str, Any]:
    """Toggle public network posture for a local-debug Storage account.

    VNet/service-endpoint rules need ``publicNetworkAccess=Enabled`` plus a
    restrictive ``defaultAction``. Accounts without VNet rules use the direct
    publicNetworkAccess toggle.
    """

    client = storage_client(credential, subscription_id)
    LOGGER.info("set_storage_public_access account=%s enabled=%s", account_name, enabled)

    account = client.storage_accounts.get_properties(resource_group, account_name)
    vnet_rules = getattr(account.network_rule_set, "virtual_network_rules", None) or []
    if vnet_rules:
        from azure.mgmt.storage.models import DefaultAction

        new_action = DefaultAction.ALLOW if enabled else DefaultAction.DENY
        update = client.storage_accounts.update(
            resource_group,
            account_name,
            {
                "public_network_access": "Enabled",
                "network_rule_set": {"default_action": new_action.value},
            },
        )
        return {
            "public_network_access": update.public_network_access,
            "default_action": new_action.value,
        }

    update = client.storage_accounts.update(
        resource_group,
        account_name,
        {"public_network_access": "Enabled" if enabled else "Disabled"},
    )
    return {"public_network_access": update.public_network_access}


# ---------------------------------------------------------------------------
# ACR
# ---------------------------------------------------------------------------

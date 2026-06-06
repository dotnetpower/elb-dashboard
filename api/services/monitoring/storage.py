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


def get_storage_account_detail(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
) -> dict[str, Any]:
    """Rich Well-Architected / CAF configuration surface for one Storage account.

    Reads the management properties plus the blob-service properties (soft
    delete / versioning / point-in-time restore live there, a second call). The
    blob-service read is best-effort: if it fails the account-level fields are
    still returned and the blob-service fields are left ``None`` (the rule then
    skips, never fabricates).
    """
    client = storage_client(credential, subscription_id)
    account = client.storage_accounts.get_properties(resource_group, account_name)

    encryption = getattr(account, "encryption", None)
    key_source = getattr(encryption, "key_source", None) if encryption is not None else None
    require_infra = (
        getattr(encryption, "require_infrastructure_encryption", None)
        if encryption is not None
        else None
    )
    network = getattr(account, "network_rule_set", None)
    pe = list(getattr(account, "private_endpoint_connections", None) or [])

    detail: dict[str, Any] = {
        "name": account.name,
        "region": account.location,
        "sku": account.sku.name if account.sku else None,
        "kind": account.kind,
        "access_tier": getattr(account, "access_tier", None),
        "public_network_access": account.public_network_access,
        "is_hns_enabled": account.is_hns_enabled,
        "https_only": getattr(account, "enable_https_traffic_only", None),
        "min_tls_version": getattr(account, "minimum_tls_version", None),
        "allow_blob_public_access": getattr(account, "allow_blob_public_access", None),
        "allow_shared_key_access": getattr(account, "allow_shared_key_access", None),
        "default_to_oauth": getattr(account, "default_to_o_auth_authentication", None),
        "cross_tenant_replication": getattr(account, "allow_cross_tenant_replication", None),
        "cmk": (str(key_source) == "Microsoft.Keyvault") if key_source is not None else None,
        "infrastructure_encryption": require_infra,
        "default_network_action": (
            getattr(network, "default_action", None) if network is not None else None
        ),
        "private_endpoint_count": len(pe),
        # Blob-service-level fields, filled below (best-effort).
        "blob_soft_delete": None,
        "container_soft_delete": None,
        "versioning": None,
        "point_in_time_restore": None,
        "change_feed": None,
    }

    try:
        props = client.blob_services.get_service_properties(resource_group, account_name)
        del_policy = getattr(props, "delete_retention_policy", None)
        cont_policy = getattr(props, "container_delete_retention_policy", None)
        restore = getattr(props, "restore_policy", None)
        change_feed = getattr(props, "change_feed", None)
        detail.update(
            {
                "blob_soft_delete": bool(getattr(del_policy, "enabled", False))
                if del_policy is not None
                else None,
                "container_soft_delete": bool(getattr(cont_policy, "enabled", False))
                if cont_policy is not None
                else None,
                "versioning": getattr(props, "is_versioning_enabled", None),
                "point_in_time_restore": bool(getattr(restore, "enabled", False))
                if restore is not None
                else None,
                "change_feed": bool(getattr(change_feed, "enabled", False))
                if change_feed is not None
                else None,
            }
        )
    except Exception as exc:
        LOGGER.warning(
            "storage blob-service props failed account=%s rg=%s: %s",
            account_name,
            resource_group,
            type(exc).__name__,
        )

    return detail


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

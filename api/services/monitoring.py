"""Azure resource monitoring and provisioning facade.

Responsibility: Azure resource monitoring and provisioning facade
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `list_aks_clusters`, `_select_workload_agent_pool`, `_kubelet_object_id`,
`get_storage_summary`, `set_storage_public_access`, `list_acr_repositories`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, cast

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError

from api.services.azure_clients import (
    acr_client,
    aks_client,
    compute_client,
    storage_client,
)
from api.services.image_tags import IMAGE_TAGS
from api.services.k8s.monitoring import (
    _get_k8s_session,
    k8s_cancel_blast_job,
    k8s_check_blast_status,
    k8s_check_namespace_exists,
    k8s_get_nodes,
    k8s_get_pods,
    k8s_get_service_ip,
    k8s_list_events,
    k8s_pod_logs,
    k8s_top_nodes,
    k8s_warmup_status,
)
from api.services.storage import usage_cache as storage_usage_cache
from api.services.storage.network import ensure_workload_storage_private_endpoints

LOGGER = logging.getLogger(__name__)
BLAST_POOL_NAME = "blastpool"
STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
ACR_PULL_ROLE_ID = "8311e382-0749-4cb8-b61a-304f252e45ec"
_STORAGE_USAGE_DEFAULT_MAX_BLOBS = 50_000
_STORAGE_USAGE_HARD_MAX_BLOBS = 500_000

__all__ = [
    "_get_k8s_session",
    "ensure_acr",
    "ensure_storage_account",
    "get_storage_summary",
    "get_vm_status",
    "k8s_cancel_blast_job",
    "k8s_check_blast_status",
    "k8s_check_namespace_exists",
    "k8s_get_nodes",
    "k8s_get_pods",
    "k8s_get_service_ip",
    "k8s_list_events",
    "k8s_pod_logs",
    "k8s_top_nodes",
    "k8s_warmup_status",
    "list_acr_repositories",
    "list_aks_clusters",
    "set_storage_public_access",
]


# ---------------------------------------------------------------------------
# AKS ARM summary
# ---------------------------------------------------------------------------
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


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


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
def list_acr_repositories(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
) -> dict[str, Any]:
    """Return registry metadata with actual vs expected image tag status."""

    management = acr_client(credential, subscription_id)
    registry = management.registries.get(resource_group, registry_name)
    login_server = registry.login_server or f"{registry_name}.azurecr.io"

    actual_tags: dict[str, list[str]] = {}
    building_images: list[str] = []
    build_details: list[dict[str, str]] = []
    # Persisted run_id -> {image, tag} mapping recorded at build submission
    # time. ACR's Run.output_images only populates after the push step
    # succeeds, so Queued/Started/Running runs typically have an empty
    # output_images list — without this mapping, the per-image rows in the
    # ACR card show a "Build" button after a browser refresh instead of
    # the correct "Building" state.
    pending_by_run_id: dict[str, dict[str, str]] = {}
    pruner = None
    try:
        from api.services import acr_build_state

        pending_by_run_id = acr_build_state.load_pending_builds(registry_name)
        pruner = acr_build_state.prune_terminal_builds
    except Exception as exc:
        LOGGER.debug(
            "acr_build_state load skipped (%s)", type(exc).__name__
        )

    terminal_run_ids: set[str] = set()
    try:
        from azure.mgmt.containerregistry import ContainerRegistryManagementClient

        preview = ContainerRegistryManagementClient(
            credential, subscription_id, api_version="2019-06-01-preview"
        )
        for run in preview.runs.list(resource_group, registry_name):
            status = run.status or ""
            run_id = run.run_id or ""
            if status == "Succeeded":
                if run.output_images:
                    _collect_succeeded_acr_images(actual_tags, run.output_images)
                if run_id:
                    terminal_run_ids.add(run_id)
            elif status in ("Queued", "Started", "Running"):
                if run.output_images:
                    _collect_building_acr_images(
                        building_images,
                        build_details,
                        status or "Unknown",
                        run_id,
                        run.output_images,
                    )
                elif run_id and run_id in pending_by_run_id:
                    # ACR hasn't filled output_images yet — fall back to
                    # the persisted submission record so the row shows the
                    # correct "Building" state.
                    mapping = pending_by_run_id[run_id]
                    full = f"{mapping['image']}:{mapping['tag']}"
                    if full not in building_images:
                        building_images.append(full)
                        build_details.append(
                            {"image": full, "status": status, "run_id": run_id}
                        )
            elif status in ("Failed", "Canceled", "Error", "Timeout"):
                if run_id:
                    terminal_run_ids.add(run_id)
    except Exception as exc:
        LOGGER.warning("ACR runs query failed (non-fatal): %s", type(exc).__name__)

    # Best-effort cleanup so the pending table doesn't grow without bound.
    # Only prune rows whose run we just observed reach a terminal status.
    if pruner is not None and pending_by_run_id:
        stale_run_ids = terminal_run_ids & set(pending_by_run_id.keys())
        if stale_run_ids:
            try:
                pruner(registry_name, stale_run_ids)
            except Exception as exc:
                LOGGER.debug(
                    "acr_build_state prune skipped (%s)", type(exc).__name__
                )

    return {
        "name": registry.name,
        "login_server": login_server,
        "sku": registry.sku.name if registry.sku else None,
        "expected_image_tags": IMAGE_TAGS,
        "actual_tags": actual_tags,
        "building_images": building_images,
        "build_details": build_details,
    }


def _collect_succeeded_acr_images(actual_tags: dict[str, list[str]], images: list[Any]) -> None:
    for image in images:
        repo = image.repository or ""
        tag = image.tag or ""
        if not repo or not tag:
            continue
        actual_tags.setdefault(repo, [])
        if tag not in actual_tags[repo]:
            actual_tags[repo].append(tag)


def _collect_building_acr_images(
    building_images: list[str],
    build_details: list[dict[str, str]],
    status: str,
    run_id: str,
    images: list[Any],
) -> None:
    for image in images:
        full = f"{image.repository or ''}:{image.tag or ''}"
        if full in building_images:
            continue
        building_images.append(full)
        build_details.append({"image": full, "status": status, "run_id": run_id})


# ---------------------------------------------------------------------------
# Remote Terminal VM (legacy status surface)
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
        (
            status.display_status
            for status in statuses
            if status.code and status.code.startswith("PowerState/")
        ),
        None,
    )

    os_disk_gb: int | None = None
    if vm.storage_profile and vm.storage_profile.os_disk:
        os_disk_gb = vm.storage_profile.os_disk.disk_size_gb

    identity_type: str | None = None
    has_managed_identity = False
    if vm.identity:
        identity_type = vm.identity.type
        has_managed_identity = identity_type in (
            "SystemAssigned",
            "SystemAssigned, UserAssigned",
        )

    public_ip, fqdn = _resolve_vm_public_endpoint(credential, subscription_id, vm)

    return {
        "name": vm.name,
        "region": vm.location,
        "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else None,
        "provisioning_state": vm.provisioning_state,
        "power_state": power_state,
        "os_disk_gb": os_disk_gb,
        "public_ip": public_ip,
        "fqdn": fqdn,
        "has_managed_identity": has_managed_identity,
        "identity_type": identity_type,
    }


def _resolve_vm_public_endpoint(
    credential: TokenCredential,
    subscription_id: str,
    vm: Any,
) -> tuple[str | None, str | None]:
    try:
        from azure.mgmt.network import NetworkManagementClient

        if not vm.network_profile or not vm.network_profile.network_interfaces:
            return None, None
        network_client = NetworkManagementClient(credential, subscription_id)
        nic_id = vm.network_profile.network_interfaces[0].id
        nic_parts = nic_id.split("/")
        nic_rg = nic_parts[nic_parts.index("resourceGroups") + 1]
        nic_name = nic_parts[-1]
        nic = network_client.network_interfaces.get(nic_rg, nic_name)
        if not nic.ip_configurations:
            return None, None
        public_ip_ref = nic.ip_configurations[0].public_ip_address
        if not public_ip_ref or not public_ip_ref.id:
            return None, None
        public_ip_parts = public_ip_ref.id.split("/")
        public_ip_rg = public_ip_parts[public_ip_parts.index("resourceGroups") + 1]
        public_ip_name = public_ip_parts[-1]
        public_ip = network_client.public_ip_addresses.get(public_ip_rg, public_ip_name)
        fqdn = public_ip.dns_settings.fqdn if public_ip.dns_settings else None
        return public_ip.ip_address, fqdn
    except Exception as exc:
        LOGGER.debug("could not resolve public IP for %s: %s", vm.name, exc)
        return None, None


# ---------------------------------------------------------------------------
# Resource creation (idempotent)
# ---------------------------------------------------------------------------
def _auto_assign_role(
    credential: TokenCredential,
    subscription_id: str,
    principal_id: str,
    scope: str,
    role_definition_id: str,
    principal_type: str = "User",
) -> bool:
    """Assign a role to a principal. Idempotent — ignores conflict."""

    import uuid as _uuid

    from azure.mgmt.authorization import AuthorizationManagementClient

    auth_client = AuthorizationManagementClient(credential, subscription_id)
    role_def = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{role_definition_id}"
    )
    assignment_name = str(
        _uuid.uuid5(_uuid.NAMESPACE_URL, f"{scope}:{principal_id}:{role_definition_id}")
    )
    try:
        auth_client.role_assignments.create(
            scope,
            assignment_name,
            {
                "role_definition_id": role_def,
                "principal_id": principal_id,
                "principal_type": principal_type,
            },
        )
        LOGGER.info(
            "RBAC assigned role=%s principal=%s scope=%s",
            role_definition_id[:8],
            principal_id[:8],
            scope.split("/")[-1],
        )
        return True
    except Exception as exc:
        if "Conflict" in str(exc) or "RoleAssignmentExists" in str(exc):
            LOGGER.debug("Role already assigned, skipping")
            return True
        else:
            LOGGER.warning("RBAC assignment failed: %s", str(exc)[:200])
            return False


def _current_managed_identity_principal_id() -> str:
    """Return the current app UAMI principal id when the deployment injected it."""

    return os.environ.get("SHARED_IDENTITY_PRINCIPAL_ID", "").strip()


def ensure_storage_account(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    region: str,
    caller_oid: str = "",
    private_endpoint_subnet_id: str = "",
    private_dns_zone_resource_group: str = "",
) -> None:
    """Create a Standard_LRS HNS-enabled storage account and default containers."""

    client = storage_client(credential, subscription_id)
    LOGGER.info("ensure_storage_account account=%s rg=%s", account_name, resource_group)
    try:
        client.storage_accounts.get_properties(resource_group, account_name)
    except ResourceNotFoundError:
        poller = client.storage_accounts.begin_create(
            resource_group,
            account_name,
            {
                "location": region,
                "sku": {"name": "Standard_LRS"},
                "kind": "StorageV2",
                "is_hns_enabled": True,
                "public_network_access": "Disabled",
                "minimum_tls_version": "TLS1_2",
                "tags": {"managed-by": "elb-dashboard"},
            },
        )
        poller.result()

    for container_name in ("blast-db", "queries", "results"):
        try:
            client.blob_containers.create(resource_group, account_name, container_name, {})
        except Exception:  # noqa: S110 - container may already exist
            pass

    storage_scope = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{account_name}"
    )

    if caller_oid:
        _auto_assign_role(
            credential,
            subscription_id,
            caller_oid,
            storage_scope,
            STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID,
            principal_type="User",
        )

    uami_principal_id = _current_managed_identity_principal_id()
    if uami_principal_id:
        assigned = _auto_assign_role(
            credential,
            subscription_id,
            uami_principal_id,
            storage_scope,
            STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID,
            principal_type="ServicePrincipal",
        )
        if not assigned:
            raise RuntimeError(
                "failed to assign Storage Blob Data Contributor to the shared "
                "managed identity; grant the control-plane identity User Access "
                "Administrator on the Storage scope or assign the Blob role manually"
            )

    ensure_workload_storage_private_endpoints(
        credential,
        subscription_id,
        resource_group,
        account_name,
        region,
        private_endpoint_subnet_id,
        private_dns_zone_resource_group,
    )


def ensure_acr(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
    region: str,
    caller_oid: str = "",
) -> None:
    """Create a Standard SKU ACR and assign caller RBAC when requested."""

    client = acr_client(credential, subscription_id)
    LOGGER.info("ensure_acr registry=%s rg=%s", registry_name, resource_group)
    poller = client.registries.begin_create(
        resource_group,
        registry_name,
        {
            "location": region,
            "sku": {"name": "Standard"},
            "admin_user_enabled": False,
            "tags": {"managed-by": "elb-dashboard"},
        },
    )
    poller.result()

    if caller_oid:
        _auto_assign_role(
            credential,
            subscription_id,
            caller_oid,
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.ContainerRegistry/registries/{registry_name}",
            ACR_PULL_ROLE_ID,
        )

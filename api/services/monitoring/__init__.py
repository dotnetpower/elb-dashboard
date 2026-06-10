"""Azure resource monitoring + provisioning subpackage (split from monitoring.py).

Responsibility: Group AKS/storage/ACR/VM/provisioning helpers under one namespace.
Edit boundaries: Submodules own behaviour; this `__init__` only aggregates exports.
Key entry points: see `__all__`.
Risky contracts: Keep Azure credentials centralized; sanitise data at HTTP/log boundaries.
Validation: `uv run pytest -q api/tests/test_monitoring_aks_pools.py`.
"""

from __future__ import annotations

from typing import Any

from azure.core.credentials import TokenCredential

from api.services.azure_clients import acr_client, aks_client, storage_client

# Re-export k8s.* symbols for callers that historically used
# `from api.services.monitoring import k8s_get_pods` etc.
from api.services.k8s.monitoring import (
    SYSTEM_NAMESPACES,
    _get_k8s_session,
    k8s_cancel_blast_job,
    k8s_check_blast_status,
    k8s_check_namespace_exists,
    k8s_deployment_delete,
    k8s_deployment_describe,
    k8s_deployment_logs,
    k8s_get_deployments,
    k8s_get_jobs,
    k8s_get_nodes,
    k8s_get_pods,
    k8s_get_service_ip,
    k8s_job_delete,
    k8s_job_describe,
    k8s_job_logs,
    k8s_list_events,
    k8s_pod_delete,
    k8s_pod_describe,
    k8s_pod_logs,
    k8s_top_nodes,
    k8s_warmup_status,
)
from api.services.monitoring import acr as _acr_module
from api.services.monitoring import aks as _aks_module
from api.services.monitoring import provisioning as _provisioning_module
from api.services.monitoring.aks import (
    BLAST_POOL_NAME,
)
from api.services.monitoring.provisioning import (
    ACR_CONTRIBUTOR_ROLE_ID,
    ACR_PULL_ROLE_ID,
    ACR_PUSH_ROLE_ID,
    STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID,
    _auto_assign_role,
)
from api.services.monitoring.storage import (
    get_storage_account_detail,
    get_storage_summary,
    set_storage_public_access,
)
from api.services.monitoring.vm import get_vm_status
from api.services.storage.network import ensure_workload_storage_private_endpoints


def list_aks_clusters(
    credential: TokenCredential, subscription_id: str, resource_group: str
) -> list[dict[str, Any]]:
    _aks_module.aks_client = aks_client
    return _aks_module.list_aks_clusters(credential, subscription_id, resource_group)


def get_aks_cluster_snapshot(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any] | None:
    _aks_module.aks_client = aks_client
    return _aks_module.get_aks_cluster_snapshot(
        credential, subscription_id, resource_group, cluster_name
    )


def list_aks_clusters_in_subscription(
    credential: TokenCredential,
    subscription_id: str,
    *,
    include_unmanaged: bool = False,
) -> list[dict[str, Any]]:
    _aks_module.aks_client = aks_client
    return _aks_module.list_aks_clusters_in_subscription(
        credential, subscription_id, include_unmanaged=include_unmanaged
    )


def list_aks_clusters_detail_in_subscription(
    credential: TokenCredential, subscription_id: str
) -> list[dict[str, Any]]:
    _aks_module.aks_client = aks_client
    return _aks_module.list_aks_clusters_detail_in_subscription(credential, subscription_id)


def get_acr_registry_detail(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
) -> dict[str, Any]:
    _acr_module.acr_client = acr_client
    return _acr_module.get_acr_registry_detail(
        credential, subscription_id, resource_group, registry_name
    )


def list_acr_repositories(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
) -> dict[str, Any]:
    _acr_module.acr_client = acr_client
    return _acr_module.list_acr_repositories(
        credential, subscription_id, resource_group, registry_name
    )


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
    _provisioning_module.storage_client = storage_client
    _provisioning_module._auto_assign_role = _auto_assign_role
    _provisioning_module.ensure_workload_storage_private_endpoints = (
        ensure_workload_storage_private_endpoints
    )
    return _provisioning_module.ensure_storage_account(
        credential,
        subscription_id,
        resource_group,
        account_name,
        region,
        caller_oid=caller_oid,
        private_endpoint_subnet_id=private_endpoint_subnet_id,
        private_dns_zone_resource_group=private_dns_zone_resource_group,
    )


def ensure_acr(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
    region: str,
    caller_oid: str = "",
) -> None:
    _provisioning_module.acr_client = acr_client
    _provisioning_module._auto_assign_role = _auto_assign_role
    return _provisioning_module.ensure_acr(
        credential,
        subscription_id,
        resource_group,
        registry_name,
        region,
        caller_oid=caller_oid,
    )

__all__ = [
    "ACR_CONTRIBUTOR_ROLE_ID",
    "ACR_PULL_ROLE_ID",
    "ACR_PUSH_ROLE_ID",
    "BLAST_POOL_NAME",
    "STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID",
    "SYSTEM_NAMESPACES",
    "_auto_assign_role",
    "_get_k8s_session",
    "acr_client",
    "aks_client",
    "ensure_acr",
    "ensure_storage_account",
    "ensure_workload_storage_private_endpoints",
    "get_acr_registry_detail",
    "get_storage_account_detail",
    "get_storage_summary",
    "get_vm_status",
    "k8s_cancel_blast_job",
    "k8s_check_blast_status",
    "k8s_check_namespace_exists",
    "k8s_deployment_delete",
    "k8s_deployment_describe",
    "k8s_deployment_logs",
    "k8s_get_deployments",
    "k8s_get_jobs",
    "k8s_get_nodes",
    "k8s_get_pods",
    "k8s_get_service_ip",
    "k8s_job_delete",
    "k8s_job_describe",
    "k8s_job_logs",
    "k8s_list_events",
    "k8s_pod_delete",
    "k8s_pod_describe",
    "k8s_pod_logs",
    "k8s_top_nodes",
    "k8s_warmup_status",
    "list_acr_repositories",
    "list_aks_clusters",
    "list_aks_clusters_detail_in_subscription",
    "list_aks_clusters_in_subscription",
    "set_storage_public_access",
    "storage_client",
]

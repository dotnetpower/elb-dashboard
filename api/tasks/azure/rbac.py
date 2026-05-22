"""Runtime RBAC helpers for the AKS kubelet identity + `assign_aks_roles` task.

Responsibility: Grant the AKS kubelet identity the runtime roles it needs (`AcrPull`
    on the registry, `Storage Blob Data Contributor` on the workload storage account)
    and expose the same flow as a stand-alone Celery task for the SPA's "Re-assign
    roles" affordance.
Edit boundaries: All AKS-kubelet role-assignment writes belong here. The provision
    task calls `ensure_aks_runtime_rbac`; routes call the `assign_aks_roles` task by
    string name.
Key entry points: `attach_acr`, `grant_storage_blob_contributor_to_aks`,
    `ensure_aks_runtime_rbac`, `assign_aks_roles` (Celery task
    `api.tasks.azure.assign_aks_roles`).
Risky contracts: Task name `api.tasks.azure.assign_aks_roles` must not change — the
    SPA + tests reference it. Role assignment Conflicts / RoleAssignmentExists are
    treated as success; other failures are non-fatal but recorded in `roles_failed`.
Validation: `uv run pytest -q api/tests/test_azure_tasks.py
    api/tests/test_warmup_route.py`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from celery import shared_task

import api.tasks.azure as _facade

LOGGER = logging.getLogger(__name__)


# Tests monkeypatch `api.tasks.azure.aks_client` / `acr_client` / `storage_client` /
# `get_credential` / `_attach_acr` / `_grant_storage_blob_contributor_to_aks`. Look
# the symbols up on the package at call time so those patches take effect here.
def aks_client(cred: Any, subscription_id: str) -> Any:
    return _facade.aks_client(cred, subscription_id)


def acr_client(cred: Any, subscription_id: str) -> Any:
    return _facade.acr_client(cred, subscription_id)


def storage_client(cred: Any, subscription_id: str) -> Any:
    return _facade.storage_client(cred, subscription_id)


def get_credential() -> Any:
    return _facade.get_credential()


def attach_acr(
    cred: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    acr_resource_group: str,
    acr_name: str,
) -> None:
    """Grant AcrPull to the AKS kubelet identity on the ACR."""
    from azure.mgmt.authorization import AuthorizationManagementClient
    from azure.mgmt.authorization.models import RoleAssignmentCreateParameters

    aks_cl = aks_client(cred, subscription_id)
    cluster = aks_cl.managed_clusters.get(resource_group, cluster_name)

    kubelet_oid = None
    if cluster.identity_profile and "kubeletidentity" in cluster.identity_profile:
        kubelet_oid = cluster.identity_profile["kubeletidentity"].object_id

    if not kubelet_oid:
        LOGGER.warning("No kubelet identity found, skipping ACR attach")
        return

    acr_cl = acr_client(cred, subscription_id)
    registry = acr_cl.registries.get(acr_resource_group, acr_name)
    acr_scope = registry.id

    # AcrPull role definition ID (well-known)
    acr_pull_role = "7f951dda-4ed3-4680-a7ca-43fe172d538d"

    auth_cl = AuthorizationManagementClient(cred, subscription_id)
    role_definition_id = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{acr_pull_role}"
    )
    role_assignment_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{acr_scope}|{kubelet_oid}|{acr_pull_role}")
    )
    try:
        auth_cl.role_assignments.create(
            scope=acr_scope,
            role_assignment_name=role_assignment_id,
            parameters=RoleAssignmentCreateParameters(  # type: ignore[call-arg]
                role_definition_id=role_definition_id,
                principal_id=kubelet_oid,
                principal_type="ServicePrincipal",
            ),
        )
        LOGGER.info("AcrPull role assigned to %s on %s", kubelet_oid, acr_name)
    except Exception as exc:
        if "RoleAssignmentExists" in str(exc):
            LOGGER.info("AcrPull role already assigned")
        else:
            raise


def grant_storage_blob_contributor_to_aks(
    cred: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_resource_group: str,
    storage_account: str,
) -> None:
    """Grant Storage Blob Data Contributor to the AKS kubelet identity."""
    from azure.mgmt.authorization import AuthorizationManagementClient
    from azure.mgmt.authorization.models import RoleAssignmentCreateParameters

    aks_cl = aks_client(cred, subscription_id)
    cluster = aks_cl.managed_clusters.get(resource_group, cluster_name)

    kubelet_oid = None
    if cluster.identity_profile and "kubeletidentity" in cluster.identity_profile:
        kubelet_oid = cluster.identity_profile["kubeletidentity"].object_id

    if not kubelet_oid:
        LOGGER.warning("No kubelet identity found, skipping Storage Blob Data Contributor")
        return

    storage = storage_client(cred, subscription_id).storage_accounts.get_properties(
        storage_resource_group,
        storage_account,
    )
    storage_scope = storage.id
    blob_contributor_role = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"

    auth_cl = AuthorizationManagementClient(cred, subscription_id)
    role_definition_id = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{blob_contributor_role}"
    )
    role_assignment_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{storage_scope}|{kubelet_oid}|{blob_contributor_role}")
    )
    try:
        auth_cl.role_assignments.create(
            scope=storage_scope,
            role_assignment_name=role_assignment_id,
            parameters=RoleAssignmentCreateParameters(  # type: ignore[call-arg]
                role_definition_id=role_definition_id,
                principal_id=kubelet_oid,
                principal_type="ServicePrincipal",
            ),
        )
        LOGGER.info(
            "Storage Blob Data Contributor role assigned to %s on %s",
            kubelet_oid,
            storage_account,
        )
    except Exception as exc:
        if "RoleAssignmentExists" in str(exc):
            LOGGER.info("Storage Blob Data Contributor role already assigned")
        else:
            raise


def ensure_aks_runtime_rbac(
    cred: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    acr_resource_group: str = "",
    acr_name: str = "",
    storage_resource_group: str = "",
    storage_account: str = "",
) -> dict[str, Any]:
    """Best-effort runtime RBAC ensure for the AKS kubelet identity."""
    roles_assigned: list[str] = []
    roles_failed: dict[str, str] = {}

    if acr_name and acr_resource_group:
        try:
            _facade._attach_acr(
                cred, subscription_id, resource_group, cluster_name, acr_resource_group, acr_name
            )
            roles_assigned.append("AcrPull")
        except Exception as exc:
            LOGGER.warning("AcrPull assignment failed (non-fatal): %s", exc)
            roles_failed["AcrPull"] = str(exc)[:300]

    if storage_account and storage_resource_group:
        try:
            _facade._grant_storage_blob_contributor_to_aks(
                cred,
                subscription_id,
                resource_group,
                cluster_name,
                storage_resource_group,
                storage_account,
            )
            roles_assigned.append("Storage Blob Data Contributor")
        except Exception as exc:
            LOGGER.warning("Storage Blob Data Contributor assignment failed (non-fatal): %s", exc)
            roles_failed["Storage Blob Data Contributor"] = str(exc)[:300]

    return {
        "cluster_name": cluster_name,
        "roles_assigned": roles_assigned,
        "roles_failed": roles_failed,
    }


@shared_task(name="api.tasks.azure.assign_aks_roles", bind=True, max_retries=2)
def assign_aks_roles(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    acr_resource_group: str = "",
    acr_name: str = "",
    storage_resource_group: str = "",
    storage_account: str = "",
) -> dict[str, Any]:
    """Assign runtime RBAC roles to the AKS kubelet identity."""
    cred = _facade.get_credential()
    summary = _facade._ensure_aks_runtime_rbac(
        cred,
        subscription_id,
        resource_group,
        cluster_name,
        acr_resource_group=acr_resource_group,
        acr_name=acr_name,
        storage_resource_group=storage_resource_group or resource_group,
        storage_account=storage_account,
    )
    return {**summary, "status": "completed"}

"""RBAC + workload-identity wiring for the ``elb-openapi`` deploy.

Responsibility: Idempotently create the managed identity, federated credential, and
    role assignments that the on-cluster ``elb-openapi`` pod uses to call ARM / Storage
    via AKS Workload Identity.
Edit boundaries: All identity / role assignment writes live here. The manifests module
    consumes the returned `mi_client_id`; the deploy task wires them together.
Key entry points: `assign_role_idempotent`, `setup_workload_identity`.
Risky contracts: `setup_workload_identity` must remain idempotent — re-running the
    deploy task should never produce duplicate MI or federated credentials. Role
    assignments are best-effort (Conflict / RoleAssignmentExists is treated as success).
Validation: `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from api.services.azure_clients import aks_client
from api.tasks.openapi.constants import (
    FED_CRED_NAME,
    K8S_NAMESPACE,
    K8S_SA_NAME,
    MI_NAME,
    ROLE_AKS_CLUSTER_USER,
    ROLE_CONTRIBUTOR,
    ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR,
)

LOGGER = logging.getLogger(__name__)


def assign_role_idempotent(
    auth_client: Any,
    scope: str,
    principal_id: str,
    role_definition_id: str,
    label: str,
) -> bool:
    """Create a role assignment; return True on create / already-exists."""

    role_def = f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{role_definition_id}"
    name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}:{principal_id}:{role_definition_id}"))
    try:
        auth_client.role_assignments.create(
            scope,
            name,
            {
                "role_definition_id": role_def,
                "principal_id": principal_id,
                "principal_type": "ServicePrincipal",
            },
        )
        LOGGER.info(
            "RBAC role=%s principal=%s scope=%s assigned",
            label,
            principal_id[:8],
            scope.split("/")[-1],
        )
        return True
    except Exception as exc:
        msg = str(exc)
        if "RoleAssignmentExists" in msg or "Conflict" in msg:
            LOGGER.info("RBAC role=%s already assigned", label)
            return True
        LOGGER.warning("RBAC role=%s failed: %s", label, msg[:200])
        return False


def setup_workload_identity(
    cred: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    region: str,
    storage_account: str,
    storage_resource_group: str,
) -> dict[str, Any]:
    """Create MI + Federated Credential + role assignments. Idempotent."""

    from azure.mgmt.authorization import AuthorizationManagementClient
    from azure.mgmt.msi import ManagedServiceIdentityClient

    # 1. OIDC issuer URL from the cluster (must already be enabled).
    aks = aks_client(cred, subscription_id)
    cluster = aks.managed_clusters.get(resource_group, cluster_name)
    oidc_url = (cluster.oidc_issuer_profile.issuer_url if cluster.oidc_issuer_profile else "") or ""
    if not oidc_url:
        raise RuntimeError(
            f"AKS cluster {cluster_name!r} does not expose an OIDC issuer "
            "URL. Re-provision the cluster with oidc_issuer_profile.enabled "
            "and security_profile.workload_identity.enabled set to True, "
            "then retry the OpenAPI deployment."
        )

    # 2. User-Assigned Managed Identity (idempotent create_or_update).
    msi = ManagedServiceIdentityClient(cred, subscription_id)
    mi = msi.user_assigned_identities.create_or_update(
        resource_group,
        MI_NAME,
        {
            "location": region,
            "tags": {
                "purpose": "elb-openapi-workload-identity",
                "managedBy": "elb-dashboard",
            },
        },
    )

    # 3. Federated Identity Credential — AKS OIDC ↔ K8s ServiceAccount.
    msi.federated_identity_credentials.create_or_update(
        resource_group,
        MI_NAME,
        FED_CRED_NAME,
        {
            "issuer": oidc_url,
            "subject": f"system:serviceaccount:{K8S_NAMESPACE}:{K8S_SA_NAME}",
            "audiences": ["api://AzureADTokenExchange"],
        },
    )

    # 4. Role assignments (best-effort — never fatal).
    auth = AuthorizationManagementClient(cred, subscription_id)
    roles_assigned: list[str] = []
    roles_failed: list[str] = []

    def _try(scope: str, role_id: str, label: str) -> None:
        if assign_role_idempotent(auth, scope, mi.principal_id, role_id, label):
            roles_assigned.append(label)
        else:
            roles_failed.append(label)

    _try(
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}",
        ROLE_CONTRIBUTOR,
        "Contributor",
    )
    if storage_account:
        storage_rg = storage_resource_group or resource_group
        _try(
            (
                f"/subscriptions/{subscription_id}/resourceGroups/{storage_rg}/"
                f"providers/Microsoft.Storage/storageAccounts/{storage_account}"
            ),
            ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR,
            "StorageBlobDataContributor",
        )
    _try(
        (
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/"
            f"providers/Microsoft.ContainerService/managedClusters/{cluster_name}"
        ),
        ROLE_AKS_CLUSTER_USER,
        "AzureKubernetesServiceClusterUserRole",
    )

    return {
        "mi_name": MI_NAME,
        "mi_client_id": mi.client_id,
        "mi_principal_id": mi.principal_id,
        "oidc_issuer": oidc_url,
        "federated_credential": FED_CRED_NAME,
        "roles_assigned": roles_assigned,
        "roles_failed": roles_failed,
    }

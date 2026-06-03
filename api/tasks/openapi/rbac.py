"""RBAC + workload-identity wiring for the ``elb-openapi`` deploy.

Responsibility: Idempotently create the managed identity, federated credential, and
    role assignments that the on-cluster ``elb-openapi`` pod uses to call ARM / Storage
    via AKS Workload Identity.
Edit boundaries: All identity / role assignment writes live here. The manifests module
    consumes the returned `mi_client_id`; the deploy task wires them together.
Key entry points: `assign_role_idempotent`, `setup_workload_identity`.
Risky contracts: `setup_workload_identity` must remain idempotent — re-running the
    deploy task should never produce duplicate MI or federated credentials. Role
    assignments treat `RoleAssignmentExists` / `Conflict` as success (re-deploy is the
    common case); permission / scope / transient errors propagate as a single
    ``RuntimeError`` so the deploy task can return ``status: failed`` instead of
    "succeeded but pod has no permissions".
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
    ROLE_AKS_CLUSTER_USER,
    ROLE_CONTRIBUTOR,
    ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR,
    mi_name_for_cluster,
)

LOGGER = logging.getLogger(__name__)


def assign_role_idempotent(
    auth_client: Any,
    scope: str,
    principal_id: str,
    role_definition_id: str,
    label: str,
) -> tuple[bool, str]:
    """Create a role assignment; return ``(ok, reason)``.

    ``ok=True`` when the assignment was created or already exists.
    ``ok=False`` with a short ``reason`` string when the assignment genuinely
    failed (permission denied, invalid scope, transient API error). Callers
    must propagate ``ok=False`` instead of swallowing it — a "deploy
    succeeded but the pod can't call ARM/Storage" outcome is the worst kind
    of silent failure for the BLAST submit path.
    """

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
        return (True, "created")
    except Exception as exc:
        msg = str(exc)
        if "RoleAssignmentExists" in msg or "Conflict" in msg:
            LOGGER.info("RBAC role=%s already assigned", label)
            return (True, "exists")
        LOGGER.warning("RBAC role=%s failed: %s", label, msg[:200])
        return (False, msg[:300])


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
    """Create MI + Federated Credential + role assignments.

    Idempotent. Raises ``RuntimeError`` when a role assignment fails so the
    deploy task can return ``status: failed`` instead of marking the pod
    "deployed" while it lacks the permissions to call ARM/Storage. The
    return shape (``mi_client_id`` etc.) is unchanged.
    """

    from api.services.azure_clients import authorization_client, msi_client

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
    # Per-cluster name so two clusters in the same resource group keep
    # isolated identities + federated credentials instead of overwriting
    # one another (see constants.mi_name_for_cluster).
    mi_name = mi_name_for_cluster(subscription_id, cluster_name)
    msi = msi_client(cred, subscription_id)
    mi = msi.user_assigned_identities.create_or_update(
        resource_group,
        mi_name,
        {
            "location": region,
            "tags": {
                "purpose": "elb-openapi-workload-identity",
                "managedBy": "elb-dashboard",
                "cluster": cluster_name,
            },
        },
    )

    # 3. Federated Identity Credential — AKS OIDC ↔ K8s ServiceAccount.
    msi.federated_identity_credentials.create_or_update(
        resource_group,
        mi_name,
        FED_CRED_NAME,
        {
            "issuer": oidc_url,
            "subject": f"system:serviceaccount:{K8S_NAMESPACE}:{K8S_SA_NAME}",
            "audiences": ["api://AzureADTokenExchange"],
        },
    )

    # 4. Role assignments — RoleAssignmentExists / Conflict is success; any
    # other failure is fatal. The pod cannot perform its job without these
    # roles, and surfacing "succeeded" while the pod 403s on first call is
    # the failure mode this branch exists to prevent.
    auth = authorization_client(cred, subscription_id)
    roles_assigned: list[str] = []
    roles_failed: list[tuple[str, str]] = []

    def _try(scope: str, role_id: str, label: str) -> None:
        ok, reason = assign_role_idempotent(auth, scope, mi.principal_id, role_id, label)
        if ok:
            roles_assigned.append(label)
        else:
            roles_failed.append((label, reason))

    _try(
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}",
        ROLE_CONTRIBUTOR,
        "Contributor",
    )
    if storage_account:
        if not storage_resource_group:
            # Refuse to silently fall back to the AKS cluster RG: the
            # workload Storage account commonly lives in a different RG,
            # and routing the role assignment at the cluster RG produces
            # a confusing ARM 404 ("storage account not found in
            # rg-elb-cluster") that masks the real issue (caller did not
            # plumb storage_resource_group through). Surface it cleanly.
            roles_failed.append(
                (
                    "StorageBlobDataContributor",
                    "storage_resource_group is required when storage_account "
                    "is set; the caller must pass the Storage account's "
                    "resource group (do not rely on the AKS cluster RG)",
                )
            )
        else:
            _try(
                (
                    f"/subscriptions/{subscription_id}/resourceGroups/{storage_resource_group}/"
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

    if roles_failed:
        failed_labels = ", ".join(f"{label} ({reason})" for label, reason in roles_failed)
        raise RuntimeError(
            "Workload Identity setup failed at role assignment: "
            f"{failed_labels}. The elb-openapi pod cannot call ARM/Storage "
            "without these roles. Verify the deployer has User Access "
            "Administrator on the target scope, then re-run."
        )

    return {
        "mi_name": mi_name,
        "mi_client_id": mi.client_id,
        "mi_principal_id": mi.principal_id,
        "oidc_issuer": oidc_url,
        "federated_credential": FED_CRED_NAME,
        "roles_assigned": roles_assigned,
        "roles_failed": [label for label, _ in roles_failed],
    }

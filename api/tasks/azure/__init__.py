"""Azure infrastructure Celery tasks - AKS provision / start / stop / delete.

Responsibility: Azure infrastructure Celery tasks - AKS provision / start / stop / delete
Edit boundaries: Keep long-running side effects here; route handlers should enqueue tasks and
persist state.
Key entry points: `_now_iso`, `diag_noop`, `_update_state`, `provision_aks`, `start_aks`,
`stop_aks`
Risky contracts: Tasks should be idempotent, retry-aware, and write progress/state checkpoints.
Validation: `uv run pytest -q api/tests/test_azure_tasks.py api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Any

from celery import shared_task

from api.services import get_credential
from api.services.azure_clients import (
    acr_client,
    aks_client,
    storage_client,
)

LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


@shared_task(name="api.tasks.azure.diag_noop", bind=True, max_retries=0)
def diag_noop(self: Any, *, message: str = "ping") -> dict[str, Any]:
    """Diagnostic-only no-op task — proves enqueue ↔ consume round-trip works."""
    LOGGER.info("DIAG_NOOP message=%r task_id=%s", message, self.request.id)
    return {"message": message, "task_id": self.request.id, "ts": _now_iso()}


def _update_state(task_id: str, phase: str, status: str = "running", **extra: Any) -> None:
    """Best-effort state update to the job state repo."""
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(task_id)
        if state:
            state.status = status
            state.phase = phase
            state.updated_at = _now_iso()
            for k, v in extra.items():
                if hasattr(state, k):
                    setattr(state, k, v)
            repo.update(state.job_id, status=status, phase=phase)
            repo.append_history(task_id, phase, {"phase": phase, "status": status, **extra})
    except Exception as exc:
        LOGGER.warning("state update failed for %s: %s", task_id, exc)


def _build_cluster_params(
    *,
    region: str,
    cluster_name: str,
    sys_sku: str,
    sys_count: int,
    blast_sku: str,
    blast_count: int,
    caller_oid: str,
) -> Any:
    """Build the AKS managed cluster model used by the provision task."""
    from azure.mgmt.containerservice.models import (
        ManagedCluster,
        ManagedClusterAgentPoolProfile,
        ManagedClusterIdentity,
        ManagedClusterStorageProfile,
        ManagedClusterStorageProfileBlobCSIDriver,
    )

    # Mirror the sibling constants exactly so kubectl manifests that
    # reference the pool name/label/taint stay valid.
    SYSTEM_POOL_NAME = "systempool"
    BLAST_POOL_NAME = "blastpool"
    BLAST_LABEL_KEY = "workload"
    BLAST_LABEL_VALUE = "blast"
    BLAST_TAINT = f"{BLAST_LABEL_KEY}={BLAST_LABEL_VALUE}:NoSchedule"
    SYSTEM_TAINT = "CriticalAddonsOnly=true:NoSchedule"

    return ManagedCluster(
        location=region,
        identity=ManagedClusterIdentity(type="SystemAssigned"),
        dns_prefix=cluster_name,
        storage_profile=ManagedClusterStorageProfile(
            blob_csi_driver=ManagedClusterStorageProfileBlobCSIDriver(enabled=True)
        ),
        agent_pool_profiles=[
            ManagedClusterAgentPoolProfile(
                name=SYSTEM_POOL_NAME,
                count=sys_count,
                vm_size=sys_sku,
                os_type="Linux",
                mode="System",
                type="VirtualMachineScaleSets",
                enable_auto_scaling=False,
                node_taints=[SYSTEM_TAINT],
            ),
            ManagedClusterAgentPoolProfile(
                name=BLAST_POOL_NAME,
                count=blast_count,
                vm_size=blast_sku,
                os_type="Linux",
                mode="User",
                type="VirtualMachineScaleSets",
                enable_auto_scaling=False,
                node_labels={BLAST_LABEL_KEY: BLAST_LABEL_VALUE},
                node_taints=[BLAST_TAINT],
            ),
        ],
        tags={
            "app": "elastic-blast",
            "managedBy": "elb-dashboard",
            "owner": caller_oid or "unknown",
            "elb-system-pool": SYSTEM_POOL_NAME,
            "elb-blast-pool": BLAST_POOL_NAME,
        },
    )


@shared_task(
    name="api.tasks.azure.provision_aks",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def provision_aks(
    self: Any,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    region: str,
    cluster_name: str,
    node_sku: str,
    node_count: int,
    system_vm_size: str = "",
    system_node_count: int = 1,
    acr_resource_group: str = "",
    acr_name: str = "",
    storage_resource_group: str = "",
    storage_account: str = "",
    caller_oid: str = "",
) -> dict[str, Any]:
    """Provision an AKS cluster with the sibling repo's two-pool layout.

    Layout (mirrors ``elastic-blast-azure`` ``constants.py`` exactly):

    * ``systempool`` (mode=System) \u2014 small CriticalAddonsOnly pool that
      hosts CoreDNS / metrics-server / csi-azuredisk-node etc. Default
      VM size: ``Standard_D2s_v3``. Taint: ``CriticalAddonsOnly=true:NoSchedule``.
    * ``blastpool`` (mode=User)   \u2014 the workload pool. Carries the
      ``workload=blast`` label and matching ``workload=blast:NoSchedule``
      taint so only Pods with the explicit toleration land here.
    """
    from api.services.aks_skus import (
        DEFAULT_SKU,
        DEFAULT_SYSTEM_SKU,
        SKU_BY_NAME,
        is_allowed,
    )

    _update_state(job_id, "creating_cluster")

    # Validate / fall back to defaults defensively \u2014 the route layer
    # already enforces this but a stale enqueue payload should not poison
    # the cluster shape.
    sys_sku = system_vm_size or DEFAULT_SYSTEM_SKU
    blast_sku = node_sku or DEFAULT_SKU
    if not is_allowed(sys_sku):
        LOGGER.warning(
            "system_vm_size %r not in allow-list; falling back to %s", sys_sku, DEFAULT_SYSTEM_SKU
        )
        sys_sku = DEFAULT_SYSTEM_SKU
    if not is_allowed(blast_sku):
        LOGGER.warning("node_sku %r not in allow-list; falling back to %s", blast_sku, DEFAULT_SKU)
        blast_sku = DEFAULT_SKU
    sys_role = SKU_BY_NAME[sys_sku].role
    if sys_role not in ("system", "both"):
        LOGGER.warning(
            "system_vm_size %r is flagged role=%s (expected system/both); falling back to %s",
            sys_sku,
            sys_role,
            DEFAULT_SYSTEM_SKU,
        )
        sys_sku = DEFAULT_SYSTEM_SKU
    sys_count = max(1, min(int(system_node_count or 1), 3))
    blast_count = max(1, int(node_count or 1))

    cred = get_credential()
    aks = aks_client(cred, subscription_id)

    SYSTEM_POOL_NAME = "systempool"
    BLAST_POOL_NAME = "blastpool"

    cluster_params = _build_cluster_params(
        region=region,
        cluster_name=cluster_name,
        sys_sku=sys_sku,
        sys_count=sys_count,
        blast_sku=blast_sku,
        blast_count=blast_count,
        caller_oid=caller_oid,
    )

    _update_state(job_id, "arm_create_or_update")
    try:
        poller = aks.managed_clusters.begin_create_or_update(
            resource_group,
            cluster_name,
            cluster_params,
        )
        # Poll until done (this can take 5-10 minutes)
        result = poller.result()
        LOGGER.info(
            "AKS cluster %s provisioned: state=%s system=%s\u00d7%s blast=%s\u00d7%s",
            cluster_name,
            result.provisioning_state,
            sys_sku,
            sys_count,
            blast_sku,
            blast_count,
        )
    except Exception as exc:
        _update_state(job_id, "failed", status="failed", error_code=str(exc)[:500])
        raise

    _update_state(job_id, "ensuring_rbac")
    rbac_summary = _ensure_aks_runtime_rbac(
        cred,
        subscription_id,
        resource_group,
        cluster_name,
        acr_resource_group=acr_resource_group,
        acr_name=acr_name,
        storage_resource_group=storage_resource_group or resource_group,
        storage_account=storage_account,
    )
    if rbac_summary["roles_failed"]:
        _update_state(job_id, "rbac_ensure_failed_nonfatal", rbac=rbac_summary)

    _update_state(job_id, "completed", status="completed")
    return {
        "cluster_name": cluster_name,
        "provisioning_state": "Succeeded",
        "node_count": blast_count,
        "node_sku": blast_sku,
        "system_node_count": sys_count,
        "system_vm_size": sys_sku,
        "roles_assigned": rbac_summary["roles_assigned"],
        "roles_failed": rbac_summary["roles_failed"],
        "pools": [
            {"name": SYSTEM_POOL_NAME, "mode": "System", "vm_size": sys_sku, "count": sys_count},
            {"name": BLAST_POOL_NAME, "mode": "User", "vm_size": blast_sku, "count": blast_count},
        ],
    }


def _attach_acr(
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

    import uuid

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


def _grant_storage_blob_contributor_to_aks(
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

    import uuid

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


def _ensure_aks_runtime_rbac(
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
            _attach_acr(
                cred, subscription_id, resource_group, cluster_name, acr_resource_group, acr_name
            )
            roles_assigned.append("AcrPull")
        except Exception as exc:
            LOGGER.warning("AcrPull assignment failed (non-fatal): %s", exc)
            roles_failed["AcrPull"] = str(exc)[:300]

    if storage_account and storage_resource_group:
        try:
            _grant_storage_blob_contributor_to_aks(
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


@shared_task(
    name="api.tasks.azure.start_aks",
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def start_aks(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    auto_warmup: dict[str, Any] | None = None,
    auto_openapi: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a stopped AKS cluster.

    Side effects: starts the AKS control plane/node pools. When an Auto warm
    preference is supplied, persists it and queues storage warmup reconciliation
    after AKS start completes so the browser can be refreshed safely. When an
    OpenAPI deployment preference is supplied, queues the idempotent OpenAPI
    service deployment after the cluster is reachable.
    """
    cred = get_credential()
    aks = aks_client(cred, subscription_id)
    poller = aks.managed_clusters.begin_start(resource_group, cluster_name)
    poller.result()
    LOGGER.info("AKS cluster %s started", cluster_name)
    auto_warmup_task_id = ""
    if auto_warmup:
        try:
            from api.celery_app import celery_app
            from api.services.auto_warmup import (
                normalise_preference,
                save_auto_warmup_preference,
            )

            pref_payload = {
                **auto_warmup,
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
            }
            pref = save_auto_warmup_preference(normalise_preference(pref_payload))
            task = celery_app.send_task(
                "api.tasks.storage.reconcile_auto_warmup",
                kwargs={"preference": pref.to_dict(), "force": True},
                queue="storage",
            )
            auto_warmup_task_id = task.id
        except Exception as exc:
            LOGGER.warning("auto warm reconcile enqueue failed after AKS start: %s", exc)
    openapi_task_id = ""
    if auto_openapi:
        try:
            from api.celery_app import celery_app

            openapi_payload = {
                **auto_openapi,
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
            }
            task = celery_app.send_task(
                "api.tasks.openapi.deploy_openapi_service",
                kwargs=openapi_payload,
                queue="azure",
            )
            openapi_task_id = task.id
        except Exception as exc:
            LOGGER.warning("openapi deploy enqueue failed after AKS start: %s", exc)
    return {
        "cluster_name": cluster_name,
        "action": "start",
        "status": "completed",
        "auto_warmup_task_id": auto_warmup_task_id,
        "openapi_task_id": openapi_task_id,
    }


@shared_task(
    name="api.tasks.azure.stop_aks",
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def stop_aks(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Stop a running AKS cluster."""
    cred = get_credential()
    aks = aks_client(cred, subscription_id)
    poller = aks.managed_clusters.begin_stop(resource_group, cluster_name)
    poller.result()
    LOGGER.info("AKS cluster %s stopped", cluster_name)
    return {"cluster_name": cluster_name, "action": "stop", "status": "completed"}


@shared_task(
    name="api.tasks.azure.delete_aks",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def delete_aks(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Delete an AKS cluster."""
    cred = get_credential()
    aks = aks_client(cred, subscription_id)
    poller = aks.managed_clusters.begin_delete(resource_group, cluster_name)
    poller.result()
    LOGGER.info("AKS cluster %s deleted", cluster_name)
    return {"cluster_name": cluster_name, "action": "delete", "status": "completed"}


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
    cred = get_credential()
    summary = _ensure_aks_runtime_rbac(
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

"""Azure infrastructure Celery tasks — AKS provision / start / stop / delete.

Side effects: ARM calls to create/mutate AKS managed clusters and RBAC role
assignments. All tasks are idempotent.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from celery import shared_task

from api.services.azure_clients import (
    acr_client,
    aks_client,
    resource_client,
)
from api.services import get_credential

LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@shared_task(name="api.tasks.azure.diag_noop", bind=True, max_retries=0)
def diag_noop(self, *, message: str = "ping") -> dict[str, Any]:
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
            repo.update(state)
            repo.append_history(task_id, {"phase": phase, "status": status, **extra})
    except Exception as exc:
        LOGGER.warning("state update failed for %s: %s", task_id, exc)


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
    self,
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
        LOGGER.warning("system_vm_size %r not in allow-list; falling back to %s",
                       sys_sku, DEFAULT_SYSTEM_SKU)
        sys_sku = DEFAULT_SYSTEM_SKU
    if not is_allowed(blast_sku):
        LOGGER.warning("node_sku %r not in allow-list; falling back to %s",
                       blast_sku, DEFAULT_SKU)
        blast_sku = DEFAULT_SKU
    sys_role = SKU_BY_NAME[sys_sku].role
    if sys_role not in ("system", "both"):
        LOGGER.warning(
            "system_vm_size %r is flagged role=%s (expected system/both); "
            "falling back to %s", sys_sku, sys_role, DEFAULT_SYSTEM_SKU,
        )
        sys_sku = DEFAULT_SYSTEM_SKU
    sys_count = max(1, min(int(system_node_count or 1), 3))
    blast_count = max(1, int(node_count or 1))

    cred = get_credential()
    aks = aks_client(cred, subscription_id)

    # Build cluster parameters
    from azure.mgmt.containerservice.models import (
        ManagedCluster,
        ManagedClusterAgentPoolProfile,
        ManagedClusterIdentity,
    )

    # Mirror the sibling constants exactly so kubectl manifests that
    # reference the pool name/label/taint stay valid.
    SYSTEM_POOL_NAME = "systempool"
    BLAST_POOL_NAME = "blastpool"
    BLAST_LABEL_KEY = "workload"
    BLAST_LABEL_VALUE = "blast"
    BLAST_TAINT = f"{BLAST_LABEL_KEY}={BLAST_LABEL_VALUE}:NoSchedule"
    SYSTEM_TAINT = "CriticalAddonsOnly=true:NoSchedule"

    cluster_params = ManagedCluster(
        location=region,
        identity=ManagedClusterIdentity(type="SystemAssigned"),
        dns_prefix=cluster_name,
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

    _update_state(job_id, "arm_create_or_update")
    try:
        poller = aks.managed_clusters.begin_create_or_update(
            resource_group, cluster_name, cluster_params,
        )
        # Poll until done (this can take 5-10 minutes)
        result = poller.result()
        LOGGER.info(
            "AKS cluster %s provisioned: state=%s system=%s\u00d7%s blast=%s\u00d7%s",
            cluster_name, result.provisioning_state,
            sys_sku, sys_count, blast_sku, blast_count,
        )
    except Exception as exc:
        _update_state(job_id, "failed", status="failed", error_code=str(exc)[:500])
        raise

    # Attach ACR if specified
    if acr_name and acr_resource_group:
        _update_state(job_id, "attaching_acr")
        try:
            _attach_acr(cred, subscription_id, resource_group, cluster_name, acr_resource_group, acr_name)
        except Exception as exc:
            LOGGER.warning("ACR attach failed (non-fatal): %s", exc)
            _update_state(job_id, "acr_attach_failed_nonfatal")

    _update_state(job_id, "completed", status="completed")
    return {
        "cluster_name": cluster_name,
        "provisioning_state": "Succeeded",
        "node_count": blast_count,
        "node_sku": blast_sku,
        "system_node_count": sys_count,
        "system_vm_size": sys_sku,
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
    import uuid
    role_assignment_id = str(uuid.uuid4())
    try:
        auth_cl.role_assignments.create(
            scope=acr_scope,
            role_assignment_name=role_assignment_id,
            parameters={
                "properties": {
                    "role_definition_id": f"{acr_scope}/providers/Microsoft.Authorization/roleDefinitions/{acr_pull_role}",
                    "principal_id": kubelet_oid,
                    "principal_type": "ServicePrincipal",
                }
            },
        )
        LOGGER.info("AcrPull role assigned to %s on %s", kubelet_oid, acr_name)
    except Exception as exc:
        if "RoleAssignmentExists" in str(exc):
            LOGGER.info("AcrPull role already assigned")
        else:
            raise


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
    self,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Start a stopped AKS cluster."""
    cred = get_credential()
    aks = aks_client(cred, subscription_id)
    poller = aks.managed_clusters.begin_start(resource_group, cluster_name)
    poller.result()
    LOGGER.info("AKS cluster %s started", cluster_name)
    return {"cluster_name": cluster_name, "action": "start", "status": "completed"}


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
    self,
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
    self,
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
    self,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    acr_resource_group: str = "",
    acr_name: str = "",
) -> dict[str, Any]:
    """Assign RBAC roles (AcrPull, Storage Blob Data Contributor) to AKS kubelet."""
    cred = get_credential()
    roles_assigned: list[str] = []

    if acr_name and acr_resource_group:
        try:
            _attach_acr(cred, subscription_id, resource_group, cluster_name, acr_resource_group, acr_name)
            roles_assigned.append("AcrPull")
        except Exception as exc:
            LOGGER.warning("AcrPull assignment failed: %s", exc)

    return {
        "cluster_name": cluster_name,
        "roles_assigned": roles_assigned,
        "status": "completed",
    }

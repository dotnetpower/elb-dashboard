"""`provision_aks` Celery task — create a two-pool AKS cluster.

Responsibility: Submit the `ManagedCluster` create_or_update, wait for completion, and
    follow up with the runtime RBAC ensure so the freshly-provisioned cluster can pull
    images from ACR and read/write workload Storage.
Edit boundaries: Orchestration only — model assembly lives in `cluster_params.py`,
    RBAC writes in `rbac.py`, state updates in `helpers.py`.
Key entry points: `provision_aks` (Celery task `api.tasks.azure.provision_aks`).
Risky contracts: Task name `api.tasks.azure.provision_aks` is referenced by routes.
    The defensive fallback when an unknown / wrong-role SKU arrives in the payload is
    intentional — a stale enqueue should not poison the cluster shape.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py
    api/tests/test_azure_tasks.py`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from celery import shared_task

import api.tasks.azure as _facade
from api.tasks.azure.cluster_params import build_cluster_params
from api.tasks.azure.helpers import update_state

LOGGER = logging.getLogger(__name__)

# ARM eventual-consistency: after resource_groups.create_or_update returns
# 200 OK, downstream control planes (notably AKS) occasionally still see
# ResourceGroupNotFound for a brief window. Poll resource_groups.get to
# confirm visibility before the long AKS create call (which would otherwise
# fail ~10 minutes in).
_RG_VISIBILITY_ATTEMPTS = 12
_RG_VISIBILITY_DELAY_SECONDS = 5.0


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

    update_state(job_id, "creating_cluster")

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

    cred = _facade.get_credential()
    aks = _facade.aks_client(cred, subscription_id)

    # Ensure the resource group exists before the long AKS provisioning call.
    # The SPA defaults the RG to `rg-<base-cluster-name>` (e.g.
    # `elb-cluster-01` → `rg-elb-cluster`); on a fresh subscription that RG
    # may not exist yet, which would otherwise fail with `ResourceGroupNotFound`
    # ~10 minutes into the create call. create_or_update is idempotent:
    # creates if missing, no-op tag refresh if it already exists.
    update_state(job_id, "ensuring_resource_group")
    rc = _facade.resource_client(cred, subscription_id)
    rc.resource_groups.create_or_update(resource_group, {"location": region})

    # ARM eventual-consistency guard: confirm the RG is visible before
    # handing off to AKS. Without this, AKS create occasionally still
    # returns ResourceGroupNotFound for the freshly-created RG.
    for attempt in range(_RG_VISIBILITY_ATTEMPTS):
        try:
            rc.resource_groups.get(resource_group)
            break
        except ResourceNotFoundError:
            if attempt == _RG_VISIBILITY_ATTEMPTS - 1:
                raise
            LOGGER.info(
                "resource group %s not yet visible (attempt %d/%d); waiting %.0fs",
                resource_group,
                attempt + 1,
                _RG_VISIBILITY_ATTEMPTS,
                _RG_VISIBILITY_DELAY_SECONDS,
            )
            time.sleep(_RG_VISIBILITY_DELAY_SECONDS)

    SYSTEM_POOL_NAME = "systempool"
    BLAST_POOL_NAME = "blastpool"

    cluster_params = build_cluster_params(
        region=region,
        cluster_name=cluster_name,
        sys_sku=sys_sku,
        sys_count=sys_count,
        blast_sku=blast_sku,
        blast_count=blast_count,
        caller_oid=caller_oid,
    )

    update_state(job_id, "arm_create_or_update")
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
        update_state(job_id, "failed", status="failed", error_code=str(exc)[:500])
        raise

    update_state(job_id, "ensuring_rbac")
    rbac_summary = _facade._ensure_aks_runtime_rbac(
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
        update_state(job_id, "rbac_ensure_failed_nonfatal", rbac=rbac_summary)

    update_state(job_id, "completed", status="completed")
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

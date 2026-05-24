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
from api.tasks.azure.helpers import publish_progress

LOGGER = logging.getLogger(__name__)

# ARM eventual-consistency: after resource_groups.create_or_update returns
# 200 OK, downstream control planes (notably AKS) occasionally still see
# ResourceGroupNotFound for a brief window. Poll resource_groups.get to
# confirm visibility before the long AKS create call (which would otherwise
# fail ~10 minutes in).
_RG_VISIBILITY_ATTEMPTS = 12
_RG_VISIBILITY_DELAY_SECONDS = 5.0

# Sub-progress polling cadence during the long `arm_create_or_update`
# phase. Every tick we refresh ManagedCluster.provisioning_state plus
# per-AgentPool.provisioning_state so the FE banner can show "systempool
# Succeeded · blastpool Creating" instead of a 5-10 minute blank wait.
_ARM_POLL_INTERVAL_SECONDS = 20.0

# Ordered list of (machine_phase, human_label, step_number). `total_steps`
# is `len(_PROVISION_STEPS)` and is shipped with every progress tick so
# the FE banner can render "Step 3 of 5". Keep the keys in sync with
# `web/src/components/cards/ClusterCard/ProvisioningBanner.tsx` PHASE_LABELS.
_PROVISION_STEPS: list[tuple[str, str]] = [
    ("creating_cluster", "Preparing ARM request"),
    ("ensuring_resource_group", "Ensuring resource group"),
    ("arm_create_or_update", "Creating AKS cluster (5-10 min)"),
    ("ensuring_rbac", "Granting role assignments"),
    ("completed", "Cluster ready"),
]
_TOTAL_STEPS = len(_PROVISION_STEPS)
_STEP_INDEX: dict[str, int] = {key: i + 1 for i, (key, _) in enumerate(_PROVISION_STEPS)}
_STEP_LABEL: dict[str, str] = {key: label for key, label in _PROVISION_STEPS}


def _publish(
    task: Any,
    job_id: str,
    phase: str,
    *,
    status: str = "running",
    message: str | None = None,
    **extra: Any,
) -> None:
    """Wrapper around `publish_progress` that auto-fills step/total_steps from
    `_PROVISION_STEPS` and uses the canonical human label as the default
    message. Use this instead of bare `update_state` so the FE banner gets
    a consistent step indicator on every tick."""
    step = _STEP_INDEX.get(phase)
    label = _STEP_LABEL.get(phase)
    publish_progress(
        task,
        job_id,
        phase,
        step=step,
        total_steps=_TOTAL_STEPS,
        status=status,
        message=message if message is not None else label,
        **extra,
    )


def _collect_pool_states(aks: Any, resource_group: str, cluster_name: str) -> list[dict[str, Any]]:
    """Best-effort snapshot of every AgentPool's provisioning state.

    Returns an empty list when the pools are not yet visible (the cluster
    PUT is still in `Creating`). Never raises — a transient AKS poll
    failure must not abort the provision loop."""
    try:
        pools = list(aks.agent_pools.list(resource_group, cluster_name))
    except ResourceNotFoundError:
        return []
    except Exception as exc:
        LOGGER.debug("agent pool poll failed: %s", type(exc).__name__)
        return []
    snapshot: list[dict[str, Any]] = []
    for p in pools:
        snapshot.append(
            {
                "name": getattr(p, "name", None),
                "state": getattr(p, "provisioning_state", None),
                "count": getattr(p, "count", None),
                "vm_size": getattr(p, "vm_size", None),
                "mode": getattr(p, "mode", None),
            }
        )
    return snapshot


def _poll_arm_create(
    task: Any,
    poller: Any,
    *,
    aks: Any,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> None:
    """Block until `poller.done()` returns True, publishing sub-progress every
    `_ARM_POLL_INTERVAL_SECONDS`. Pollers that don't expose `.done()` (older
    test fakes) skip the loop entirely so `poller.result()` is called once
    by the caller.

    The first tick that finds the cluster visible in ARM also publishes a
    `portal_url` deep-link so the banner can offer "Open in Azure portal"
    while the create is still in flight."""
    from api.services.aks_availability import azure_portal_aks_url

    if not hasattr(poller, "done"):
        return
    arm_started = time.monotonic()
    portal_url: str | None = None
    while True:
        try:
            if poller.done():
                return
        except Exception as exc:
            LOGGER.debug("poller.done() failed: %s", type(exc).__name__)
            return
        cluster_state: str | None = None
        cluster_visible = False
        try:
            cluster = aks.managed_clusters.get(resource_group, cluster_name)
            cluster_state = getattr(cluster, "provisioning_state", None)
            cluster_visible = True
        except ResourceNotFoundError:
            cluster_state = "Pending"
        except Exception as exc:
            LOGGER.debug("managed_clusters.get failed: %s", type(exc).__name__)
        if cluster_visible and not portal_url:
            portal_url = azure_portal_aks_url(subscription_id, resource_group, cluster_name)
        pools = _collect_pool_states(aks, resource_group, cluster_name)
        elapsed = int(time.monotonic() - arm_started)
        extra: dict[str, Any] = {
            "cluster_state": cluster_state,
            "pools": pools,
            "arm_elapsed_seconds": elapsed,
        }
        if portal_url:
            extra["portal_url"] = portal_url
        _publish(
            task,
            job_id,
            "arm_create_or_update",
            message=f"AKS state: {cluster_state or 'Pending'}",
            **extra,
        )
        time.sleep(_ARM_POLL_INTERVAL_SECONDS)


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

    _publish(self, job_id, "creating_cluster")

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
    _publish(self, job_id, "ensuring_resource_group", resource_group=resource_group)
    rc = _facade.resource_client(cred, subscription_id)
    try:
        rc.resource_groups.get(resource_group)
    except ResourceNotFoundError:
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
            _publish(
                self,
                job_id,
                "ensuring_resource_group",
                message=f"Waiting for resource group {resource_group} to become visible "
                f"({attempt + 1}/{_RG_VISIBILITY_ATTEMPTS})",
                resource_group=resource_group,
                rg_visibility_attempt=attempt + 1,
                rg_visibility_total=_RG_VISIBILITY_ATTEMPTS,
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

    portal_url = None
    try:
        from api.services.aks_availability import azure_portal_aks_url

        portal_url = azure_portal_aks_url(subscription_id, resource_group, cluster_name)
    except Exception as exc:
        LOGGER.debug("azure_portal_aks_url failed: %s", type(exc).__name__)

    _publish(
        self,
        job_id,
        "arm_create_or_update",
        message="Submitting cluster create to Azure",
        cluster_name=cluster_name,
        portal_url=portal_url,
        pools=[
            {
                "name": SYSTEM_POOL_NAME,
                "state": "Pending",
                "count": sys_count,
                "vm_size": sys_sku,
                "mode": "System",
            },
            {
                "name": BLAST_POOL_NAME,
                "state": "Pending",
                "count": blast_count,
                "vm_size": blast_sku,
                "mode": "User",
            },
        ],
    )
    try:
        poller = aks.managed_clusters.begin_create_or_update(
            resource_group,
            cluster_name,
            cluster_params,
        )
        # Drive sub-progress for the long (5-10 min) ARM call. Without
        # this loop the FE would sit on "Submitting cluster to Azure"
        # with no visible delta until completion.
        _poll_arm_create(
            self,
            poller,
            aks=aks,
            job_id=job_id,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
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
        _publish(
            self,
            job_id,
            "failed",
            status="failed",
            message=str(exc)[:500],
            error_code=str(exc)[:500],
        )
        raise

    _publish(self, job_id, "ensuring_rbac")
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
        _publish(
            self,
            job_id,
            "rbac_ensure_failed_nonfatal",
            message=f"RBAC partial: {len(rbac_summary['roles_failed'])} role(s) failed",
            rbac=rbac_summary,
        )

    _publish(
        self,
        job_id,
        "completed",
        status="completed",
        message="Cluster ready",
        portal_url=portal_url,
        roles_assigned=rbac_summary["roles_assigned"],
        roles_failed=rbac_summary["roles_failed"],
    )
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

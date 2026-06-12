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
import os
import time
from datetime import UTC, datetime
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from celery import shared_task

import api.tasks.azure as _facade
from api.services.feature_events import TERMINAL_STATUSES, record_feature_event
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
_AKS_OPERATION_CONFLICT_RETRY_SECONDS = 60
_STALE_QUEUED_SECONDS = int(os.environ.get("AKS_PROVISION_STALE_QUEUED_SECONDS", "900"))
_STALE_RUNNING_SECONDS = int(os.environ.get("AKS_PROVISION_STALE_RUNNING_SECONDS", "2700"))


def _resolve_aks_vnet_subnet_id() -> str:
    """Resolve the hub `snet-aks` subnet id for BYO-subnet AKS creation.

    Prefers the explicit `PLATFORM_AKS_SUBNET_ID` env var. Falls back to
    deriving it from `PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID` (same hub VNet,
    swap the trailing `/subnets/snet-private-endpoints` segment for
    `/subnets/snet-aks`) so the fix works on already-deployed revisions that
    only carry the private-endpoint subnet env var. Returns "" when neither
    is resolvable, in which case the cluster falls back to managed-VNet mode.
    """
    explicit = os.environ.get("PLATFORM_AKS_SUBNET_ID", "").strip()
    if explicit:
        return explicit
    pe_subnet = os.environ.get("PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID", "").strip()
    if pe_subnet and "/subnets/" in pe_subnet:
        vnet_id = pe_subnet.rsplit("/subnets/", 1)[0]
        return f"{vnet_id}/subnets/snet-aks"
    return ""

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

# Sub-phases published during the RBAC step. They share the parent
# `ensuring_rbac` step counter (step 4 of 5) so the banner can show the
# specific role currently being granted instead of one opaque "Granting
# role assignments" line. The phase strings are also surfaced in the
# FE banner's PHASE_LABELS map.
_RBAC_SUB_PHASES: dict[str, str] = {
    "ensuring_rbac_acr": "Granting AcrPull to AKS kubelet",
    "ensuring_rbac_storage": "Granting Storage Blob Data Contributor",
    "ensuring_dashboard_mi_rbac": "Self-granting dashboard MI on cluster RG",
    "ensuring_vnet_peering": "Peering dashboard VNet with AKS VNet",
}


def _publish(
    task: Any,
    job_id: str,
    phase: str,
    *,
    status: str = "running",
    message: str | None = None,
    step_override: int | None = None,
    **extra: Any,
) -> None:
    """Wrapper around `publish_progress` that auto-fills step/total_steps from
    `_PROVISION_STEPS` and uses the canonical human label as the default
    message. Use this instead of bare `update_state` so the FE banner gets
    a consistent step indicator on every tick.

    RBAC sub-phases (`ensuring_rbac_acr` / `ensuring_rbac_storage`) inherit
    the parent `ensuring_rbac` step number so they render under "Step 4/5"
    instead of an unknown step.

    `step_override` pins the step counter to an explicit value regardless of
    the phase's natural step. This is needed for the pre-create RBAC
    self-grant: it reuses the `_RBAC_SUB_PHASES` strings (which map to the
    post-create step 4) but runs BEFORE the ARM create (step 3), so without
    the override the banner would jump 2 → 4 → 3 → 4 → 5. Pinning the
    pre-create ticks to the RG step (2) keeps the counter monotonic.
    """
    step = _STEP_INDEX.get(phase)
    label = _STEP_LABEL.get(phase)
    if step is None and phase in _RBAC_SUB_PHASES:
        step = _STEP_INDEX["ensuring_rbac"]
        label = _RBAC_SUB_PHASES[phase]
    if step_override is not None:
        step = step_override
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
    if status in TERMINAL_STATUSES:
        record_feature_event(
            "cluster_provision",
            status=status,
            job_id=job_id,
            phase=phase,
            error_code=extra.get("error_code"),
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


def _is_in_progress_aks_operation(exc: BaseException) -> bool:
    if not isinstance(exc, ResourceExistsError):
        return False
    message = str(exc)
    return (
        "OperationNotAllowed" in message
        and "in progress" in message
        and "managed cluster operation" in message
    )


def _retry_in_progress_aks_operation(
    task: Any,
    *,
    job_id: str,
    cluster_name: str,
    exc: BaseException,
) -> None:
    _publish(
        task,
        job_id,
        "arm_create_or_update",
        status="running",
        message="Azure is still finishing a previous AKS operation; retrying shortly.",
        cluster_name=cluster_name,
        retry_after_seconds=_AKS_OPERATION_CONFLICT_RETRY_SECONDS,
        error_code="aks_operation_in_progress",
        transient_error=str(exc)[:500],
    )
    raise task.retry(
        exc=exc,
        countdown=_AKS_OPERATION_CONFLICT_RETRY_SECONDS,
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
    tier: str = "",
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
        DEFAULT_SYSTEM_NODE_COUNT,
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
    sys_count = max(1, min(int(system_node_count or DEFAULT_SYSTEM_NODE_COUNT), 3))
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
        # Tag the freshly-created RG so `delete_aks` can later auto-clean
        # it when the cluster is removed. The tag is the gate that
        # prevents accidental deletion of a user-owned RG that just
        # happened to be empty at delete time.
        rc.resource_groups.create_or_update(
            resource_group,
            {
                "location": region,
                "tags": {
                    "managed-by": "elb-dashboard",
                    "purpose": "elb-aks-workload",
                },
            },
        )

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

    # Self-grant Contributor + UAA on the (possibly just-created) cluster
    # RG to the dashboard MI BEFORE the AKS create. The MI's sub-scope
    # `Elb Workload RG Creator` custom role grants RG-write + the
    # ABAC-whitelisted roleAssignments/write needed for this self-grant.
    # Without this pre-grant, picking a brand-new cluster name (e.g.
    # `elb-cluster-small` → `rg-elb-cluster-small`) fails preflight with
    # "Contributor missing at <new-RG>" and, if the user grants the
    # role manually and resubmits, would still 403 on
    # `Microsoft.ContainerService/managedClusters/write` because the
    # post-create self-grant (see end of this function) only runs after
    # AKS create succeeds. Idempotent — uses stable assignment UUIDs, so
    # the post-create call below becomes a no-op on success.
    def _pre_create_rbac_progress(sub_phase: str, msg: str) -> None:
        # This self-grant runs BEFORE the ARM create (step 3), inside the
        # RG-preparation window. The sub-phase strings map to the
        # post-create RBAC step (4) via `_RBAC_SUB_PHASES`, so pin these
        # ticks to the RG step (2) to keep the banner step counter
        # monotonic (1 → 2 → 3 → 4 → 5) instead of jumping 2 → 4 → 3.
        _publish(
            self,
            job_id,
            sub_phase,
            message=msg,
            step_override=_STEP_INDEX["ensuring_resource_group"],
        )

    pre_create_mi_summary = _facade._ensure_dashboard_mi_cluster_rg_roles(
        cred,
        subscription_id=subscription_id,
        cluster_resource_group=resource_group,
        mi_principal_id=os.environ.get("SHARED_IDENTITY_PRINCIPAL_ID", "").strip(),
        progress_callback=_pre_create_rbac_progress,
    )
    if pre_create_mi_summary.get("roles_failed"):
        LOGGER.warning(
            "pre-create dashboard-MI self-grant on %s incomplete: %s. "
            "AKS managedClusters/write may fail; falling through to ARM.",
            resource_group,
            pre_create_mi_summary["roles_failed"],
        )

    SYSTEM_POOL_NAME = "systempool"
    BLAST_POOL_NAME = "blastpool"

    aks_vnet_subnet_id = _resolve_aks_vnet_subnet_id()
    if aks_vnet_subnet_id:
        LOGGER.info(
            "AKS %s will be created in BYO-subnet mode (nodes in hub snet-aks) "
            "so workload Storage private endpoints resolve and route intra-VNet.",
            cluster_name,
        )
    else:
        LOGGER.warning(
            "AKS %s will be created in managed-VNet mode: neither "
            "PLATFORM_AKS_SUBNET_ID nor PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID is set. "
            "Workload Storage (publicNetworkAccess=Disabled) will be unreachable "
            "from cluster pods (warmup azcopy will 403).",
            cluster_name,
        )

    # Resolve the per-cluster warm-cache persistence mode. A missing
    # preference row reads back as `ephemeral`, which keeps the historical
    # cluster payload byte-identical (no disk overrides).
    from api.services.performance_pref import (
        DEFAULT_WARM_CACHE_MODE,
        resolve_warm_cache_mode,
    )

    warm_cache_mode = resolve_warm_cache_mode(subscription_id, resource_group, cluster_name)
    if warm_cache_mode != DEFAULT_WARM_CACHE_MODE:
        # Surface the non-default choice in App Insights so an operator can
        # confirm a cluster was provisioned with a persistent warm-cache disk.
        # The default `ephemeral` path stays silent (no behaviour change).
        LOGGER.info(
            "AKS %s provisioning with warm_cache_mode=%s (per-cluster Performance preference).",
            cluster_name,
            warm_cache_mode,
        )

    cluster_params = build_cluster_params(
        region=region,
        cluster_name=cluster_name,
        sys_sku=sys_sku,
        sys_count=sys_count,
        blast_sku=blast_sku,
        blast_count=blast_count,
        caller_oid=caller_oid,
        tier=tier,
        vnet_subnet_id=aks_vnet_subnet_id,
        warm_cache_mode=warm_cache_mode,
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
        if _is_in_progress_aks_operation(exc):
            _retry_in_progress_aks_operation(
                self,
                job_id=job_id,
                cluster_name=cluster_name,
                exc=exc,
            )
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

    def _rbac_progress(sub_phase: str, msg: str) -> None:
        # Inner helper closes over `self` + `job_id` so the rbac module
        # stays decoupled from Celery state internals. Sub-phase names
        # must be present in `_RBAC_SUB_PHASES` so `_publish` resolves
        # the parent step number.
        _publish(self, job_id, sub_phase, message=msg)

    rbac_summary = _facade._ensure_aks_runtime_rbac(
        cred,
        subscription_id,
        resource_group,
        cluster_name,
        acr_resource_group=acr_resource_group,
        acr_name=acr_name,
        # Do not fall back to the AKS cluster RG here. The workload Storage
        # account commonly lives in a different RG; the historical fallback
        # silently routed the role assignment at the cluster RG and ARM 404'd,
        # leaving the kubelet without Storage Blob Data Contributor. When the
        # caller omits the RG, `_resolve_workload_storage_defaults` inside
        # `ensure_aks_runtime_rbac` reads `AZURE_RESOURCE_GROUP` from env.
        storage_resource_group=storage_resource_group,
        storage_account=storage_account,
        progress_callback=_rbac_progress,
    )
    # Runtime RBAC failure is no longer "best effort": a cluster whose
    # kubelet identity cannot pull from ACR or read from Storage will
    # silently break BLAST submits with ImagePullBackOff or
    # AuthorizationPermissionMismatch. Surface it as a task failure now
    # so the SPA prompts the operator to fix RBAC (or re-run with the
    # right shared-MI grants) instead of "Cluster ready" hiding it.
    if rbac_summary["roles_failed"]:
        failed = rbac_summary["roles_failed"]
        roles_assigned = rbac_summary.get("roles_assigned") or []
        # Render `{role: error}` (dict) or `[role, ...]` (legacy list)
        # consistently so the FE error card is readable.
        if isinstance(failed, dict):
            failed_items = ", ".join(f"{r}: {e}" for r, e in failed.items())
        else:
            failed_items = ", ".join(str(r) for r in failed)
        rbac_error = (
            "AKS cluster created but runtime RBAC failed; the kubelet "
            "identity cannot access the requested resources. "
            f"Assigned: {roles_assigned or 'none'}. Failed: {failed_items}. "
            "Verify the dashboard managed identity has User Access "
            "Administrator on the ACR and Storage scopes, then re-run "
            "/api/aks/assign-roles or delete + recreate the cluster."
        )
        _publish(
            self,
            job_id,
            "failed",
            status="failed",
            message=rbac_error[:500],
            error_code="rbac_assignment_failed",
            rbac=rbac_summary,
            portal_url=portal_url,
        )
        raise RuntimeError(rbac_error)

    # Self-grant Contributor + User Access Administrator to the dashboard
    # MI on the AKS cluster RG so the operator can later click
    # "Deploy elb-openapi" without first running `grant-runtime-rbac.sh`
    # or re-provisioning `workloadClusterRoles.bicep`. Best-effort: if
    # the MI lacks `Microsoft.Authorization/roleAssignments/write` at
    # this scope (pre-Part-C deployments), the helper records the
    # failure into ``roles_failed`` and returns a `recovery_command`
    # string the SPA can surface — but the cluster itself is fully
    # usable, so we do NOT fail the task here.
    mi_summary = _facade._ensure_dashboard_mi_cluster_rg_roles(
        cred,
        subscription_id=subscription_id,
        cluster_resource_group=resource_group,
        mi_principal_id=os.environ.get("SHARED_IDENTITY_PRINCIPAL_ID", "").strip(),
        progress_callback=_rbac_progress,
    )
    if mi_summary.get("roles_failed"):
        failed = mi_summary["roles_failed"]
        if isinstance(failed, dict):
            failed_items = ", ".join(f"{r}: {e}" for r, e in failed.items())
        else:
            failed_items = ", ".join(str(r) for r in failed)
        recovery = mi_summary.get("recovery_command") or ""
        LOGGER.warning(
            "AKS %s ready, but dashboard-MI self-grant on %s incomplete: %s. "
            "OpenAPI deploy will fail until an admin runs: %s",
            cluster_name,
            resource_group,
            failed_items,
            recovery,
        )

    # BYO-subnet clusters: grant the cluster control-plane identity Network
    # Contributor on the hub snet-aks subnet so the Azure cloud-provider can
    # provision the `elb-openapi` internal LoadBalancer frontend IP in that
    # subnet at runtime. Node attachment was already authorised at create
    # time by the dashboard MI (Network Contributor on the platform RG);
    # this grant covers the *runtime* LB reconcile, which runs as the
    # cluster identity. Best-effort: warmup azcopy (node outbound to the
    # Storage private endpoint) does not need it, so a failure here must not
    # fail the provision — it only degrades OpenAPI Try-It until an admin
    # re-grants. Skipped entirely in managed-VNet mode.
    if aks_vnet_subnet_id:
        cluster_principal = getattr(
            getattr(result, "identity", None), "principal_id", ""
        )
        if cluster_principal:
            try:
                _facade._grant_network_contributor_on_subnet(
                    cred,
                    subscription_id,
                    principal_id=cluster_principal,
                    subnet_id=aks_vnet_subnet_id,
                    label=f"{cluster_name} cluster identity",
                )
            except Exception as exc:
                LOGGER.warning(
                    "AKS %s ready, but Network Contributor grant on snet-aks for the "
                    "cluster identity failed: %s. OpenAPI internal LoadBalancer may "
                    "stay <pending>; warmup/BLAST are unaffected.",
                    cluster_name,
                    exc,
                )
        else:
            LOGGER.warning(
                "AKS %s BYO-subnet: cluster identity principal_id missing from create "
                "result; skipping subnet Network Contributor grant.",
                cluster_name,
            )

    # Peer the dashboard platform VNet with the AKS-auto-created VNet so
    # the api sidecar can reach the `elb-openapi` Service's internal LB
    # IP (10.224.0.0/12 by default). Without this, `/api/aks/openapi/{proxy,
    # spec}` time out at 30s with httpx ConnectError even though the pod
    # is healthy and endpoints exist. Best-effort like the dashboard-MI
    # self-grant — failure surfaces via `vnet_peering.error` +
    # `recovery_command` in the completion payload but does NOT fail the
    # task. The AKS cluster is fully usable for BLAST submits via the
    # terminal sidecar regardless.
    _publish(self, job_id, "ensuring_vnet_peering")
    peering_summary = _facade._ensure_vnet_peering_with_cluster(
        cred,
        subscription_id=subscription_id,
        cluster_resource_group=resource_group,
        cluster_name=cluster_name,
    )
    if peering_summary.get("error"):
        LOGGER.warning(
            "AKS %s ready, but VNet peering with dashboard incomplete: %s. "
            "OpenAPI Try-It / spec will be unreachable until an admin runs: %s",
            cluster_name,
            peering_summary["error"],
            peering_summary.get("recovery_command", ""),
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
        dashboard_mi_rbac=mi_summary,
        vnet_peering=peering_summary,
    )
    # Auto-deploy the `elb-openapi` Service so a freshly-provisioned
    # cluster comes up with the OpenAPI surface ready without a separate
    # "Deploy elb-openapi" click. Best-effort: a failed enqueue does NOT
    # roll back the provision result — the dashboard's OpenAPI panel
    # surfaces the missing Deployment and the operator can click Deploy
    # manually. Set `ELB_AUTO_OPENAPI_DEPLOY=false` on the api/worker
    # sidecar to opt out.
    openapi_task_id = ""
    try:
        from api.tasks.openapi.auto_deploy import (
            enqueue_openapi_deploy_after_aks_event,
        )

        openapi_task_id = enqueue_openapi_deploy_after_aks_event(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            trigger="aks_provision",
        )
    except Exception as exc:
        LOGGER.warning(
            "auto OpenAPI deploy enqueue failed after AKS provision: %s", exc
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
        "dashboard_mi_rbac": mi_summary,
        "vnet_peering": peering_summary,
        "openapi_task_id": openapi_task_id,
        "pools": [
            {"name": SYSTEM_POOL_NAME, "mode": "System", "vm_size": sys_sku, "count": sys_count},
            {"name": BLAST_POOL_NAME, "mode": "User", "vm_size": blast_sku, "count": blast_count},
        ],
    }


def _parse_state_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _stale_limit_seconds(status: str) -> int:
    return _STALE_QUEUED_SECONDS if status in {"queued", "pending"} else _STALE_RUNNING_SECONDS


@shared_task(  # type: ignore[misc]
    name="api.tasks.azure.reconcile_stale_aks_provisions",
    bind=True,
    max_retries=0,
)
def reconcile_stale_aks_provisions(self: Any, *, limit: int = 200) -> dict[str, int]:
    """Fail AKS provision JobState rows that stopped making progress.

    This catches the class of failures where the route enqueued a Celery task
    but no worker consumed it, a worker child died before task-specific error
    handling ran, or Redis lost the task result while the dashboard still has a
    queued/running JobState row. Active ARM creates publish progress roughly
    every 20 seconds, so the default 45 minute running threshold is deliberately
    conservative.
    """
    del self
    from api.services.state_repo import JobStateRepository

    repo = JobStateRepository()
    now = datetime.now(UTC)
    result = {"scanned": 0, "failed": 0, "skipped": 0, "errors": 0}
    try:
        rows = repo.list_active(job_type="aks_provision", limit=limit)
    except Exception as exc:
        LOGGER.warning("aks stale provision reconcile list failed: %s", type(exc).__name__)
        result["errors"] += 1
        return result

    for row in rows:
        result["scanned"] += 1
        status = str(getattr(row, "status", "") or "").lower()
        updated_at = _parse_state_timestamp(
            getattr(row, "updated_at", None) or getattr(row, "created_at", None)
        )
        if updated_at is None:
            result["skipped"] += 1
            continue
        age_seconds = int((now - updated_at).total_seconds())
        limit_seconds = _stale_limit_seconds(status)
        if age_seconds < limit_seconds:
            result["skipped"] += 1
            continue

        phase = (
            "aks_provision_queue_stalled"
            if status in {"queued", "pending"}
            else "aks_provision_stalled"
        )
        message = (
            f"AKS provisioning made no progress for {age_seconds}s "
            f"(threshold {limit_seconds}s)."
        )
        payload = dict(getattr(row, "payload", {}) or {})
        payload["terminal_task_event"] = {
            "task_id": getattr(row, "task_id", "") or "",
            "task_name": "api.tasks.azure.provision_aks",
            "phase": phase,
            "status": "failed",
            "message": message,
            "error_code": phase,
            "recorded_at": now.isoformat(timespec="seconds"),
        }
        try:
            repo.update(
                row.job_id,
                status="failed",
                phase=phase,
                error_code=phase,
                payload=payload,
            )
            repo.append_history(
                row.job_id,
                phase,
                {
                    "status": "failed",
                    "task_id": getattr(row, "task_id", "") or "",
                    "message": message,
                    "age_seconds": age_seconds,
                    "threshold_seconds": limit_seconds,
                },
            )
            LOGGER.error(
                "aks_provision_stale job_id=%s task_id=%s status=%s age=%ss phase=%s",
                row.job_id,
                getattr(row, "task_id", "") or "-",
                status,
                age_seconds,
                phase,
            )
            result["failed"] += 1
        except Exception as exc:
            LOGGER.warning(
                "aks stale provision mark failed job_id=%s err=%s",
                getattr(row, "job_id", ""),
                type(exc).__name__,
            )
            result["errors"] += 1
    return result

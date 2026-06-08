"""AKS lifecycle Celery tasks (`start_aks` / `stop_aks` / `delete_aks`).

Responsibility: Drive the AKS managed-cluster lifecycle ARM operations and, on start,
    enqueue any follow-on side effects (Auto warm reconcile, OpenAPI deploy) that the
    SPA asked for when the user pressed Start.
Edit boundaries: Lifecycle calls and follow-on enqueues only. Provision-time concerns
    (pool layout, runtime RBAC) live in `provision.py` / `rbac.py`.
Key entry points: `start_aks`, `scale_aks`, `stop_aks`, `delete_aks` (Celery tasks
    `api.tasks.azure.{start,scale,stop,delete}_aks`).
Risky contracts: Task names referenced by routes and tests (`test_warmup_route`
    monkeypatches `api.tasks.azure.start_aks.delay` and
    `api.tasks.azure.assign_aks_roles.delay`). Follow-on enqueues must remain
    non-fatal — a failed reconcile/deploy enqueue must not roll back AKS start.
Validation: `uv run pytest -q api/tests/test_azure_tasks.py
    api/tests/test_warmup_route.py`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from azure.core.exceptions import HttpResponseError
from celery import shared_task

import api.tasks.azure as _facade

LOGGER = logging.getLogger(__name__)


# ARM rejects a `begin_start` on a cluster that is already Running/Starting and
# a `begin_stop` on one that is already Stopped/Stopping. The rejection codes /
# messages below identify exactly that "already in (or transitioning to) the
# requested power state" condition.
_ALREADY_IN_TARGET_STATE_MARKERS = (
    "operationnotallowed",
    "not in a stopped state",
    "not in a running state",
    "is already running",
    "is already stopped",
    "already in the desired",
    "already being started",
    "already being stopped",
)


def _is_already_in_target_power_state(exc: BaseException) -> bool:
    """True when ARM rejected a start/stop because the cluster is already in
    (or transitioning to) the requested power state.

    Why this matters: ``start_aks`` / ``stop_aks`` carry
    ``autoretry_for=(Exception,)`` but the AKS power LRO is **not** idempotent
    — ARM rejects ``begin_start`` on a Running/Starting cluster (and
    ``begin_stop`` on a Stopped/Stopping one) with ``OperationNotAllowed`` /
    ``BadRequest`` ("… is not in a stopped state"). Without recognising that
    condition, a transient blip during the multi-minute ``poller.result()``
    poll — or a duplicate Start/Stop click, or a manual Stop racing the idle
    auto-stop — re-issues the op on a now-transitioning cluster, turning an
    *effective success* into a hard task ERROR plus up to 3 wasted retries.
    Treating the marker as an idempotent no-op success makes the task
    converge instead. Genuinely transient errors (network, 429/5xx before the
    op is accepted) still raise and still autoretry.
    """
    if not isinstance(exc, HttpResponseError):
        return False
    code = ""
    err = getattr(exc, "error", None)
    if err is not None:
        code = f"{getattr(err, 'code', '') or ''}".lower()
    message = f"{getattr(exc, 'message', '') or ''}".lower()
    blob = f"{code} {message}"
    return any(marker in blob for marker in _ALREADY_IN_TARGET_STATE_MARKERS)


def _record_lifecycle_timing(
    phase: str,
    duration_seconds: float,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> None:
    """Persist an observed lifecycle duration. Best-effort; never raises.

    Feeds the `/api/monitor/aks/start-stats` estimate so the SPA start panel
    can show a measured "Last observed …" value instead of a constant.
    """
    try:
        from api.services.cluster_timings import record_timing

        record_timing(
            phase,
            duration_seconds,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    except Exception as exc:  # metrics must not fail the task
        LOGGER.warning("cluster timing record failed (%s): %s", phase, exc)


def _enqueue_forced_rewarm(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    auto_warmup: dict[str, Any] | None,
    num_nodes_override: int | None = None,
    log_context: str = "AKS lifecycle",
) -> str:
    """Persist the Auto warm preference with ``force_rewarm_pending`` and enqueue
    a forced ``reconcile_auto_warmup``. Returns the reconcile task id, or ``""``
    when there is nothing to warm / the enqueue failed (never raises).

    Shared by ``start_aks`` (re-warm a freshly-started cluster) and ``scale_aks``
    (re-warm after a workload-pool node-count change). The one-shot reconcile
    enqueued here usually fires before the (re-)scaled blastpool nodes register
    Ready, so it is dropped at the readiness gate; ``force_rewarm_pending`` lets
    the recurring beat reconcile keep forcing the re-warm across ticks until the
    cluster is workload-ready (it clears the flag once the warmup is actually
    enqueued).

    ``num_nodes_override`` lets the scale path pin the expected warmup node count
    to the new pool size so the readiness gate waits for exactly the post-scale
    node set instead of a stale ``pref.num_nodes``.
    """
    if not auto_warmup:
        return ""
    try:
        from api.celery_app import celery_app
        from api.services.auto_warmup import (
            normalise_preference,
            save_auto_warmup_preference,
        )

        pref_payload: dict[str, Any] = {
            **auto_warmup,
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": cluster_name,
            "force_rewarm_pending": True,
        }
        if num_nodes_override is not None:
            pref_payload["num_nodes"] = num_nodes_override
        pref = save_auto_warmup_preference(normalise_preference(pref_payload))
        task = celery_app.send_task(
            "api.tasks.storage.reconcile_auto_warmup",
            kwargs={"preference": pref.to_dict(), "force": True},
            # Route to the dedicated `reconcile` queue (same as the beat
            # reconcile) so this post-lifecycle reconcile is not delayed behind
            # — or competing with — interactive BLAST submits backed up on the
            # `storage` queue.
            queue="reconcile",
        )
        return task.id
    except Exception as exc:
        LOGGER.warning(
            "forced re-warm reconcile enqueue failed (%s): %s",
            log_context,
            type(exc).__name__,
        )
        return ""


def _resolve_workload_pool_name(
    aks: Any, resource_group: str, cluster_name: str, pool_name: str = ""
) -> str:
    """Return the name of the workload AgentPool to scale.

    When ``pool_name`` is supplied it is used verbatim. Otherwise the workload
    pool is resolved the same way the SPA does (``selectWorkloadPool`` in
    ``web/src/pages/blastSubmit/computeEnvironment.ts``): prefer ``blastpool``,
    else the first User-mode pool. Returns ``""`` when no candidate exists so the
    caller can fail with a clear error instead of a confusing ARM 404.
    """
    if pool_name:
        return pool_name
    try:
        pools = list(aks.agent_pools.list(resource_group, cluster_name))
    except Exception as exc:
        LOGGER.warning(
            "workload pool resolve failed cluster=%s: %s",
            cluster_name,
            type(exc).__name__,
        )
        return ""
    blast = next(
        (p for p in pools if (getattr(p, "name", "") or "").lower() == "blastpool"),
        None,
    )
    if blast is not None:
        return str(getattr(blast, "name", "") or "")
    user = next(
        (p for p in pools if (getattr(p, "mode", "") or "").lower() == "user"),
        None,
    )
    return str(getattr(user, "name", "") or "") if user is not None else ""


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
    cred = _facade.get_credential()
    aks = _facade.aks_client(cred, subscription_id)
    _started_at = time.monotonic()
    # Reset the idle auto-stop clock as soon as the start begins so an
    # evaluator tick racing the ~5-min start LRO sees a fresh anchor and
    # grants a full ``idle_minutes`` grace instead of stopping the cluster
    # the user just asked to start. Non-fatal: a bookkeeping miss must
    # never block the start itself. No-op when the cluster has no
    # auto-stop preference row.
    try:
        from api.services.auto_stop import mark_auto_stop_started

        mark_auto_stop_started(subscription_id, resource_group, cluster_name)
    except Exception as exc:
        LOGGER.warning(
            "auto_stop last_started_at stamp failed before AKS start for %s: %s",
            cluster_name,
            exc,
        )
    poller = aks.managed_clusters.begin_start(resource_group, cluster_name)
    started_now = True
    try:
        poller.result()
        LOGGER.info("AKS cluster %s started", cluster_name)
    except Exception as exc:
        if not _is_already_in_target_power_state(exc):
            raise
        # Idempotent no-op: the cluster is already Running/Starting (duplicate
        # Start click, an autoretry after a transient poll error, or a manual
        # Start on a live cluster). Fall through to the follow-on enqueues so
        # the warmup/OpenAPI side effects the user asked for still run — they
        # are themselves idempotent.
        started_now = False
        LOGGER.info(
            "AKS cluster %s already running/starting; treating start as no-op",
            cluster_name,
        )
    if started_now:
        _record_lifecycle_timing(
            "aks_start",
            time.monotonic() - _started_at,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    auto_warmup_task_id = ""
    if auto_warmup:
        auto_warmup_task_id = _enqueue_forced_rewarm(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            auto_warmup=auto_warmup,
            log_context="aks_start",
        )
    openapi_task_id = ""
    try:
        from api.tasks.openapi.auto_deploy import (
            enqueue_openapi_deploy_after_aks_event,
        )

        openapi_task_id = enqueue_openapi_deploy_after_aks_event(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            overrides=auto_openapi if isinstance(auto_openapi, dict) else None,
            trigger="aks_start",
        )
    except Exception as exc:
        LOGGER.warning(
            "openapi deploy enqueue failed after AKS start: %s", type(exc).__name__
        )
    return {
        "cluster_name": cluster_name,
        "action": "start",
        "status": "completed",
        "noop": not started_now,
        "auto_warmup_task_id": auto_warmup_task_id,
        "openapi_task_id": openapi_task_id,
    }


@shared_task(
    name="api.tasks.azure.scale_aks",
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def scale_aks(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    node_count: int,
    pool_name: str = "",
    auto_warmup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scale the workload (blastpool) node pool to ``node_count`` nodes.

    Side effects: PUTs the resolved workload AgentPool with the new ``count`` via
    ``agent_pools.begin_create_or_update``. When an Auto warm preference is
    supplied, queues a forced warmup reconcile (same mechanism as ``start_aks``)
    so freshly-added nodes get their node-local BLAST DB cache and the warmup
    status reflects the new pool size. A no-op (target == current count) skips
    the ARM PUT but still ensures the re-warm runs when ``auto_warmup`` is given
    (see the retry note below).

    Idempotency / retry safety: the per-pool PUT is itself idempotent (ARM
    converges the count), so ``autoretry_for=(Exception,)`` is safe — re-issuing
    the PUT with the same target converges rather than diverges. But that retry
    creates a subtle gap: if ``poller.result()`` raises a *transient* error
    AFTER ARM already accepted the change, the autoretry re-runs from the top,
    now observes ``previous_count == target`` (ARM already converged), and would
    otherwise take the no-op branch and skip the re-warm entirely — silently
    leaving the freshly-added nodes cold with no backstop (no ``force_rewarm_
    pending`` preference was saved on a first-ever scale). To close that gap the
    no-op branch ALSO enqueues the forced re-warm whenever ``auto_warmup`` is
    supplied. That is safe: the re-warm is idempotent (the warmup task skips
    already-cached nodes) and ``reconcile_auto_warmup`` already runs concurrently
    with the beat reconcile in production. The SPA disables Apply when the count
    is unchanged, so the only callers that reach the no-op branch are races /
    retries — both of which want the re-warm.
    """
    target = int(node_count)
    cred = _facade.get_credential()
    aks = _facade.aks_client(cred, subscription_id)
    resolved_pool_name = _resolve_workload_pool_name(
        aks, resource_group, cluster_name, pool_name
    )
    if not resolved_pool_name:
        raise RuntimeError(
            f"no workload node pool found on cluster '{cluster_name}' to scale"
        )
    pool = aks.agent_pools.get(resource_group, cluster_name, resolved_pool_name)
    previous_count = int(getattr(pool, "count", 0) or 0)
    if previous_count == target:
        LOGGER.info(
            "scale_aks no-op cluster=%s pool=%s count=%d",
            cluster_name,
            resolved_pool_name,
            target,
        )
        # Still ensure the re-warm: a no-op here is reachable via an autoretry
        # that races ARM's own convergence after a successful PUT, where the
        # user's "re-warm on change" intent must not be silently dropped.
        auto_warmup_task_id = _enqueue_forced_rewarm(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            auto_warmup=auto_warmup,
            num_nodes_override=target,
            log_context="aks_scale_noop",
        )
        return {
            "cluster_name": cluster_name,
            "action": "scale",
            "status": "completed",
            "noop": True,
            "pool_name": resolved_pool_name,
            "previous_count": previous_count,
            "node_count": target,
            "auto_warmup_task_id": auto_warmup_task_id,
        }
    _scaled_at = time.monotonic()
    pool.count = target
    poller = aks.agent_pools.begin_create_or_update(
        resource_group, cluster_name, resolved_pool_name, pool
    )
    poller.result()
    LOGGER.info(
        "scale_aks cluster=%s pool=%s %d->%d",
        cluster_name,
        resolved_pool_name,
        previous_count,
        target,
    )
    _record_lifecycle_timing(
        "aks_scale",
        time.monotonic() - _scaled_at,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    # Re-warm after a node-count change: scale-up adds cold nodes that need the
    # node-local BLAST DB cache, and the warmup status/readiness gate must track
    # the new pool size. Idempotent — the warmup task skips already-cached nodes,
    # so a scale-down (remaining nodes already warm) is a cheap no-op.
    auto_warmup_task_id = _enqueue_forced_rewarm(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        auto_warmup=auto_warmup,
        num_nodes_override=target,
        log_context="aks_scale",
    )
    return {
        "cluster_name": cluster_name,
        "action": "scale",
        "status": "completed",
        "noop": False,
        "pool_name": resolved_pool_name,
        "previous_count": previous_count,
        "node_count": target,
        "auto_warmup_task_id": auto_warmup_task_id,
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
    cred = _facade.get_credential()
    aks = _facade.aks_client(cred, subscription_id)
    _started_at = time.monotonic()
    poller = aks.managed_clusters.begin_stop(resource_group, cluster_name)
    stopped_now = True
    try:
        poller.result()
        LOGGER.info("AKS cluster %s stopped", cluster_name)
    except Exception as exc:
        if not _is_already_in_target_power_state(exc):
            raise
        # Idempotent no-op: already Stopped/Stopping (duplicate Stop, an
        # autoretry after a transient poll error, or a manual Stop racing the
        # idle auto-stop which the evaluator's provisioning-state guard did
        # not catch). Converge to success instead of a hard ERROR + retries.
        stopped_now = False
        LOGGER.info(
            "AKS cluster %s already stopped/stopping; treating stop as no-op",
            cluster_name,
        )
    if stopped_now:
        _record_lifecycle_timing(
            "aks_stop",
            time.monotonic() - _started_at,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    return {
        "cluster_name": cluster_name,
        "action": "stop",
        "status": "completed",
        "noop": not stopped_now,
    }


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
    """Delete an AKS cluster, and the enclosing resource group if it becomes empty.

    Side effects: ARM `managed_clusters.begin_delete` (which also tears down the
    auto-managed `MC_*` node-infra RG). After the AKS LRO completes, the parent RG
    is inspected; if it (a) carries the `managed-by=elb-dashboard` tag we wrote
    at create time **and** (b) contains no remaining resources, it is deleted too
    so the dashboard does not accumulate empty `rg-elb-cluster`-style shells.
    A non-empty RG or an RG without our ownership tag is left untouched — that
    closes both the user-shared-RG case and the TOCTOU race where the RG was
    listed empty here but another caller created a resource between
    `list_by_resource_group` and `begin_delete`. RG cleanup failures are logged
    but do not fail the task — the AKS delete already succeeded.
    """
    cred = _facade.get_credential()
    aks = _facade.aks_client(cred, subscription_id)
    poller = aks.managed_clusters.begin_delete(resource_group, cluster_name)
    poller.result()
    LOGGER.info("AKS cluster %s deleted", cluster_name)

    rg_status = "retained"
    rg_remaining = -1
    try:
        rc = _facade.resource_client(cred, subscription_id)
        # Ownership gate first — never auto-delete an RG we did not
        # create. `provision_aks` tags newly-created RGs with
        # `managed-by: elb-dashboard`. RGs that pre-date this tagging
        # change (or that the user created by hand) are intentionally
        # left for the operator to clean up manually.
        rg_props = rc.resource_groups.get(resource_group)
        rg_tags = dict(getattr(rg_props, "tags", None) or {})
        owns_rg = (
            rg_tags.get("managed-by") == "elb-dashboard"
            or rg_tags.get("managedBy") == "elb-dashboard"
        )
        if not owns_rg:
            rg_status = "retained_not_owned"
            LOGGER.info(
                "Resource group %s retained (no managed-by=elb-dashboard tag; "
                "treating as user-owned)",
                resource_group,
            )
        else:
            remaining = [
                r.name for r in rc.resources.list_by_resource_group(resource_group)
            ]
            rg_remaining = len(remaining)
            if rg_remaining == 0:
                rc.resource_groups.begin_delete(resource_group).result()
                rg_status = "deleted"
                LOGGER.info(
                    "Resource group %s deleted (owned + empty after AKS removal)",
                    resource_group,
                )
            else:
                rg_status = "retained_not_empty"
                LOGGER.info(
                    "Resource group %s retained (%d resource(s) remain: %s)",
                    resource_group,
                    rg_remaining,
                    ", ".join(remaining[:5]) + (" ..." if rg_remaining > 5 else ""),
                )
    except Exception as exc:
        rg_status = "error"
        LOGGER.warning("RG cleanup check failed for %s: %s", resource_group, exc)

    return {
        "cluster_name": cluster_name,
        "action": "delete",
        "status": "completed",
        "resource_group": resource_group,
        "resource_group_status": rg_status,
        "resource_group_remaining": rg_remaining,
    }

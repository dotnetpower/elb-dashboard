"""AKS lifecycle routes.

Responsibility: AKS lifecycle routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `aks_start`, `aks_scale`, `aks_stop`, `aks_delete`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate. Lifecycle admission must be persisted before task enqueue; an enqueue failure cancels
only the token created by that request.
Validation: `uv run pytest -q api/tests/test_warmup_route.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay
from api.routes.aks.common import _invalidate_aks_monitor_cache
from api.services.feature_events import record_feature_event

router = APIRouter()

# Upper bound for an interactive workload-pool scale. The slider/input in the
# SPA caps lower than this; the hard ceiling here is a safety rail so a crafted
# request cannot ask ARM for an absurd node count (ARM/quota still enforces the
# real per-subscription limit and returns a structured error if exceeded).
_MAX_SCALE_NODE_COUNT = int(os.environ.get("AKS_MAX_SCALE_NODE_COUNT", "100"))


def _lifecycle_scope(body: dict[str, Any]) -> tuple[str, str, str]:
    subscription_id = str(body.get("subscription_id") or "").strip()
    resource_group = str(body.get("resource_group") or "").strip()
    cluster_name = str(body.get("cluster_name") or "").strip()
    if not all((subscription_id, resource_group, cluster_name)):
        raise HTTPException(
            422,
            detail={
                "code": "invalid_cluster_scope",
                "message": "subscription_id, resource_group, and cluster_name are required",
            },
        )
    return subscription_id, resource_group, cluster_name


def _create_barrier(
    *,
    action: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    target_node_count: int = 0,
    auto_warmup: dict[str, Any] | None = None,
) -> str:
    from api.services.aks.execution_admission import (
        ExecutionAdmissionPersistenceError,
        create_lifecycle_barrier,
    )

    databases = auto_warmup.get("databases", []) if auto_warmup else []
    try:
        barrier = create_lifecycle_barrier(
            action=action,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            target_node_count=target_node_count,
            databases=databases if isinstance(databases, list) else [],
        )
    except ExecutionAdmissionPersistenceError as exc:
        raise HTTPException(
            503,
            detail={
                "code": "execution_admission_unavailable",
                "message": "Cluster lifecycle safety state could not be persisted.",
                "retryable": True,
                "retry_after_seconds": 30,
            },
        ) from exc
    return barrier.token


def _cancel_barrier(token: str, *, reason: str) -> None:
    from api.services.aks.execution_admission import cancel_lifecycle_barrier

    try:
        cancel_lifecycle_barrier(token, reason=reason)
    except Exception as exc:
        # The durable barrier remains in the safe direction. Its cancellation
        # can be retried by the next lifecycle request, which creates a new token.
        import logging

        logging.getLogger(__name__).warning(
            "lifecycle barrier cancellation failed token=%s error=%s",
            token[:12],
            type(exc).__name__,
        )



@router.post("/start")
def aks_start(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import start_aks

    subscription_id, resource_group, cluster_name = _lifecycle_scope(body)
    auto_warmup = body.get("auto_warmup") if isinstance(body.get("auto_warmup"), dict) else None
    auto_openapi = body.get("auto_openapi") if isinstance(body.get("auto_openapi"), dict) else None
    if auto_openapi is None:
        source = auto_warmup if isinstance(auto_warmup, dict) else body
        if source.get("acr_name"):
            auto_openapi = {
                "acr_name": source.get("acr_name", ""),
                "acr_resource_group": source.get("acr_resource_group", ""),
                "storage_account": source.get("storage_account", ""),
                "storage_resource_group": source.get("storage_resource_group", ""),
            }
    if isinstance(auto_openapi, dict):
        auto_openapi = {
            "acr_name": auto_openapi.get("acr_name", ""),
            "acr_resource_group": auto_openapi.get("acr_resource_group", ""),
            "storage_account": auto_openapi.get("storage_account", ""),
            "storage_resource_group": auto_openapi.get("storage_resource_group", ""),
            "tenant_id": auto_openapi.get("tenant_id") or caller.tenant_id,
            "caller_oid": auto_openapi.get("caller_oid") or caller.object_id,
        }
        if not auto_openapi["acr_name"]:
            auto_openapi = None
    try:
        target_node_count = int((auto_warmup or {}).get("num_nodes") or 0)
    except (TypeError, ValueError):
        target_node_count = 0
    barrier_token = _create_barrier(
        action="start",
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        target_node_count=target_node_count,
        auto_warmup=auto_warmup,
    )
    try:
        result = _safe_delay(
            start_aks,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            auto_warmup=auto_warmup,
            auto_openapi=auto_openapi,
            execution_admission_token=barrier_token,
        )
    except Exception:
        _cancel_barrier(barrier_token, reason="start_enqueue_failed")
        raise
    _invalidate_aks_monitor_cache(subscription_id, resource_group)
    record_feature_event(
        "cluster_lifecycle",
        status="requested",
        action="start",
        actor="user",
        actor_oid=caller.object_id,
        cluster=body.get("cluster_name", ""),
        resource_group=body.get("resource_group", ""),
        task_id=getattr(result, "id", None),
    )
    return {"task_id": result.id, "status": "queued"}


@router.post("/scale")
def aks_scale(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Scale the workload (blastpool) node pool to ``node_count`` nodes.

    Validates the requested count and enqueues ``scale_aks``, which PUTs the
    pool and — when an ``auto_warmup`` preference is supplied — chains a forced
    warmup reconcile so re-scaled nodes get their node-local BLAST DB cache.
    The optional ``auto_warmup`` object mirrors the ``/aks/start`` shape
    (databases / programs / storage targets); omit it to scale without warming.
    """
    from api.tasks.azure import scale_aks

    subscription_id, resource_group, cluster_name = _lifecycle_scope(body)
    try:
        node_count = int(body.get("node_count"))
    except (TypeError, ValueError):
        raise HTTPException(
            422,
            detail={
                "code": "invalid_node_count",
                "message": "node_count must be an integer",
            },
        ) from None
    if node_count < 1 or node_count > _MAX_SCALE_NODE_COUNT:
        raise HTTPException(
            422,
            detail={
                "code": "invalid_node_count",
                "message": (
                    f"node_count must be between 1 and {_MAX_SCALE_NODE_COUNT}"
                ),
            },
        )
    auto_warmup = body.get("auto_warmup") if isinstance(body.get("auto_warmup"), dict) else None
    barrier_token = _create_barrier(
        action="scale",
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        target_node_count=node_count,
        auto_warmup=auto_warmup,
    )
    try:
        result = _safe_delay(
            scale_aks,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            node_count=node_count,
            pool_name=body.get("pool_name", "") or "",
            auto_warmup=auto_warmup,
            execution_admission_token=barrier_token,
        )
    except Exception:
        _cancel_barrier(barrier_token, reason="scale_enqueue_failed")
        raise
    _invalidate_aks_monitor_cache(subscription_id, resource_group)
    record_feature_event(
        "cluster_lifecycle",
        status="requested",
        action="scale",
        actor="user",
        actor_oid=caller.object_id,
        cluster=body.get("cluster_name", ""),
        resource_group=body.get("resource_group", ""),
        node_count=node_count,
        task_id=getattr(result, "id", None),
    )
    return {"task_id": result.id, "status": "queued"}


@router.post("/stop")
def aks_stop(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import stop_aks

    subscription_id, resource_group, cluster_name = _lifecycle_scope(body)
    barrier_token = _create_barrier(
        action="stop",
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    try:
        result = _safe_delay(
            stop_aks,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            execution_admission_token=barrier_token,
        )
    except Exception:
        _cancel_barrier(barrier_token, reason="stop_enqueue_failed")
        raise
    _invalidate_aks_monitor_cache(subscription_id, resource_group)
    record_feature_event(
        "cluster_lifecycle",
        status="requested",
        action="stop",
        actor="user",
        actor_oid=caller.object_id,
        cluster=body.get("cluster_name", ""),
        resource_group=body.get("resource_group", ""),
        task_id=getattr(result, "id", None),
    )
    return {"task_id": result.id, "status": "queued"}


@router.post("/delete")
def aks_delete(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import delete_aks

    subscription_id, resource_group, cluster_name = _lifecycle_scope(body)
    barrier_token = _create_barrier(
        action="delete",
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    try:
        result = _safe_delay(
            delete_aks,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            execution_admission_token=barrier_token,
        )
    except Exception:
        _cancel_barrier(barrier_token, reason="delete_enqueue_failed")
        raise
    _invalidate_aks_monitor_cache(subscription_id, resource_group)
    record_feature_event(
        "cluster_lifecycle",
        status="requested",
        action="delete",
        actor="user",
        actor_oid=caller.object_id,
        cluster=body.get("cluster_name", ""),
        resource_group=body.get("resource_group", ""),
        task_id=getattr(result, "id", None),
    )
    return {"task_id": result.id, "status": "queued"}

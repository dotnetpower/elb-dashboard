"""AKS lifecycle routes.

Responsibility: AKS lifecycle routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `aks_start`, `aks_scale`, `aks_stop`, `aks_delete`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py
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



@router.post("/start")
def aks_start(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import start_aks

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
    result = _safe_delay(
        start_aks,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
        auto_warmup=auto_warmup,
        auto_openapi=auto_openapi,
    )
    _invalidate_aks_monitor_cache(body.get("subscription_id", ""), body.get("resource_group", ""))
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
    result = _safe_delay(
        scale_aks,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
        node_count=node_count,
        pool_name=body.get("pool_name", "") or "",
        auto_warmup=auto_warmup,
    )
    _invalidate_aks_monitor_cache(body.get("subscription_id", ""), body.get("resource_group", ""))
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

    result = _safe_delay(
        stop_aks,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
    )
    _invalidate_aks_monitor_cache(body.get("subscription_id", ""), body.get("resource_group", ""))
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

    result = _safe_delay(
        delete_aks,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
    )
    _invalidate_aks_monitor_cache(body.get("subscription_id", ""), body.get("resource_group", ""))
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

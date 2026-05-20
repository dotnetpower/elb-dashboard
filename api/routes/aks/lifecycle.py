"""AKS lifecycle routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay
from api.routes.aks.common import _invalidate_aks_monitor_cache

router = APIRouter()


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
    return {"task_id": result.id, "status": "queued"}

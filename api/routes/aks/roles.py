"""AKS role assignment route."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Path

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay

router = APIRouter()


@router.post("/{cluster_name}/assign-roles")
def aks_assign_roles(
    cluster_name: str = Path(...),
    body: dict[str, Any] = Body(default={}),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import assign_aks_roles

    result = _safe_delay(
        assign_aks_roles,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=cluster_name,
        acr_resource_group=body.get("acr_resource_group", ""),
        acr_name=body.get("acr_name", ""),
        storage_resource_group=body.get("storage_resource_group", ""),
        storage_account=body.get("storage_account", ""),
    )
    return {"task_id": result.id, "status": "queued"}

"""AKS provisioning route."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay
from api.services.aks_skus import DEFAULT_SKU, DEFAULT_SYSTEM_SKU

router = APIRouter()


@router.post("/provision")
def aks_provision(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import provision_aks

    job_id = str(uuid.uuid4())
    result = _safe_delay(
        provision_aks,
        job_id=job_id,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        region=body.get("region", "koreacentral"),
        cluster_name=body.get("cluster_name", "elb-cluster"),
        node_sku=body.get("node_sku", DEFAULT_SKU),
        node_count=body.get("node_count", 3),
        # Sibling repo's two-pool layout: small system pool + workload pool.
        # Defaults mirror constants.py::ELB_DFLT_AZURE_SYSTEM_VM_SIZE.
        system_vm_size=body.get("system_vm_size", DEFAULT_SYSTEM_SKU),
        system_node_count=body.get("system_node_count", 1),
        acr_resource_group=body.get("acr_resource_group", ""),
        acr_name=body.get("acr_name", ""),
        storage_resource_group=body.get("storage_resource_group", ""),
        storage_account=body.get("storage_account", ""),
        caller_oid=caller.object_id,
    )
    return {
        "id": job_id,
        "job_id": job_id,
        "instance_id": result.id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }

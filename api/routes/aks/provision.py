"""AKS provisioning route.

Responsibility: AKS provisioning route
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `aks_provision`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay
from api.services.aks_skus import DEFAULT_SKU, DEFAULT_SYSTEM_NODE_COUNT, DEFAULT_SYSTEM_SKU

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.post("/provision")
def aks_provision(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import provision_aks

    job_id = str(uuid.uuid4())
    region = body.get("region", "koreacentral")
    cluster_name = str(body.get("cluster_name", "") or "").strip()
    resource_group = str(body.get("resource_group", "") or "").strip()

    # Never invent a cluster / resource-group name. A hardcoded fallback
    # (historically ``elb-cluster``) silently provisions or operates on the
    # wrong resource when the SPA omits the field, so require both explicitly
    # and fail fast at the HTTP boundary instead.
    if not (cluster_name and resource_group):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_parameters",
                "message": (
                    "cluster_name and resource_group are required to "
                    "provision an AKS cluster."
                ),
            },
        )

    # Create the JobState row up front so:
    #   * `helpers.update_state` calls from the task body have something
    #     to update (previously the row was missing and every state
    #     write silently no-op'd against the table),
    #   * `/api/aks/recent-failed-provisions` can list this row when the
    #     task ends in `failed`,
    #   * `/api/aks/cancel-provision/{task_id}` and `/api/tasks/{id}`
    #     can verify `owner_oid` before exposing or revoking the task.
    # Failure to write is logged + ignored so the route still enqueues
    # the work — the worker fallback to "Last attempt failed" via
    # localStorage handles the no-state-row degraded case.
    try:
        from api.services.state.job_state import JobState
        from api.services.state_repo import get_state_repo

        now = datetime.now(UTC).isoformat(timespec="seconds")
        repo = get_state_repo()
        repo.create(
            JobState(
                job_id=job_id,
                type="aks_provision",
                status="queued",
                phase="queued",
                owner_oid=caller.object_id,
                tenant_id=caller.tenant_id,
                subscription_id=body.get("subscription_id", ""),
                resource_group=resource_group,
                cluster_name=cluster_name,
                created_at=now,
                updated_at=now,
                payload={
                    "subscription_id": body.get("subscription_id", ""),
                    "resource_group": resource_group,
                    "region": region,
                    "cluster_name": cluster_name,
                    "node_sku": body.get("node_sku", DEFAULT_SKU),
                    "node_count": body.get("node_count", 3),
                    "system_vm_size": body.get("system_vm_size", DEFAULT_SYSTEM_SKU),
                    "system_node_count": body.get(
                        "system_node_count", DEFAULT_SYSTEM_NODE_COUNT
                    ),
                    "tier": str(body.get("tier", "") or ""),
                },
            )
        )
    except Exception as exc:
        LOGGER.warning("failed to create aks_provision job state: %s", exc)

    result = _safe_delay(
        provision_aks,
        job_id=job_id,
        subscription_id=body.get("subscription_id", ""),
        resource_group=resource_group,
        region=region,
        cluster_name=cluster_name,
        node_sku=body.get("node_sku", DEFAULT_SKU),
        node_count=body.get("node_count", 3),
        # Sibling repo's two-pool layout: small system pool + workload pool.
        # Defaults mirror constants.py::ELB_DFLT_AZURE_SYSTEM_VM_SIZE.
        system_vm_size=body.get("system_vm_size", DEFAULT_SYSTEM_SKU),
        system_node_count=body.get("system_node_count", DEFAULT_SYSTEM_NODE_COUNT),
        acr_resource_group=body.get("acr_resource_group", ""),
        acr_name=body.get("acr_name", ""),
        storage_resource_group=body.get("storage_resource_group", ""),
        storage_account=body.get("storage_account", ""),
        caller_oid=caller.object_id,
        # Free-form tier label written to ARM as `elb-tier=<value>`. The
        # SPA uses it to group multi-cluster deployments (heavy / light /
        # gpu). Empty / whitespace values are dropped inside
        # `build_cluster_params` so we never store `elb-tier=""`.
        tier=str(body.get("tier", "") or ""),
    )

    # Now that we have a task id from Celery, write it back so
    # ownership lookup by `task_id` (used by cancel + tasks routes)
    # resolves to this row. Failure is best-effort — the task itself
    # still runs.
    try:
        from api.services.state_repo import get_state_repo

        get_state_repo().update(job_id, task_id=result.id)
    except Exception as exc:
        LOGGER.warning("failed to stamp task_id on aks_provision row: %s", exc)

    return {
        "id": job_id,
        "job_id": job_id,
        "instance_id": result.id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }

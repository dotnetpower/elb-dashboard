"""/api/warmup/*`` - auto-preference + start/release/status endpoints.

Responsibility: /api/warmup/*`` - auto-preference + start/release/status endpoints
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_resolve_warmup_db_name`, `warmup_auto_preference_put`,
`warmup_auto_preference_get`, `warmup_start`, `warmup_release`, `warmup_status`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _WARMUP_RELEASE_BODY,
    _WARMUP_RELEASE_CALLER,
    _safe_send_task,
)

LOGGER = logging.getLogger(__name__)

warmup_router = APIRouter(prefix="/api/warmup", tags=["warmup"])


def _resolve_warmup_db_name(body: dict[str, Any]) -> str:
    """Pick the database name out of either the new SPA shape (`db` /
    `db_display_name`) or the legacy `database_name` shape. Returns the
    bare DB name (e.g. ``16S_ribosomal_RNA``) — strips any
    ``blast-db/`` container prefix the SPA sends with `db`.
    """
    raw = body.get("database_name") or body.get("db_display_name") or body.get("db") or ""
    if isinstance(raw, str) and "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    return str(raw or "").strip()


@warmup_router.put("/auto-preference")
def warmup_auto_preference_put(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.services.auto_warmup import normalise_preference, save_auto_warmup_preference

    try:
        pref = normalise_preference(
            {**body, "owner_oid": caller.object_id, "tenant_id": caller.tenant_id}
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    saved = save_auto_warmup_preference(pref)
    return {"status": "saved", "preference": saved.to_dict()}


@warmup_router.get("/auto-preference")
def warmup_auto_preference_get(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.services.auto_warmup import get_auto_warmup_preference

    pref = get_auto_warmup_preference(subscription_id, resource_group, cluster_name)
    if pref is None:
        return {"preference": None}
    return {"preference": pref.to_dict()}


@warmup_router.post("/start")
def warmup_start(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    database_name = _resolve_warmup_db_name(body)
    # Create job state
    try:
        from datetime import datetime

        from api.services.state_repo import JobState, get_state_repo

        now = datetime.now(UTC).isoformat(timespec="seconds")
        repo = get_state_repo()
        state = JobState(
            job_id=job_id,
            type="warmup",
            status="queued",
            phase="queued",
            owner_oid=caller.object_id,
            tenant_id=caller.tenant_id,
            created_at=now,
            updated_at=now,
            payload=body,
        )
        repo.create(state)
    except Exception as exc:
        LOGGER.warning("failed to create warmup job state: %s", exc)

    try:
        num_nodes = int(body.get("num_nodes") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "num_nodes must be an integer") from exc

    result = _safe_send_task(
        "api.tasks.storage.warmup_database",
        queue="storage",
        job_id=job_id,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        storage_account=body.get("storage_account", ""),
        database_name=database_name,
        cluster_name=body.get("aks_cluster_name") or body.get("cluster_name", ""),
        machine_type=body.get("machine_type", ""),
        num_nodes=num_nodes,
        acr_resource_group=body.get("acr_resource_group", ""),
        acr_name=body.get("acr_name", ""),
        program=body.get("program", "blastn"),
        caller_oid=caller.object_id,
    )
    try:
        from api.services.state_repo import get_state_repo

        get_state_repo().update(job_id, task_id=result.id)
    except Exception as exc:
        LOGGER.warning("failed to attach warmup task id: %s", exc)
    # The SPA's WarmupSection polls `/warmup/{instance_id}/status`, where
    # `instance_id` is the Celery task id. We expose all three aliases so
    # both the new SPA and any legacy callers keep working.
    return {
        "id": job_id,
        "instance_id": result.id,
        "task_id": result.id,
        "db": database_name,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }


@warmup_router.post("/release")
def warmup_release(
    body: dict[str, Any] = _WARMUP_RELEASE_BODY,
    caller: CallerIdentity = _WARMUP_RELEASE_CALLER,
) -> dict[str, Any]:
    database_name = _resolve_warmup_db_name(body)
    subscription_id = str(body.get("subscription_id") or "")
    resource_group = str(body.get("resource_group") or "")
    cluster_name = str(body.get("aks_cluster_name") or body.get("cluster_name") or "")
    if not database_name:
        raise HTTPException(400, "database_name is required")
    if not resource_group:
        raise HTTPException(400, "resource_group is required")
    if not cluster_name:
        raise HTTPException(400, "aks_cluster_name is required")

    from api.services import get_credential
    from api.services.k8s.monitoring import k8s_release_warmup_cache

    try:
        result = k8s_release_warmup_cache(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            database_name,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        LOGGER.warning("warmup release failed: %s", type(exc).__name__)
        raise HTTPException(
            503,
            detail={
                "code": "warmup_release_failed",
                "message": f"Could not release warm cache: {type(exc).__name__}",
            },
        ) from exc

    return {"db": database_name, **result}


@warmup_router.get("/{instance_id}/status")
def warmup_status(
    instance_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return warmup task status mapped to the SPA's orchestrator-style shape.

    The SPA's `WarmupSection` was originally written against a Durable
    Functions orchestrator (``runtime_status`` ∈ {Pending, Running,
    Completed, Failed, Terminated}) and a ``custom_status``/``output``
    payload. We translate the Celery ``AsyncResult`` to that shape so
    the SPA can be migrated incrementally.
    """
    from celery.result import AsyncResult

    from api.celery_app import celery_app

    result = AsyncResult(instance_id, app=celery_app)
    status = (result.status or "PENDING").upper()
    runtime_status = {
        "PENDING": "Pending",
        "RECEIVED": "Pending",
        "STARTED": "Running",
        "RETRY": "Running",
        "PROGRESS": "Running",
        "SUCCESS": "Completed",
        "FAILURE": "Failed",
        "REVOKED": "Terminated",
    }.get(status, "Running")

    custom_status: dict[str, Any] = {"phase": status.lower()}
    output: dict[str, Any] | None = None

    if not result.ready():
        info = result.info if isinstance(result.info, dict) else None
        if info:
            custom_status.update({k: v for k, v in info.items() if k != "exc_type"})
    elif result.successful():
        payload = result.result if isinstance(result.result, dict) else {}
        db_name = str(payload.get("database") or payload.get("db") or "")
        payload_status = str(payload.get("status", "")).lower()
        succeeded = payload_status in {"completed", "succeeded", "success"}
        custom_status.update({"phase": "completed", "db": db_name})
        output = {
            "status": "succeeded" if succeeded else "failed",
            "db": db_name,
        }
        if not succeeded and payload.get("error"):
            output["error"] = str(payload.get("error"))[:500]
    else:
        # FAILURE / REVOKED
        err = ""
        try:
            err = str(result.result or result.info or "")[:500]
        except Exception:
            err = "task failed"
        custom_status.update({"phase": "failed"})
        output = {"status": "failed", "db": "", "error": err}

    return {
        "instance_id": instance_id,
        "runtime_status": runtime_status,
        "custom_status": custom_status,
        "output": output,
    }


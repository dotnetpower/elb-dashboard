"""Stubs for endpoints that have not yet been ported from the legacy
Function App. They return well-structured empty/202 responses so the SPA
renders without crashing while the real implementations land.

Each stub logs a `STUB_CALLED` warning so we can see in App Insights which
endpoints the SPA actually exercises in production and prioritise the real
implementations accordingly.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from api_app.auth import CallerIdentity, require_caller

LOGGER = logging.getLogger(__name__)


def _stub_log(name: str, **ctx: Any) -> None:
    LOGGER.warning("STUB_CALLED endpoint=%s ctx=%s", name, ctx)


# ===========================================================================
# /api/resources/* moved to api_app/routes/resources.py (real implementation).
# Keeping this empty router so the import in main.py keeps working without a
# code change at swap time. The real router takes precedence because it is
# included after this one.
# ===========================================================================
resources_router = APIRouter(prefix="/api/resources", tags=["resources-stub"])


# ===========================================================================
# /api/aks/* — provision, openapi/deploy/spec, skus, lifecycle
# ===========================================================================
aks_router = APIRouter(prefix="/api/aks", tags=["aks"])


@aks_router.get("/skus")
def aks_skus(
    location: str = Query(default="koreacentral"),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("aks/skus", location=location)
    # Hard-code a small list of common SKUs so the SPA's dropdown renders.
    # Real implementation queries Microsoft.Compute/skus and filters to
    # B/D/E series.
    return {
        "skus": [
            {"name": "Standard_D2s_v5", "vCPUs": 2, "memoryGiB": 8, "category": "general"},
            {"name": "Standard_D4s_v5", "vCPUs": 4, "memoryGiB": 16, "category": "general"},
            {"name": "Standard_D8s_v5", "vCPUs": 8, "memoryGiB": 32, "category": "general"},
            {"name": "Standard_E4s_v5", "vCPUs": 4, "memoryGiB": 32, "category": "memory"},
            {"name": "Standard_E8s_v5", "vCPUs": 8, "memoryGiB": 64, "category": "memory"},
        ],
        "degraded": True,
        "degraded_reason": "static_skus_celery_task_pending",
    }


@aks_router.post("/provision")
def aks_provision(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("aks/provision", body=body, oid=caller.object_id)
    instance_id = secrets.token_urlsafe(16)
    return {
        "id": instance_id,
        "statusQueryGetUri": f"/api/aks/openapi/deploy/{instance_id}/status",
        "status": "stub",
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


@aks_router.post("/openapi/deploy")
def aks_openapi_deploy(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("aks/openapi/deploy", body=body, oid=caller.object_id)
    instance_id = secrets.token_urlsafe(16)
    return {
        "id": instance_id,
        "statusQueryGetUri": f"/api/aks/openapi/deploy/{instance_id}/status",
        "status": "stub",
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


@aks_router.get("/openapi/deploy/{instance_id}/status")
def aks_openapi_deploy_status(
    instance_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("aks/openapi/deploy/status", id=instance_id)
    return {
        "instance_id": instance_id,
        "runtime_status": "Pending",
        "custom_status": {"phase": "stub", "description": "Celery task pending"},
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


@aks_router.get("/openapi/spec")
def aks_openapi_spec(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("aks/openapi/spec", rg=resource_group, cluster=cluster_name)
    return {
        "openapi": "3.0.0",
        "info": {"title": "elb-openapi (stub)", "version": "0.0.0"},
        "paths": {},
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


@aks_router.post("/start")
def aks_start(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("aks/start", body=body, oid=caller.object_id)
    return {"status": "stub", "degraded": True, "degraded_reason": "celery_task_not_yet_implemented"}


@aks_router.post("/stop")
def aks_stop(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("aks/stop", body=body, oid=caller.object_id)
    return {"status": "stub", "degraded": True, "degraded_reason": "celery_task_not_yet_implemented"}


@aks_router.post("/delete")
def aks_delete(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("aks/delete", body=body, oid=caller.object_id)
    return {"status": "stub", "degraded": True, "degraded_reason": "celery_task_not_yet_implemented"}


@aks_router.post("/{cluster_name}/assign-roles")
def aks_assign_roles(
    cluster_name: str = Path(...),
    body: dict[str, Any] = Body(default={}),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("aks/assign-roles", cluster=cluster_name, oid=caller.object_id)
    return {
        "cluster_name": cluster_name,
        "roles_assigned": [],
        "status": "stub",
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


# ===========================================================================
# /api/blast/* — submit/jobs/databases/schedules
# ===========================================================================
blast_router = APIRouter(prefix="/api/blast", tags=["blast"])


@blast_router.get("/jobs")
def blast_jobs_list(
    limit: int = Query(default=50, ge=1, le=500),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """List the caller's BLAST jobs from the platform jobstate table.

    This is the read-only path; the legacy job_registry entity has been
    replaced by the Storage table created in storageState.bicep.
    """
    try:
        from api_app.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        rows = repo.list_for_owner(caller.object_id, limit=limit)
        # Filter to BLAST jobs only.
        blast_rows = [r for r in rows if r.type == "blast"]
        return {
            "jobs": [
                {
                    "job_id": j.job_id,
                    "status": j.status,
                    "phase": j.phase,
                    "task_id": j.task_id,
                    "created_at": j.created_at,
                    "updated_at": j.updated_at,
                    "error_code": j.error_code,
                    "payload": j.payload,
                }
                for j in blast_rows
            ]
        }
    except Exception as exc:
        LOGGER.warning("blast_jobs_list failed: %s", type(exc).__name__)
        return {"jobs": [], "degraded": True, "degraded_reason": "state_repo_unavailable"}


@blast_router.get("/jobs/{job_id}")
def blast_job_get(
    job_id: str = Path(...),
    history: int = Query(default=0),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api_app.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        if state.owner_oid and state.owner_oid != caller.object_id:
            raise HTTPException(403, "not owner")
        out: dict[str, Any] = {
            "job_id": state.job_id,
            "status": state.status,
            "phase": state.phase,
            "task_id": state.task_id,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "error_code": state.error_code,
            "payload": state.payload,
        }
        if history:
            out["history"] = repo.get_history(job_id, limit=200)
        return out
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_get failed: %s", type(exc).__name__)
        raise HTTPException(500, str(exc)) from exc


@blast_router.post("/jobs/{job_id}/cancel")
def blast_job_cancel(
    job_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/jobs/cancel", job_id=job_id, oid=caller.object_id)
    return {
        "job_id": job_id,
        "status": "stub",
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


@blast_router.post("/submit")
def blast_submit(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/submit", body_keys=list(body.keys()), oid=caller.object_id)
    instance_id = secrets.token_urlsafe(16)
    return {
        "id": instance_id,
        "statusQueryGetUri": f"/api/blast/submit/{instance_id}/status",
        "status": "stub",
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


@blast_router.get("/submit/{instance_id}/status")
def blast_submit_status(
    instance_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/submit/status", id=instance_id)
    return {
        "instance_id": instance_id,
        "runtime_status": "Pending",
        "custom_status": {"phase": "stub", "description": "Celery task pending"},
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


@blast_router.post("/upload-query")
def blast_upload_query(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/upload-query", oid=caller.object_id)
    return {
        "status": "stub",
        "degraded": True,
        "degraded_reason": "streaming_proxy_not_yet_implemented",
    }


@blast_router.get("/databases")
def blast_databases(
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/databases", sa=storage_account)
    return {
        "databases": [],
        "degraded": True,
        "degraded_reason": "blast_db_listing_not_yet_implemented",
    }


@blast_router.get("/databases/check-updates")
def blast_databases_check_updates(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/databases/check-updates")
    return {
        "updates_available": [],
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


@blast_router.get("/databases/versions")
def blast_databases_versions(
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/databases/versions", sa=storage_account)
    return {
        "versions": {},
        "degraded": True,
        "degraded_reason": "blast_db_listing_not_yet_implemented",
    }


# --- Schedules ---
@blast_router.get("/schedules")
def blast_schedules_list(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/list")
    return {
        "schedules": [],
        "degraded": True,
        "degraded_reason": "beat_scheduler_not_yet_implemented",
    }


@blast_router.post("/schedules")
def blast_schedules_create(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/create", body_keys=list(body.keys()))
    raise HTTPException(503, detail={
        "code": "celery_beat_pending",
        "message": "Beat-driven schedules not yet implemented in the Container Apps backend.",
    })


@blast_router.delete("/schedules/{schedule_id}")
def blast_schedules_delete(
    schedule_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/delete", id=schedule_id)
    raise HTTPException(503, detail={"code": "celery_beat_pending"})


@blast_router.post("/schedules/{schedule_id}/run")
def blast_schedules_run(
    schedule_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/run", id=schedule_id)
    raise HTTPException(503, detail={"code": "celery_beat_pending"})


# --- Result download / aggregate / export ---
@blast_router.get("/jobs/{job_id}/file")
def blast_job_file(
    job_id: str = Path(...),
    name: str = Query(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    max_bytes: int = Query(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/jobs/file", job_id=job_id, name=name)
    raise HTTPException(503, detail={
        "code": "streaming_proxy_pending",
        "message": "File download proxy not yet implemented.",
    })


@blast_router.get("/jobs/{job_id}/results")
def blast_job_results(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/jobs/results", job_id=job_id)
    return {
        "results": [],
        "degraded": True,
        "degraded_reason": "results_listing_not_yet_implemented",
    }


@blast_router.get("/jobs/{job_id}/results/aggregate")
def blast_job_results_aggregate(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/jobs/results/aggregate", job_id=job_id)
    return {
        "rows": [],
        "degraded": True,
        "degraded_reason": "results_listing_not_yet_implemented",
    }


@blast_router.get("/jobs/{job_id}/results/alignments")
def blast_job_results_alignments(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/jobs/results/alignments", job_id=job_id)
    return {
        "alignments": [],
        "degraded": True,
        "degraded_reason": "results_listing_not_yet_implemented",
    }


@blast_router.get("/jobs/{job_id}/results/download")
def blast_job_results_download(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    blob_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/jobs/results/download", job_id=job_id)
    raise HTTPException(503, detail={"code": "streaming_proxy_pending"})


@blast_router.get("/jobs/{job_id}/results/export")
def blast_job_results_export(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    format: str = Query(default="csv"),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/jobs/results/export", job_id=job_id, fmt=format)
    raise HTTPException(503, detail={"code": "streaming_proxy_pending"})


# ===========================================================================
# /api/warmup/* — Celery task placeholder
# ===========================================================================
warmup_router = APIRouter(prefix="/api/warmup", tags=["warmup"])


@warmup_router.post("/start")
def warmup_start(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("warmup/start", oid=caller.object_id)
    instance_id = secrets.token_urlsafe(16)
    return {
        "id": instance_id,
        "statusQueryGetUri": f"/api/warmup/{instance_id}/status",
        "status": "stub",
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


@warmup_router.get("/{instance_id}/status")
def warmup_status(
    instance_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("warmup/status", id=instance_id)
    return {
        "instance_id": instance_id,
        "runtime_status": "Pending",
        "custom_status": {"phase": "stub"},
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


# ===========================================================================
# /api/audit/log — best-effort read from jobhistory; empty if unavailable
# ===========================================================================
audit_router = APIRouter(prefix="/api/audit", tags=["audit"])


@audit_router.get("/log")
def audit_log(
    limit: int = Query(default=200, ge=1, le=1000),
    action: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("audit/log", limit=limit, action=action)
    return {
        "events": [],
        "degraded": True,
        "degraded_reason": "audit_aggregator_not_yet_implemented",
    }

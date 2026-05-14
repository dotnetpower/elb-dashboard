"""Stubs for endpoints that have not yet been ported from the legacy
Function App. They return well-structured empty/202 responses so the SPA
renders without crashing while the real implementations land.

Each stub logs a `STUB_CALLED` warning so we can see in App Insights which
endpoints the SPA actually exercises in production and prioritise the real
implementations accordingly.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from api.auth import CallerIdentity, require_caller
from api.services.aks_skus import DEFAULT_SKU, list_skus

LOGGER = logging.getLogger(__name__)


def _stub_log(name: str, **ctx: Any) -> None:
    LOGGER.warning("STUB_CALLED endpoint=%s ctx=%s", name, ctx)


# ===========================================================================
# /api/resources/* moved to api/routes/resources.py (real implementation).
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
    # Source-of-truth lives in api.services.aks_skus, which mirrors the
    # sibling repo's elastic_blast.azure_traits.AZURE_HPC_MACHINES allow-list.
    # Picking anything outside this list makes elastic-blast raise
    # NotImplementedError("Cannot get properties for ...") at submit time, so
    # the SPA dropdown must source its options from here.
    #
    # `degraded` stays True until a Celery task replaces this with a live
    # Microsoft.Compute/skus query that intersects with the allow-list and
    # filters by region availability. The static list is correct for the
    # SKU set elastic-blast understands; what's missing is per-region
    # availability and quota.
    skus = list_skus()
    return {
        "skus": [dict(s) for s in skus],
        "default_sku": DEFAULT_SKU,
        "degraded": True,
        "degraded_reason": "static_skus_celery_task_pending",
    }


@aks_router.post("/provision")
def aks_provision(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import provision_aks
    job_id = str(uuid.uuid4())
    result = provision_aks.delay(
        job_id=job_id,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        region=body.get("region", "koreacentral"),
        cluster_name=body.get("cluster_name", "elb-cluster"),
        node_sku=body.get("node_sku", DEFAULT_SKU),
        node_count=body.get("node_count", 3),
        acr_resource_group=body.get("acr_resource_group", ""),
        acr_name=body.get("acr_name", ""),
        storage_resource_group=body.get("storage_resource_group", ""),
        storage_account=body.get("storage_account", ""),
        caller_oid=caller.object_id,
    )
    return {
        "id": job_id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
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
    from api.tasks.azure import start_aks
    result = start_aks.delay(
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
    )
    return {"task_id": result.id, "status": "queued"}


@aks_router.post("/stop")
def aks_stop(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import stop_aks
    result = stop_aks.delay(
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
    )
    return {"task_id": result.id, "status": "queued"}


@aks_router.post("/delete")
def aks_delete(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import delete_aks
    result = delete_aks.delay(
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
    )
    return {"task_id": result.id, "status": "queued"}


@aks_router.post("/{cluster_name}/assign-roles")
def aks_assign_roles(
    cluster_name: str = Path(...),
    body: dict[str, Any] = Body(default={}),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import assign_aks_roles
    result = assign_aks_roles.delay(
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=cluster_name,
        acr_resource_group=body.get("acr_resource_group", ""),
        acr_name=body.get("acr_name", ""),
    )
    return {"task_id": result.id, "status": "queued"}


# ===========================================================================
# /api/acr/* — ACR image build
# ===========================================================================
acr_build_router = APIRouter(prefix="/api/acr", tags=["acr"])


@acr_build_router.post("/build-images")
def acr_build_images(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.acr import build_images
    result = build_images.delay(
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        registry_name=body.get("registry_name", ""),
        images=body.get("images"),
    )
    # For immediate feedback, return the expected images with "scheduled" status
    from api.services.image_tags import IMAGE_TAGS
    targets = body.get("images") or list(IMAGE_TAGS.keys())
    results = []
    for img in targets:
        tag = IMAGE_TAGS.get(img, "latest")
        results.append({"image": f"{img}:{tag}", "status": "scheduled"})
    return {"results": results, "task_id": result.id}


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
        from api.services.state_repo import JobStateRepository

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
        # Distinguish between "not configured" (local dev) and real errors
        exc_name = type(exc).__name__
        if exc_name == "RuntimeError" and "AZURE_TABLE_ENDPOINT" in str(exc):
            return {
                "jobs": [],
                "degraded": True,
                "degraded_reason": "not_configured",
                "message": "Job state storage is not configured. Set AZURE_TABLE_ENDPOINT to connect to Azure Table Storage.",
            }
        return {
            "jobs": [],
            "degraded": True,
            "degraded_reason": "state_repo_unavailable",
            "message": f"Could not reach job state storage: {exc_name}",
        }


@blast_router.get("/jobs/{job_id}")
def blast_job_get(
    job_id: str = Path(...),
    history: int = Query(default=0),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api.services.state_repo import JobStateRepository

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
    body: dict[str, Any] = Body(default={}),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.blast import cancel
    result = cancel.delay(
        job_id=job_id,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
        storage_account=body.get("storage_account", ""),
    )
    return {"job_id": job_id, "task_id": result.id, "status": "cancelling"}


@blast_router.post("/submit")
def blast_submit(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    # Input validation
    from api._http_utils import BlastSubmitRequest
    try:
        req = BlastSubmitRequest(**body)
    except Exception as exc:
        raise HTTPException(422, detail={"code": "validation_error", "message": str(exc)[:500]})

    # Capacity pre-check — verify AKS cluster exists and is running
    try:
        from api.services import get_credential
        from api.services.monitoring import list_aks_clusters
        cred = get_credential()
        sub = req.subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
        clusters = list_aks_clusters(cred, sub, req.resource_group)
        cluster = next((c for c in clusters if c.get("name") == req.cluster_name), None)
        if not cluster:
            raise HTTPException(409, detail={
                "code": "cluster_not_found",
                "message": f"AKS cluster '{req.cluster_name}' not found in '{req.resource_group}'",
                "retryable": False,
            })
        power = cluster.get("power_state", "")
        if power != "Running":
            raise HTTPException(503, detail={
                "code": "cluster_not_ready",
                "message": f"AKS cluster '{req.cluster_name}' is {power}. Start it first.",
                "retryable": True,
                "retry_after_seconds": 60,
            })
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("capacity pre-check failed (non-blocking): %s", exc)

    from api.tasks.blast import submit
    job_id = str(uuid.uuid4())
    # Create job state record
    try:
        from api.services.state_repo import JobState, JobStateRepository
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        repo = JobStateRepository()
        state = JobState(
            job_id=job_id, type="blast", status="queued",
            phase="queued", owner_oid=caller.object_id,
            tenant_id=caller.tenant_id, created_at=now, updated_at=now,
            payload=body,
        )
        repo.create(state)
    except Exception as exc:
        LOGGER.warning("failed to create job state: %s", exc)

    result = submit.delay(
        job_id=job_id,
        subscription_id=req.subscription_id,
        resource_group=req.resource_group,
        cluster_name=req.cluster_name,
        storage_account=req.storage_account,
        program=req.program,
        database=req.database,
        query_file=req.query_file,
        options=req.options,
        caller_oid=caller.object_id,
    )
    return {
        "id": job_id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
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
    if not storage_account or not resource_group:
        return {"databases": []}
    try:
        from api.services.storage_data import list_databases
        from api.services import get_credential
        cred = get_credential()
        databases = list_databases(cred, storage_account)
        return {"databases": databases}
    except Exception as exc:
        LOGGER.warning("blast_databases failed: %s", type(exc).__name__)
        # User-friendly messages for common failure modes
        err_str = str(exc)
        if "AuthorizationFailure" in err_str:
            return {
                "databases": [],
                "degraded": True,
                "degraded_reason": "access_denied",
                "message": (
                    f"Cannot access storage account '{storage_account}'. "
                    "In production, the Container App reaches storage via private endpoint. "
                    "Locally, assign 'Storage Blob Data Reader' role to your az login identity."
                ),
            }
        if "ResourceNotFound" in err_str or "AccountNotFound" in err_str:
            return {
                "databases": [],
                "degraded": True,
                "degraded_reason": "not_found",
                "message": f"Storage account '{storage_account}' not found in resource group '{resource_group}'.",
            }
        return {
            "databases": [],
            "degraded": True,
            "degraded_reason": type(exc).__name__,
            "message": f"Could not list databases: {type(exc).__name__}",
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
    from api.tasks.storage import warmup_database
    job_id = str(uuid.uuid4())
    # Create job state
    try:
        from api.services.state_repo import JobState, JobStateRepository
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        repo = JobStateRepository()
        state = JobState(
            job_id=job_id, type="warmup", status="queued",
            phase="queued", owner_oid=caller.object_id,
            tenant_id=caller.tenant_id, created_at=now, updated_at=now,
            payload=body,
        )
        repo.create(state)
    except Exception as exc:
        LOGGER.warning("failed to create warmup job state: %s", exc)

    result = warmup_database.delay(
        job_id=job_id,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        storage_account=body.get("storage_account", ""),
        database_name=body.get("database_name", ""),
        caller_oid=caller.object_id,
    )
    return {
        "id": job_id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
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
    """Return recent audit events from the jobhistory table."""
    try:
        from api.services.state_repo import JobStateRepository
        repo = JobStateRepository()
        # List recent jobs for the caller, then collect their history
        jobs = repo.list_for_owner(caller.object_id, limit=50)
        events: list[dict[str, Any]] = []
        for job in jobs[:20]:  # cap to avoid excessive table queries
            history = repo.get_history(job.job_id, limit=20)
            for h in history:
                if action and h.get("event") != action:
                    continue
                events.append({
                    "job_id": job.job_id,
                    "job_type": job.type,
                    "event": h.get("event", ""),
                    "ts": h.get("ts", ""),
                    "payload": h.get("payload_json", ""),
                })
                if len(events) >= limit:
                    break
            if len(events) >= limit:
                break
        events.sort(key=lambda e: e.get("ts", ""), reverse=True)
        return {"events": events[:limit]}
    except Exception as exc:
        LOGGER.warning("audit_log failed: %s", exc)
        return {"events": [], "error": str(exc)[:200]}

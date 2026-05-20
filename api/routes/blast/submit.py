"""/api/blast submit, pre-flight, and pending lab-tool routes."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _apply_web_blast_searchsp_default,
    _normalise_blast_submit_body,
    _stub_log,
)
from api.routes.blast.common import LAB_TOOL_PENDING

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.post("/pre-flight")
def blast_pre_flight(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Run pre-flight checks before BLAST submit.

    Validates that the required infrastructure is in place:
    AKS cluster running, storage accessible, database exists, query valid.
    """
    checks: list[dict[str, Any]] = []
    critical = 0

    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    cluster = body.get("cluster_name") or body.get("aks_cluster_name") or ""
    storage = body.get("storage_account", "")
    db = body.get("db") or body.get("database", "")
    raw_options = body.get("options") if isinstance(body.get("options"), dict) else {}
    precision_options = {**raw_options}
    for key in (
        "additional_options",
        "allow_approximate_sharding",
        "db_auto_partition",
        "db_partitions",
        "db_partition_prefix",
        "db_effective_search_space",
        "db_total_letters",
        "outfmt",
        "query_effective_search_spaces",
        "searchsp",
        "sharding_mode",
    ):
        if key in body:
            if key == "searchsp":
                precision_options.setdefault("db_effective_search_space", body[key])
            else:
                precision_options[key] = body[key]
    _apply_web_blast_searchsp_default(str(db), precision_options)

    # 1. AKS cluster check
    try:
        from api.services import get_credential
        from api.services.monitoring import list_aks_clusters

        cred = get_credential()
        clusters = list_aks_clusters(cred, sub, rg)
        found = next((c for c in clusters if c.get("name") == cluster), None)
        if not found:
            checks.append(
                {
                    "id": "aks_cluster",
                    "status": "fail",
                    "title": "AKS Cluster",
                    "detail": f"Cluster '{cluster}' not found in '{rg}'",
                    "severity": "critical",
                }
            )
            critical += 1
        elif found.get("power_state") != "Running":
            checks.append(
                {
                    "id": "aks_cluster",
                    "status": "fail",
                    "title": "AKS Cluster",
                    "detail": f"Cluster is {found.get('power_state', 'unknown')}. Start it first.",
                    "severity": "critical",
                    "action": "Start cluster",
                    "action_type": "start_cluster",
                }
            )
            critical += 1
        else:
            checks.append(
                {
                    "id": "aks_cluster",
                    "status": "pass",
                    "title": "AKS Cluster",
                    "detail": f"{cluster} is running ({found.get('node_count', '?')} nodes)",
                }
            )
    except Exception as exc:
        checks.append(
            {
                "id": "aks_cluster",
                "status": "warn",
                "title": "AKS Cluster",
                "detail": f"Could not verify: {type(exc).__name__}",
            }
        )

    # 2. Storage check
    if storage:
        checks.append(
            {
                "id": "storage",
                "status": "pass",
                "title": "Storage Account",
                "detail": f"{storage} configured",
            }
        )
    else:
        checks.append(
            {
                "id": "storage",
                "status": "fail",
                "title": "Storage Account",
                "detail": "No storage account configured",
                "severity": "critical",
            }
        )
        critical += 1

    # 3. Database check
    if db:
        checks.append(
            {
                "id": "database",
                "status": "pass",
                "title": "BLAST Database",
                "detail": f"Database '{db}' selected",
            }
        )
    else:
        checks.append(
            {
                "id": "database",
                "status": "fail",
                "title": "BLAST Database",
                "detail": "No database selected",
                "severity": "critical",
            }
        )
        critical += 1

    # 3b. Sharding precision policy check
    try:
        from api.services.sharding_precision import build_precision_report

        query_metadata = None
        query_count = body.get("query_count")
        query_data = body.get("query_data")
        if isinstance(query_data, str) and query_data.strip():
            from api.services.query_metadata import parse_fasta_metadata

            query_metadata = parse_fasta_metadata(query_data)
            query_count = query_metadata.query_count
        elif not isinstance(query_count, int):
            query_count = None
        shard_sets = body.get("shard_sets")
        if not isinstance(shard_sets, list):
            shard_sets = None
        precision_report = build_precision_report(
            precision_options,
            query_count=query_count,
            db_stats_available=bool(precision_options.get("db_total_letters")),
            shard_sets=shard_sets,
        )
        status = "pass" if precision_report.eligible else "fail"
        checks.append(
            {
                "id": "sharding_precision",
                "status": status,
                "title": "Sharding Precision",
                "detail": precision_report.precision_level,
                "severity": "critical" if not precision_report.eligible else None,
                "precision": precision_report.as_dict(),
                "query_metadata": query_metadata.as_dict() if query_metadata else None,
            }
        )
        if not precision_report.eligible:
            critical += 1
    except Exception as exc:
        checks.append(
            {
                "id": "sharding_precision",
                "status": "fail",
                "title": "Sharding Precision",
                "detail": str(exc)[:200],
                "severity": "critical",
            }
        )
        critical += 1

    # 4. Redis/Celery broker check
    try:
        from api.celery_app import celery_app

        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1, timeout=2)
        conn.close()
        checks.append(
            {
                "id": "broker",
                "status": "pass",
                "title": "Task Broker",
                "detail": "Redis is reachable",
            }
        )
    except Exception:
        checks.append(
            {
                "id": "broker",
                "status": "fail",
                "title": "Task Broker",
                "detail": "Redis is not reachable. Tasks cannot be queued.",
                "severity": "critical",
            }
        )
        critical += 1

    ready = critical == 0
    return {
        "ready": ready,
        "checks": checks,
        "critical_blockers": critical,
        "summary": "All checks passed" if ready else f"{critical} critical issue(s) found",
    }


@router.post("/submit")
def blast_submit(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    normalised_body = _normalise_blast_submit_body(body, job_id=job_id)

    # Input validation
    from api._http_utils import BlastSubmitRequest

    try:
        req = BlastSubmitRequest(**normalised_body)
    except Exception as exc:
        raise HTTPException(
            422,
            detail={"code": "validation_error", "message": str(exc)[:500]},
        ) from exc

    # Precision gate: exact/precise sharding claims must be validated before a
    # Celery task is queued. Approximate mode remains explicit and warning-only.
    try:
        from api.services.sharding_precision import build_precision_report, normalize_sharding_mode

        precision_options = dict(req.options or {})
        for key in ("query_count", "shard_sets"):
            if key in body and key not in precision_options:
                precision_options[key] = body[key]
        mode = normalize_sharding_mode(precision_options)
        if mode == "precise":
            report = build_precision_report(
                precision_options,
                query_count=precision_options.get("query_count"),
                db_stats_available=bool(precision_options.get("db_total_letters")),
                shard_sets=precision_options.get("shard_sets")
                if isinstance(precision_options.get("shard_sets"), list)
                else None,
            )
            if not report.eligible:
                raise HTTPException(
                    422,
                    detail={
                        "code": "sharding_precision_blocked",
                        "message": "; ".join(report.blocking_errors),
                        "precision": report.as_dict(),
                    },
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            422,
            detail={"code": "sharding_precision_invalid", "message": str(exc)[:500]},
        ) from exc

    # Capacity pre-check — verify AKS cluster exists and is running
    try:
        from api.services import get_credential
        from api.services.monitoring import list_aks_clusters

        cred = get_credential()
        sub = req.subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
        clusters = list_aks_clusters(cred, sub, req.resource_group)
        cluster = next((c for c in clusters if c.get("name") == req.cluster_name), None)
        if not cluster:
            raise HTTPException(
                409,
                detail={
                    "code": "cluster_not_found",
                    "message": (
                        f"AKS cluster '{req.cluster_name}' not found in '{req.resource_group}'"
                    ),
                    "retryable": False,
                },
            )
        power = cluster.get("power_state", "")
        if power != "Running":
            raise HTTPException(
                503,
                detail={
                    "code": "cluster_not_ready",
                    "message": f"AKS cluster '{req.cluster_name}' is {power}. Start it first.",
                    "retryable": True,
                    "retry_after_seconds": 60,
                },
            )
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("capacity pre-check failed (non-blocking): %s", exc)

    from api.tasks.blast import submit

    submit_options = dict(req.options or {})
    for key in ("acr_resource_group", "acr_name"):
        value = normalised_body.get(key)
        if value not in (None, ""):
            submit_options.setdefault(key, value)

    # Create job state record
    repo: Any = None
    try:
        from datetime import datetime

        from api.services.state_repo import JobState, JobStateRepository

        now = datetime.now(UTC).isoformat(timespec="seconds")
        repo = JobStateRepository()
        state = JobState(
            job_id=job_id,
            type="blast",
            status="queued",
            phase="queued",
            owner_oid=caller.object_id,
            tenant_id=caller.tenant_id,
            created_at=now,
            updated_at=now,
            payload=normalised_body,
        )
        repo.create(state)
    except Exception as exc:
        LOGGER.warning("failed to create job state: %s", exc)

    try:
        from api.routes import blast as blast_package

        result = blast_package._safe_delay(
            submit,
            job_id=job_id,
            subscription_id=req.subscription_id,
            resource_group=req.resource_group,
            cluster_name=req.cluster_name,
            storage_account=req.storage_account,
            program=req.program,
            database=req.database,
            query_file=req.query_file,
            options=submit_options,
            caller_oid=caller.object_id,
            caller_tenant_id=caller.tenant_id,
        )
    except HTTPException as exc:
        # Broker was unreachable. The Table row we just wrote would
        # otherwise sit stuck on `queued` forever — mark it as failed so
        # the dashboard surfaces the broker outage immediately and the
        # row stops counting as an "active" job.
        if repo is not None:
            try:
                repo.update(
                    job_id,
                    status="failed",
                    phase="broker_unavailable",
                    error_code="broker_unavailable",
                )
            except Exception as cleanup_exc:
                LOGGER.warning(
                    "submit broker failure cleanup failed job_id=%s: %s",
                    job_id,
                    type(cleanup_exc).__name__,
                )
        raise exc
    return {
        "id": job_id,
        "job_id": job_id,
        "task_id": result.id,
        "instance_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }


@router.post("/jobs", status_code=202)
def blast_job_submit(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Canonical BLAST job submit endpoint.

    Dashboard submissions continue to use the local Celery path. Inline FASTA
    submissions use the sibling OpenAPI execution plane but are exposed under
    the same `/api/blast/jobs` domain so clients do not need a second jobs API.
    """
    if "query_fasta" not in body:
        from api.routes import blast as blast_package

        return blast_package.blast_submit(body, caller)

    from api.routes.elastic_blast import ExternalBlastSubmitRequest
    from api.services import external_blast

    try:
        request = ExternalBlastSubmitRequest(**body)
    except Exception as exc:
        raise HTTPException(
            422, detail={"code": "validation_error", "message": str(exc)[:500]}
        ) from exc

    payload = request.model_dump(exclude_none=True)
    payload["submission_source"] = "external_api"
    LOGGER.info(
        "canonical external BLAST submit accepted caller_oid=%s db=%s program=%s",
        caller.object_id,
        request.db,
        request.program,
    )
    return external_blast.submit_job(payload)


@router.get("/submit/{instance_id}/status")
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


@router.post("/upload-query")
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


@router.post("/cost-estimate")
def blast_cost_estimate_stub(
    _body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/cost-estimate")
    raise HTTPException(503, detail=LAB_TOOL_PENDING)


@router.post("/preprocess")
def blast_preprocess_stub(
    _body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/preprocess")
    raise HTTPException(503, detail=LAB_TOOL_PENDING)


@router.post("/primer-design")
def blast_primer_design_stub(
    _body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/primer-design")
    raise HTTPException(503, detail=LAB_TOOL_PENDING)

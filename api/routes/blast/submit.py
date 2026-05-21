"""BLAST submit route and payload validation controller.

Responsibility: BLAST submit route and payload validation controller
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_submit_job_id`, `_submit_response`, `_validate_submit_contracts`,
`blast_submit`, `blast_job_submit`, `blast_submit_status`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, Response

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _normalise_blast_submit_body, _stub_log
from api.routes.blast.common import LAB_TOOL_PENDING
from api.services.blast_submit_payload import (
    canonical_submit_metadata,
    canonical_submit_snapshot,
    submit_contracts,
)
from api.services.response_contracts import (
    build_admission,
    build_meta,
    build_operation,
    build_target,
    request_id_from_scope,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _submit_job_id(body: dict[str, Any], caller: CallerIdentity) -> str:
    idempotency_key = body.get("idempotency_key")
    if isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256:
        scope = ":".join(
            [
                caller.tenant_id or "tenant",
                caller.object_id or "caller",
                idempotency_key,
            ]
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"elb-dashboard:blast-submit:{scope}"))
    return str(uuid.uuid4())


def _submit_response(
    job_id: str,
    task_id: str | None,
    *,
    status: str = "queued",
    operation_type: str = "blast.submit",
    request_id: str | None = None,
    openapi_job_id: str | None = None,
    admission_reason: str = "request_accepted",
) -> dict[str, Any]:
    instance_id = task_id or job_id
    operation_id = instance_id
    dashboard_status_url = f"/api/blast/jobs/{job_id}"
    operation_status_url = f"/api/operations/{operation_id}"
    return {
        "id": job_id,
        "job_id": job_id,
        "job_id_kind": "dashboard",
        "dashboard_job_id": job_id,
        "openapi_job_id": openapi_job_id,
        "task_id": task_id,
        "instance_id": instance_id,
        "statusQueryGetUri": f"/api/tasks/{instance_id}",
        "operation_status_url": operation_status_url,
        "status": status,
        "operation": build_operation(
            operation_id=operation_id,
            operation_type=operation_type,
            state=status,
            links={
                "self": operation_status_url,
                "target": dashboard_status_url,
                "events": f"{dashboard_status_url}/events",
            },
        ),
        "target": build_target(
            resource_type="blast_job",
            job_id=job_id,
            job_id_kind="dashboard",
            dashboard_job_id=job_id,
            openapi_job_id=openapi_job_id,
            links={
                "dashboard_status": dashboard_status_url,
                "events": f"{dashboard_status_url}/events",
            },
        ),
        "admission": build_admission(
            decision="accepted",
            reason=admission_reason,
            queue={"state": "accepted", "depth_bucket": "unknown", "poll_after_seconds": 5},
        ),
        "meta": build_meta(request_id=request_id),
    }


def _validate_submit_contracts(body: dict[str, Any]) -> dict[str, Any]:
    """Validate precision and Web BLAST compatibility before side effects."""

    try:
        contracts = submit_contracts(body)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            422,
            detail={"code": "sharding_precision_invalid", "message": str(exc)[:500]},
        ) from exc

    precision = contracts["precision"]
    if not precision.get("eligible"):
        raise HTTPException(
            422,
            detail={
                "code": "sharding_precision_blocked",
                "message": "; ".join(precision.get("blocking_errors") or []),
                "precision": precision,
            },
        )

    compatibility = contracts["compatibility_contract"]
    if not compatibility.get("eligible"):
        raise HTTPException(
            422,
            detail={
                "code": "web_blast_compatibility_blocked",
                "message": "; ".join(compatibility.get("blocking_errors") or []),
                "compatibility": compatibility,
            },
        )
    return contracts


def _reset_jobs_list_cache() -> None:
    try:
        from api.routes.blast.jobs import _reset_blast_jobs_list_cache

        _reset_blast_jobs_list_cache()
    except Exception as exc:
        LOGGER.debug("jobs list cache reset skipped: %s", type(exc).__name__)


@router.post("/submit")
def blast_submit(
    request: Request,
    response: Response,
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    job_id = _submit_job_id(body, caller)
    early_contracts = _validate_submit_contracts(body)
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
        from api.services.blast_compatibility import build_compatibility_contract
        from api.services.sharding_precision import build_precision_report, normalize_sharding_mode

        precision_options = dict(req.options or {})
        for key in ("query_count", "shard_sets"):
            if key in body and key not in precision_options:
                precision_options[key] = body[key]
        mode = normalize_sharding_mode(precision_options)
        report = build_precision_report(
            precision_options,
            query_count=precision_options.get("query_count"),
            db_stats_available=bool(precision_options.get("db_total_letters")),
            shard_sets=precision_options.get("shard_sets")
            if isinstance(precision_options.get("shard_sets"), list)
            else None,
        )
        if mode == "precise":
            if not report.eligible:
                raise HTTPException(
                    422,
                    detail={
                        "code": "sharding_precision_blocked",
                        "message": "; ".join(report.blocking_errors),
                        "precision": report.as_dict(),
                    },
                )
        compatibility_contract = build_compatibility_contract(
            database=req.database,
            options=precision_options,
            precision_report=report,
        )
        if not compatibility_contract.eligible:
            raise HTTPException(
                422,
                detail={
                    "code": "web_blast_compatibility_blocked",
                    "message": "; ".join(compatibility_contract.blocking_errors),
                    "compatibility": compatibility_contract.as_dict(),
                },
            )
        normalised_body["compatibility_contract"] = early_contracts["compatibility_contract"]
        from api.services.blast_provenance import build_blast_provenance

        normalised_body["provenance"] = build_blast_provenance(
            job_id=job_id,
            payload=normalised_body,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            422,
            detail={"code": "sharding_precision_invalid", "message": str(exc)[:500]},
        ) from exc

    # Keep submit latency low: ARM cluster readiness checks run in the worker
    # path unless explicitly enabled for diagnostics.
    if os.environ.get("BLAST_SUBMIT_SYNC_CAPACITY_CHECK", "").lower() == "true":
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
        if normalised_body.get("idempotency_key"):
            existing = repo.get(job_id)
            if existing is not None:
                operation_id = existing.task_id or job_id
                response.headers["Location"] = f"/api/operations/{operation_id}"
                response.headers["Retry-After"] = "5"
                return _submit_response(
                    job_id,
                    existing.task_id,
                    status=existing.status or "queued",
                    request_id=request_id_from_scope(request),
                    admission_reason="idempotent_replay_returned_existing_job",
                )
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
        _reset_jobs_list_cache()
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
        task_id = str(getattr(result, "id", "") or "")
        if repo is not None and task_id:
            try:
                repo.update(job_id, task_id=task_id)
                _reset_jobs_list_cache()
            except Exception as exc:
                LOGGER.warning(
                    "submit task id persist failed job_id=%s task_id=%s: %s",
                    job_id,
                    task_id,
                    type(exc).__name__,
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
                _reset_jobs_list_cache()
            except Exception as cleanup_exc:
                LOGGER.warning(
                    "submit broker failure cleanup failed job_id=%s: %s",
                    job_id,
                    type(cleanup_exc).__name__,
                )
        raise exc
    response.headers["Location"] = f"/api/operations/{task_id or job_id}"
    response.headers["Retry-After"] = "5"
    return _submit_response(
        job_id,
        task_id,
        request_id=request_id_from_scope(request),
        admission_reason="queued_for_blast_execution",
    )


@router.post("/jobs", status_code=202)
def blast_job_submit(
    request: Request,
    response: Response,
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Canonical BLAST job submit endpoint.

    Dashboard submissions continue to use the local Celery path. Inline FASTA
    submissions use the sibling OpenAPI execution plane but are exposed under
    the same `/api/blast/jobs` domain so clients do not need a second jobs API.
    """
    if "query_fasta" not in body:
        import inspect

        from api.routes import blast as blast_package

        delegate = blast_package.blast_submit
        if len(inspect.signature(delegate).parameters) <= 2:
            return delegate(body, caller)
        return delegate(request, response, body, caller)

    from api.routes.elastic_blast import ExternalBlastSubmitRequest
    from api.services import external_blast

    try:
        request = ExternalBlastSubmitRequest(**body)
    except Exception as exc:
        raise HTTPException(
            422, detail={"code": "validation_error", "message": str(exc)[:500]}
        ) from exc

    payload = request.model_dump(exclude_none=True)
    payload.update(canonical_submit_metadata(payload, submission_source="external_api"))
    payload["canonical_request"] = canonical_submit_snapshot(payload)
    payload.update(submit_contracts(payload))
    from api.services.blast_provenance import build_blast_provenance

    payload["provenance"] = build_blast_provenance(
        job_id=str(payload["external_correlation_id"]),
        payload=payload,
    )
    LOGGER.info(
        "canonical external BLAST submit accepted caller_oid=%s db=%s program=%s",
        caller.object_id,
        request.db,
        request.program,
    )
    upstream = external_blast.submit_job(payload)
    openapi_job_id = str(upstream.get("job_id") or "")
    dashboard_job_id = str(payload["external_correlation_id"])
    response.headers["Location"] = f"/api/blast/jobs/{dashboard_job_id}"
    response.headers["Retry-After"] = "5"
    return {
        **upstream,
        "status": upstream.get("status") or "accepted",
        "job_id_kind": "openapi",
        "dashboard_job_id": dashboard_job_id,
        "openapi_job_id": openapi_job_id or None,
        "operation": build_operation(
            operation_id=openapi_job_id or dashboard_job_id,
            operation_type="blast.submit.openapi",
            state=str(upstream.get("status") or "accepted"),
            links={
                "self": f"/api/operations/{openapi_job_id or dashboard_job_id}",
                "target": f"/api/blast/jobs/{dashboard_job_id}",
                "openapi_status": f"/v1/jobs/{openapi_job_id}/status" if openapi_job_id else "",
            },
        ),
        "target": build_target(
            resource_type="blast_job",
            job_id=dashboard_job_id,
            job_id_kind="dashboard",
            dashboard_job_id=dashboard_job_id,
            openapi_job_id=openapi_job_id or None,
            links={
                "dashboard_status": f"/api/blast/jobs/{dashboard_job_id}",
                "openapi_status": f"/v1/jobs/{openapi_job_id}/status" if openapi_job_id else "",
            },
        ),
        "admission": build_admission(
            decision="accepted",
            reason="accepted_by_openapi_execution_plane",
            queue={"state": "accepted", "depth_bucket": "unknown", "poll_after_seconds": 5},
        ),
        "meta": build_meta(request_id=request_id_from_scope(request)),
    }


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

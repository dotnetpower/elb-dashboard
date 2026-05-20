"""/api/blast job listing and lifecycle routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _EXTERNAL_DETAIL_ENRICH_LIMIT,
    _EXTERNAL_NOT_ENABLED_REASONS,
    _exception_reason,
    _external_job_detail_or_row,
    _external_list_jobs_cached,
    _external_to_blast_job,
    _local_state_matches_job_scope,
    _local_to_blast_job,
    _payload_value,
    _refresh_running_blast_state,
    _split_child_summaries_from_repo,
    _split_child_summary_from_repo,
    _sync_external_jobs_to_table,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/jobs")
def blast_jobs_list(
    limit: int = Query(default=50, ge=1, le=500),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    cluster_name: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """List BLAST jobs from the platform table plus external OpenAPI jobs.

    Local Table-backed rows win when both sources know the same job. Direct
    OpenAPI submissions live in the sibling service's ConfigMaps, so merging
    them here keeps the SPA on one canonical jobs endpoint.
    """
    jobs: list[dict[str, Any]] = []
    degraded: dict[str, Any] = {}
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        rows = [
            row
            for row in repo.list_for_owner(caller.object_id, limit=limit)
            if row.type == "blast"
            and _local_state_matches_job_scope(
                row,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
            )
        ]
        parent_ids = [row.job_id for row in rows]
        split_summaries = _split_child_summaries_from_repo(
            repo,
            caller.object_id,
            parent_ids,
        )
        for row in rows:
            jobs.append(
                _local_to_blast_job(
                    row,
                    split_children=split_summaries.get(row.job_id),
                )
            )
    except Exception as exc:
        LOGGER.warning("blast_jobs_list failed: %s", type(exc).__name__)
        exc_name = type(exc).__name__
        if exc_name == "RuntimeError" and "AZURE_TABLE_ENDPOINT" in str(exc):
            degraded = {
                "degraded": True,
                "degraded_reason": "not_configured",
                "message": (
                    "Job state storage is not configured. Set AZURE_TABLE_ENDPOINT "
                    "to connect to Azure Table Storage."
                ),
            }
        else:
            degraded = {
                "degraded": True,
                "degraded_reason": "state_repo_unavailable",
                "message": f"Could not reach job state storage: {exc_name}",
            }

    external_degraded: dict[str, Any] = {}
    try:
        from api.routes import blast as blast_package
        from api.services import external_blast

        external_kwargs = blast_package._openapi_client_kwargs_from_cluster(
            subscription_id,
            resource_group,
            cluster_name,
        )
        external_rows = _external_list_jobs_cached(external_kwargs)
        # Collect external rows first (with detail enrichment), then sync to
        # Table Storage in one batch. The sync call tells us which rows are
        # tombstoned in our Table so we can drop them from the list view —
        # otherwise a soft-deleted job reappears on every poll because the
        # upstream plane still remembers it.
        candidate_rows: list[dict[str, Any]] = []
        if isinstance(external_rows, list):
            seen = {str(job.get("job_id")) for job in jobs}
            detail_budget = min(_EXTERNAL_DETAIL_ENRICH_LIMIT, max(0, limit - len(jobs)))
            for row in external_rows:
                if not isinstance(row, dict):
                    continue
                job_id = str(row.get("job_id") or "")
                if not job_id or job_id in seen:
                    continue
                if detail_budget > 0:
                    row = _external_job_detail_or_row(external_blast, row, external_kwargs)
                    detail_budget -= 1
                candidate_rows.append(row)

        # Sync newly-discovered external jobs into Table Storage so they
        # survive AKS restarts and appear on future list calls even when
        # the external OpenAPI plane is unavailable.
        #
        # owner_oid is intentionally blank — these jobs originate from the
        # cluster, not from a specific dashboard caller, and must be
        # visible to every caller with ARM scope on the cluster. The
        # route-level cluster/RG/sub filter still scopes the read.
        tombstoned_ids: set[str] = set()
        if candidate_rows:
            _created, _updated, tombstoned_ids = _sync_external_jobs_to_table(
                candidate_rows,
                caller_oid="",
                tenant_id=getattr(caller, "tenant_id", ""),
            )

        for row in candidate_rows:
            job_id = str(row.get("job_id") or "")
            if job_id in tombstoned_ids:
                # Soft-deleted in our Table; suppress from the list view
                # so the row stays gone after the user's delete click.
                continue
            jobs.append(_external_to_blast_job(row))
    except Exception as exc:
        LOGGER.info("external blast job list unavailable: %s", _exception_reason(exc))
        reason = _exception_reason(exc)
        # `openapi_not_configured` / `openapi_not_enabled` mean the optional
        # external OpenAPI plane simply isn't deployed yet — that's a normal
        # state, not a degradation. Skip surfacing it as `external_degraded`
        # so the request inspector doesn't show a perpetual red badge.
        if reason not in _EXTERNAL_NOT_ENABLED_REASONS:
            external_degraded = {
                "external_degraded": True,
                "external_degraded_reason": reason,
            }

    jobs.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
    response: dict[str, Any] = {"jobs": jobs[:limit]}
    if degraded and not jobs:
        response.update(degraded)
    if external_degraded:
        response.update(external_degraded)
    return response


@router.get("/jobs/{job_id}")
def blast_job_get(
    job_id: str = Path(...),
    history: int = Query(default=0),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    local_unavailable: Exception | None = None
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(job_id)
        if state is not None:
            if state.owner_oid and state.owner_oid != caller.object_id:
                raise HTTPException(403, "not owner")
            state = _refresh_running_blast_state(repo, state)
            out = _local_to_blast_job(
                state,
                split_children=_split_child_summary_from_repo(repo, state.job_id),
            )
            if history:
                out["history"] = repo.get_history(job_id, limit=200)
            return out
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_get failed: %s", type(exc).__name__)
        local_unavailable = exc

    try:
        from api.services import external_blast

        return _external_to_blast_job(external_blast.get_job(job_id))
    except HTTPException as exc:
        if exc.status_code == 404 and local_unavailable is not None:
            raise HTTPException(
                503,
                f"local job state unavailable: {type(local_unavailable).__name__}",
            ) from exc
        raise
    except Exception as exc:
        if local_unavailable is not None:
            raise HTTPException(
                503,
                "local job state unavailable: "
                f"{type(local_unavailable).__name__}; external lookup unavailable: "
                f"{type(exc).__name__}",
            ) from exc
        raise HTTPException(404, "job not found") from exc


@router.post("/jobs/{job_id}/cancel")
def blast_job_cancel(
    job_id: str = Path(...),
    body: dict[str, Any] | None = Body(default=None),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.blast import cancel

    request_body = dict(body or {})
    try:
        from api.services.state_repo import JobStateRepository

        state = JobStateRepository().get(job_id)
        if state is not None:
            if state.owner_oid and state.owner_oid != caller.object_id:
                raise HTTPException(403, "not owner")
            payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
            for body_key, payload_keys in {
                "subscription_id": ("subscription_id",),
                "resource_group": ("resource_group",),
                "cluster_name": ("cluster_name", "aks_cluster_name"),
                "storage_account": ("storage_account",),
            }.items():
                if request_body.get(body_key) in (None, ""):
                    value = _payload_value(payload, *payload_keys)
                    if value not in (None, ""):
                        request_body[body_key] = value
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.info(
            "blast cancel state context unavailable job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )

    from api.routes import blast as blast_package

    result = blast_package._safe_delay(
        cancel,
        job_id=job_id,
        subscription_id=request_body.get("subscription_id", ""),
        resource_group=request_body.get("resource_group", ""),
        cluster_name=request_body.get("cluster_name", ""),
        storage_account=request_body.get("storage_account", ""),
    )
    return {"job_id": job_id, "task_id": result.id, "status": "cancelling"}


@router.delete("/jobs/{job_id}")
def blast_job_delete(
    job_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Delete a job record from the state repository."""
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        if state.owner_oid and state.owner_oid != caller.object_id:
            raise HTTPException(403, "not owner")
        repo.update(job_id, status="deleted", phase="deleted")
        return {"job_id": job_id, "status": "deleted"}
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_delete failed: %s", exc)
        return {"job_id": job_id, "status": "deleted"}

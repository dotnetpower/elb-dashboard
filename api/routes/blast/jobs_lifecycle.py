"""/api/blast job lifecycle routes (cancel, delete).

Responsibility: Owner-scoped `/api/blast/jobs/{job_id}` mutation routes that cancel an
in-flight run or tombstone a job record.
Edit boundaries: Keep HTTP validation and dispatch here; the actual cancel side effect runs in
`api.tasks.blast.cancel` (local jobs) or the OpenAPI sibling's `DELETE /v1/jobs/{id}` (external
jobs). Shared helpers stay in `api/routes/_blast_shared.py`; the job listing / read routes live
in `jobs.py` / `jobs_detail.py`. This router is included onto `jobs.router`.
Key entry points: `blast_job_cancel`, `blast_job_delete`.
Risky contracts: Every route enforces `require_caller` + `_assert_job_owner`. External-job
cancel must route to the sibling (the dashboard cannot reach the sibling's AKS cluster with the
coordinates it has). `_safe_delay` / `_openapi_client_kwargs_from_cluster` are reached through
`api.routes.blast` (the package) so test monkeypatches on the package keep working.
Validation: `uv run pytest -q api/tests/test_blast_jobs_routes.py
api/tests/test_external_blast_api.py api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _assert_job_owner,
    _payload_value,
    _reset_external_jobs_cache,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _state_is_external(state: Any) -> bool:
    """Return True when the job state row originated from the OpenAPI sibling.

    External jobs are synced into the Table by ``_sync_external_jobs_to_table``
    with ``owner_upn="api"`` and a ``payload={"external": ...}`` envelope. Both
    markers are checked so the detection survives a partially-populated row.
    """
    if state is None:
        return False
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    if isinstance(payload.get("external"), dict):
        return True
    return str(getattr(state, "owner_upn", "") or "") == "api"


def _cancel_external_job(
    job_id: str,
    state: Any,
    request_body: dict[str, Any],
) -> dict[str, Any]:
    """Cancel an OpenAPI-sibling job via its own ``DELETE /v1/jobs/{id}``.

    External jobs run on the sibling's AKS cluster; the dashboard does not
    know (and must not guess) those coordinates. Routing the cancel to the
    sibling lets it stop the run with its in-cluster kubeconfig. The local
    Table row is then flipped to ``cancelled`` so the SPA reflects the change
    immediately and the next list sync keeps the row tombstoned.
    """
    from api.routes import blast as blast_package
    from api.services import external_blast

    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    external = payload.get("external") if isinstance(payload.get("external"), dict) else {}
    openapi_job_id = str(external.get("job_id") or job_id)

    external_kwargs = blast_package._openapi_client_kwargs_from_cluster(
        str(request_body.get("subscription_id") or ""),
        str(request_body.get("resource_group") or ""),
        str(request_body.get("cluster_name") or ""),
    )
    try:
        external_blast.delete_job(openapi_job_id, **external_kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning(
            "external blast cancel failed job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )
        raise HTTPException(
            502,
            detail={
                "code": "external_cancel_failed",
                "message": f"Could not cancel job on the OpenAPI service: {type(exc).__name__}",
            },
        ) from exc

    try:
        from api.services.state_repo import get_state_repo

        get_state_repo().update(job_id, status="cancelled", phase="cancelled")
    except Exception as exc:
        LOGGER.info(
            "external blast cancel local state update skipped job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )
    _reset_external_jobs_cache()
    return {"job_id": job_id, "status": "cancelled", "openapi_job_id": openapi_job_id}


@router.post("/jobs/{job_id}/cancel")
def blast_job_cancel(
    job_id: str = Path(...),
    body: dict[str, Any] | None = Body(default=None),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.blast import cancel

    request_body = dict(body or {})
    state: Any = None
    try:
        from api.services.state_repo import get_state_repo

        state = get_state_repo().get(job_id)
        if state is not None:
            _assert_job_owner(state.owner_oid, caller)
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

    # External (OpenAPI sibling) jobs run on the sibling's own AKS cluster.
    # The dashboard cannot reach that cluster's K8s API with the coordinates
    # it has (they default to the workspace anchor cluster, which is the wrong
    # one), so the direct k8s cancel task fails with `cancel_unavailable`.
    # Route these to the sibling's DELETE endpoint instead — it owns the run.
    if _state_is_external(state):
        return _cancel_external_job(job_id, state, request_body)

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


@router.post("/jobs/{job_id}/retry")
def blast_job_retry(
    job_id: str = Path(..., min_length=1, max_length=128),
    body: dict[str, Any] | None = Body(default=None),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Re-submit a transient-failed job with its original parameters (one-click).

    Only ``failure_classification.auto_retryable`` (transient submit-phase)
    failures are accepted — a one-click resubmit of a K8s runtime failure would
    orphan a cluster job and re-stage the database, so those are steered to the
    Duplicate flow (which lets the researcher review + re-enter the query). The
    enqueue happens before the state flip so a broker outage leaves the job in
    its terminal ``failed`` state.
    """
    del body  # context is reconstructed from the stored row, not the request
    try:
        from api.services.state_repo import get_state_repo

        state = get_state_repo().get(job_id)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("manual retry state read failed job_id=%s: %s", job_id, type(exc).__name__)
        raise HTTPException(
            503,
            detail={"code": "state_unavailable", "message": "job state is temporarily unavailable"},
        ) from exc

    if state is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "job not found"})
    _assert_job_owner(state.owner_oid, caller)

    if _state_is_external(state):
        raise HTTPException(
            400,
            detail={
                "code": "external_not_retryable",
                "message": "external (OpenAPI) jobs are managed by the producing service",
            },
        )

    if str(getattr(state, "status", "") or "") != "failed":
        raise HTTPException(
            409,
            detail={"code": "not_failed", "message": "only a failed job can be retried"},
        )

    from api.services.blast.failure_classification import classify_failure

    classification = classify_failure(
        str(getattr(state, "error_code", "") or ""), str(getattr(state, "phase", "") or "")
    )
    if not classification.auto_retryable:
        raise HTTPException(
            400,
            detail={
                "code": "not_retryable",
                "message": (
                    f"{classification.category} failures are not one-click retryable; "
                    "use Duplicate to review and resubmit"
                ),
                "category": classification.category,
            },
        )

    from api.services.blast.auto_retry import (
        AutoRetryMeta,
        max_auto_retries,
        merge_meta_into_payload,
        restore_submit_kwargs,
    )

    kwargs = restore_submit_kwargs(state)
    if kwargs is None:
        raise HTTPException(
            400,
            detail={
                "code": "unrestorable",
                "message": "original submit parameters could not be reconstructed; use Duplicate",
            },
        )

    from api.routes import blast as blast_package
    from api.tasks.blast.submit_task import submit

    result = blast_package._safe_delay(submit, **kwargs)
    task_id = str(getattr(result, "id", "") or "")

    # Manual retry clears the auto-retry counter + quarantine (the user is
    # explicitly starting over) and drops the stale progress timeline so the
    # resubmitted task rebuilds it. A failure here is non-fatal — the task is
    # already enqueued and owns its own state transitions.
    try:
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        reset_meta = AutoRetryMeta(count=0, max=max_auto_retries(), quarantined=False)
        merged = merge_meta_into_payload(getattr(state, "payload", None), reset_meta)
        merged.pop("_progress", None)
        repo.update(
            job_id,
            status="queued",
            phase="queued",
            error_code="",
            task_id=task_id,
            payload=merged,
        )
        repo.append_history(
            job_id,
            "manual_retry",
            {"by": caller.object_id, "auto_retry": reset_meta.as_dict()},
        )
    except Exception as exc:
        LOGGER.warning(
            "manual retry row flip failed (task already enqueued) job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )

    return {"job_id": job_id, "task_id": task_id, "status": "queued"}


@router.delete("/jobs/{job_id}")
def blast_job_delete(
    job_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Delete a job record from the state repository."""
    try:
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        _assert_job_owner(state.owner_oid, caller)
        # Recursively purge the job's result/query directories (best-effort,
        # dfs-only). Fixes the historical soft-delete-only leak where result
        # blobs accumulated forever. A no-op when STORAGE_DFS_ENABLED is off, so
        # the legacy tombstone-only behaviour is preserved by default. Never
        # raises — a storage failure must not block the tombstone.
        from api.services.storage.job_purge import purge_job_result_storage

        purge = purge_job_result_storage(state)
        repo.update(job_id, status="deleted", phase="deleted")
        _reset_external_jobs_cache()
        return {
            "job_id": job_id,
            "status": "deleted",
            "storage_purged": bool(purge.get("purged")),
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_delete failed: %s", exc)
        return {"job_id": job_id, "status": "deleted"}

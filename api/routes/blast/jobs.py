"""/api/blast job listing and lifecycle routes.

Responsibility: /api/blast job listing and lifecycle routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_local_list_row_may_have_split_children`, `_blast_jobs_list_cache_key`,
`_blast_jobs_list_cache_get`, `blast_jobs_list`, `blast_job_execution_steps`, `blast_job_get`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request

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
    _reset_external_jobs_cache,
    _split_child_summaries_from_repo,
    _split_child_summary_from_repo,
    _sync_external_jobs_to_table,
)
from api.services.blast.job_state import _K8S_REFRESH_PHASES
from api.services.response_contracts import build_meta, request_id_from_scope

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# The frontend polls ``/api/blast/jobs`` every ~14 s. A 10 s TTL keeps the
# common case (single user staring at the Jobs page) as a cache hit while
# tab-switching or page reloads still see fresh data within one cycle.
_JOBS_LIST_CACHE_TTL_SECONDS = 10.0
_JOBS_LIST_CACHE_MAX_ENTRIES = 128
# Store the serialized JSON bytes so cache get/set never deepcopies. JSON
# round-trip gives callers a fresh mutable dict (same isolation as a deep
# copy) without ``copy.deepcopy``'s O(N) traversal of nested lists. The
# OrderedDict supports O(1) LRU eviction via ``popitem(last=False)``.
_JOBS_LIST_CACHE: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_JOBS_LIST_CACHE_LOCK = threading.Lock()

_EXTERNAL_LIST_DETAIL_STATUSES = frozenset(
    {
        "pending",
        "queued",
        "running",
        "submitted",
        "submitting",
        "inprogress",
        "in_progress",
        "splitting",
        "reducing",
    }
)
_LOCAL_SPLIT_PARENT_PHASES = frozenset(
    {
        "split_queries_started",
        "split_children_failed",
        "split_children_submitted",
        "split_children_aggregating",
        "split_children_cancelled",
        "split_children_merge_ready",
        "split_results_merging",
        "split_results_merge_invalid",
    }
)


def _local_list_row_may_have_split_children(row: Any) -> bool:
    phase = str(getattr(row, "phase", "") or "").strip().casefold()
    status = str(getattr(row, "status", "") or "").strip().casefold()
    return phase in _LOCAL_SPLIT_PARENT_PHASES or status in _LOCAL_SPLIT_PARENT_PHASES


def _blast_jobs_list_cache_key(
    *,
    caller_oid: str,
    limit: int,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> str:
    return json.dumps(
        {
            "caller_oid": caller_oid,
            "limit": limit,
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": cluster_name,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _blast_jobs_list_cache_get(key: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _JOBS_LIST_CACHE_LOCK:
        entry = _JOBS_LIST_CACHE.get(key)
        if entry is None:
            return None
        expires_at, payload_bytes = entry
        if expires_at <= now:
            _JOBS_LIST_CACHE.pop(key, None)
            return None
        # Touch for LRU semantics so frequently-read entries stay warm.
        _JOBS_LIST_CACHE.move_to_end(key)
    # json.loads outside the lock — deserialization is the only per-call
    # cost and we don't want it blocking other readers.
    decoded = json.loads(payload_bytes)
    return decoded if isinstance(decoded, dict) else None


def _blast_jobs_list_cache_set(key: str, response: dict[str, Any]) -> None:
    payload_bytes = json.dumps(response, separators=(",", ":")).encode("utf-8")
    expires_at = time.monotonic() + _JOBS_LIST_CACHE_TTL_SECONDS
    with _JOBS_LIST_CACHE_LOCK:
        # Replacing an existing key needs explicit pop so move_to_end-on-set
        # semantics don't collide with the LRU bookkeeping.
        _JOBS_LIST_CACHE.pop(key, None)
        _JOBS_LIST_CACHE[key] = (expires_at, payload_bytes)
        while len(_JOBS_LIST_CACHE) > _JOBS_LIST_CACHE_MAX_ENTRIES:
            _JOBS_LIST_CACHE.popitem(last=False)


def _reset_blast_jobs_list_cache() -> None:
    with _JOBS_LIST_CACHE_LOCK:
        _JOBS_LIST_CACHE.clear()


def _external_list_row_needs_detail(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or row.get("phase") or "").strip().casefold()
    return status in _EXTERNAL_LIST_DETAIL_STATUSES


def _external_row_with_scope_defaults(
    row: dict[str, Any],
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    if not (subscription_id or resource_group or cluster_name):
        return row
    scoped = dict(row)
    if subscription_id:
        scoped.setdefault("subscription_id", subscription_id)
    if resource_group:
        scoped.setdefault("resource_group", resource_group)
    if cluster_name:
        scoped.setdefault("cluster_name", cluster_name)
    return scoped


@router.get("/jobs")
def blast_jobs_list(
    request: Request,
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
    cache_key = _blast_jobs_list_cache_key(
        caller_oid=caller.object_id,
        limit=limit,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    cached = _blast_jobs_list_cache_get(cache_key)
    if cached is not None:
        return cached

    jobs: list[dict[str, Any]] = []
    degraded: dict[str, Any] = {}
    try:
        from api.services.state.repository import get_state_repo

        repo = get_state_repo()
        rows = [
            row
            for row in repo.list_for_owner(caller.object_id, limit=limit, include_payload=False)
            if row.type == "blast"
            and _local_state_matches_job_scope(
                row,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
            )
        ]
        # Refresh active rows against K8s before responding so the list page
        # doesn't have to wait for the 60 s beat reconcile to flip a finished
        # job to "completed". The refresh helper already early-returns for
        # non-active rows and shares a per-job throttle (5 s for hot phases,
        # 20 s for `submitted`) with the detail endpoint.
        for idx, row in enumerate(rows):
            if str(getattr(row, "phase", "") or "").strip().casefold() not in _K8S_REFRESH_PHASES:
                continue
            try:
                refreshed = _refresh_running_blast_state(repo, row)
            except Exception as exc:
                LOGGER.debug(
                    "blast_jobs_list refresh skipped job_id=%s: %s",
                    row.job_id,
                    type(exc).__name__,
                )
                continue
            if refreshed is not row:
                rows[idx] = refreshed
        parent_ids = [row.job_id for row in rows if _local_list_row_may_have_split_children(row)]
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
            for ext_row in external_rows:
                if not isinstance(ext_row, dict):
                    continue
                job_id = str(ext_row.get("job_id") or "")
                if not job_id or job_id in seen:
                    continue
                ext_row = _external_row_with_scope_defaults(
                    ext_row,
                    subscription_id=subscription_id,
                    resource_group=resource_group,
                    cluster_name=cluster_name,
                )
                if detail_budget > 0 and _external_list_row_needs_detail(ext_row):
                    ext_row = _external_job_detail_or_row(external_blast, ext_row, external_kwargs)
                    detail_budget -= 1
                candidate_rows.append(ext_row)

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

        for ext_row in candidate_rows:
            job_id = str(ext_row.get("job_id") or "")
            if job_id in tombstoned_ids:
                # Soft-deleted in our Table; suppress from the list view
                # so the row stays gone after the user's delete click.
                continue
            jobs.append(_external_to_blast_job(ext_row))
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
    response: dict[str, Any] = {
        "jobs": jobs[:limit],
        "meta": build_meta(request_id=request_id_from_scope(request)),
    }
    if degraded and not jobs:
        response.update(degraded)
    if external_degraded:
        response.update(external_degraded)
    _blast_jobs_list_cache_set(cache_key, response)
    return response


@router.get("/jobs/{job_id}/execution-steps")
def blast_job_execution_steps(
    job_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the lightweight Execution Steps snapshot for a job.

    The execution-steps blob is written ONCE by ``finalize_job_artifacts``
    right when the job reaches a terminal phase. K8s pod log tails on
    ``running.last_output`` and other trailing fields can still be backfilled
    by reconcile beats AFTER that write, so the persisted blob can be
    silently stale. Prefer the live Table-backed snapshot so trailing
    backfill surfaces in the UI; fall back to the persisted blob only if
    Table is unreachable.
    """
    try:
        from api.services.job_artifacts import (
            artifact_state_payload,
            build_execution_steps_snapshot,
            read_execution_steps_snapshot,
        )
        from api.services.state.repository import get_state_repo

        repo = get_state_repo()
        summary = repo.get_summary(job_id)
        if summary is None:
            raise HTTPException(404, "job not found")
        if summary.owner_oid and summary.owner_oid != caller.object_id:
            raise HTTPException(403, "not owner")

        live_state = None
        live_error: Exception | None = None
        try:
            live_state = repo.get(job_id)
        except Exception as exc:
            live_error = exc
            LOGGER.info(
                "execution steps live state unavailable job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )

        if live_state is not None:
            payload = build_execution_steps_snapshot(live_state)
            artifact_state = artifact_state_payload(job_id, "execution_steps")
            if artifact_state:
                payload["artifact_state"] = artifact_state.get(
                    "artifact_state", payload.get("artifact_state", "missing")
                )
                if artifact_state.get("error_code"):
                    payload["artifact_error_code"] = artifact_state.get("error_code")
            return payload

        # Live read failed: fall back to the persisted snapshot blob.
        try:
            snapshot = read_execution_steps_snapshot(job_id)
        except Exception as exc:
            LOGGER.info(
                "execution steps snapshot unavailable job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )
            snapshot = None
        if snapshot is not None:
            return {**snapshot, "artifact_state": "ready"}

        if live_error is not None:
            raise live_error
        raise HTTPException(404, "job not found")
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_execution_steps failed: %s", type(exc).__name__)
        raise HTTPException(
            503,
            {
                "code": "execution_steps_unavailable",
                "message": f"Could not read execution steps: {type(exc).__name__}",
            },
        ) from exc


@router.get("/jobs/{job_id}")
def blast_job_get(
    request: Request,
    job_id: str = Path(...),
    history: int = Query(default=0),
    include_database_metadata: bool = Query(default=True),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    local_unavailable: Exception | None = None
    try:
        from api.services.state.repository import get_state_repo

        repo = get_state_repo()
        state = repo.get(job_id)
        if state is not None:
            if state.owner_oid and state.owner_oid != caller.object_id:
                raise HTTPException(403, "not owner")
            state = _refresh_running_blast_state(repo, state)
            split_children = None
            if _local_list_row_may_have_split_children(state):
                split_children = _split_child_summary_from_repo(repo, state.job_id)
            out = _local_to_blast_job(
                state,
                split_children=split_children,
                include_database_metadata=include_database_metadata,
            )
            if history:
                out["history"] = repo.get_history(job_id, limit=200)
            out["meta"] = build_meta(request_id=request_id_from_scope(request))
            return out
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_get failed: %s", type(exc).__name__)
        local_unavailable = exc

    try:
        from api.services import external_blast

        out = _external_to_blast_job(
            external_blast.get_job(job_id),
            include_database_metadata=True,
        )
        out["meta"] = build_meta(request_id=request_id_from_scope(request))
        return out
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


@router.get("/jobs/{job_id}/events")
def blast_job_events(
    job_id: str = Path(...),
    limit: int = Query(default=200, ge=1, le=500),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api.services.blast.events import canonical_job_events
        from api.services.state.repository import get_state_repo

        repo = get_state_repo()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        if state.owner_oid and state.owner_oid != caller.object_id:
            raise HTTPException(403, "not owner")
        return {
            "job_id": job_id,
            "events": canonical_job_events(repo.get_history(job_id, limit=limit)),
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_events failed: %s", type(exc).__name__)
        raise HTTPException(
            503,
            {
                "code": "job_events_unavailable",
                "message": f"Could not read job events: {type(exc).__name__}",
            },
        ) from exc


@router.get("/jobs/{job_id}/queue")
def blast_job_queue(
    job_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api.services.blast.queue import queue_snapshot
        from api.services.state.repository import get_state_repo

        repo = get_state_repo()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        if state.owner_oid and state.owner_oid != caller.object_id:
            raise HTTPException(403, "not owner")
        return queue_snapshot(repo.list_active(job_type="blast", limit=500), job_id=job_id)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_queue failed: %s", type(exc).__name__)
        raise HTTPException(
            503,
            {
                "code": "job_queue_unavailable",
                "message": f"Could not read queue state: {type(exc).__name__}",
            },
        ) from exc


@router.post("/jobs/{job_id}/cancel")
def blast_job_cancel(
    job_id: str = Path(...),
    body: dict[str, Any] | None = Body(default=None),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.blast import cancel

    request_body = dict(body or {})
    try:
        from api.services.state.repository import get_state_repo

        state = get_state_repo().get(job_id)
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
        from api.services.state.repository import get_state_repo

        repo = get_state_repo()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        if state.owner_oid and state.owner_oid != caller.object_id:
            raise HTTPException(403, "not owner")
        repo.update(job_id, status="deleted", phase="deleted")
        _reset_external_jobs_cache()
        return {"job_id": job_id, "status": "deleted"}
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_delete failed: %s", exc)
        return {"job_id": job_id, "status": "deleted"}

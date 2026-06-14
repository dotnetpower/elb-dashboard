"""/api/blast job listing and lifecycle routes.

Responsibility: /api/blast job listing and lifecycle routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_local_list_row_may_have_split_children`, `blast_jobs_list`,
`blast_jobs_by_accession`, `blast_job_execution_steps`, `blast_job_get`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
)

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _EXTERNAL_DETAIL_ENRICH_LIMIT,
    _EXTERNAL_NOT_ENABLED_REASONS,
    _assert_job_owner,
    _blocked_refresh_reasons,
    _exception_reason,
    _external_to_blast_job,
    _local_state_matches_job_scope,
    _local_to_blast_job,
    _refresh_running_blast_state,
    _split_child_summaries_from_repo,
    _split_child_summary_from_repo,
    blast_shared_visibility_enabled,
)
from api.services.blast.external_jobs import collect_and_sync_external_jobs
from api.services.blast.job_state import _K8S_REFRESH_PHASES
from api.services.blast.jobs_list_cache import (
    begin_jobs_list_revalidate,
    end_jobs_list_revalidate,
    jobs_list_cache_get,
    jobs_list_cache_get_swr,
    jobs_list_cache_key,
    jobs_list_cache_set,
    reset_jobs_list_cache,
)
from api.services.response_contracts import build_meta, request_id_from_scope

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# Backward-compatible aliases. The jobs-list response cache infrastructure now
# lives in ``api.services.blast.jobs_list_cache``; these names keep existing
# call sites (and ``conftest``'s ``_reset_blast_jobs_list_cache`` import) working.
_blast_jobs_list_cache_key = jobs_list_cache_key
_blast_jobs_list_cache_get = jobs_list_cache_get
_blast_jobs_list_cache_set = jobs_list_cache_set
_reset_blast_jobs_list_cache = reset_jobs_list_cache

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


def _external_degraded_message(exc: Exception, reason: str) -> str:
    """Build a short, human-readable note for a failed external `/v1/jobs` poll.

    The list route never raises on an external-plane failure (it degrades to the
    locally-recorded rows), so the SPA only ever sees `external_degraded=True` +
    `external_degraded_reason`. Without a message the Recent searches page used
    to swallow the failure silently — the user saw an incomplete list with no
    hint that OpenAPI-submitted jobs could not be loaded. Prefer the upstream
    client's already-sanitised structured detail message, then fall back to a
    reason-shaped sentence.
    """
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            message = detail.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:300]
    if reason == "openapi_unreachable":
        return (
            "Could not reach the OpenAPI execution plane (the AKS cluster may be "
            "stopped). Jobs submitted directly through OpenAPI are not shown."
        )
    return (
        "Could not load jobs from the OpenAPI execution plane "
        f"({reason}). Locally-recorded jobs are still shown."
    )


@router.get("/jobs")
def blast_jobs_list(
    request: Request,
    background_tasks: BackgroundTasks,
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

    Served stale-while-revalidate: a fresh cache entry is returned directly, a
    stale one is returned immediately while a single background task rebuilds
    it, and only a cold (or past-stale-ceiling) key pays the synchronous build.
    This hides the cold-build latency (external OpenAPI cluster discovery plus
    per-cluster ``/v1/jobs`` fetches) from the ~14 s polling caller.
    """
    shared_visibility = blast_shared_visibility_enabled()
    cache_key = _blast_jobs_list_cache_key(
        caller_oid=caller.object_id,
        limit=limit,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        shared_visibility=shared_visibility,
    )
    request_id = request_id_from_scope(request)
    cached, is_stale = jobs_list_cache_get_swr(cache_key)
    if cached is not None and not is_stale:
        return cached
    if cached is not None and is_stale:
        # Serve the stale payload now and rebuild off the request path so the
        # cold-build latency never blocks a poll. Single-flight: a burst of
        # polls that all see the same stale entry enqueues exactly one rebuild.
        if begin_jobs_list_revalidate(cache_key):
            background_tasks.add_task(
                _revalidate_blast_jobs_list,
                cache_key=cache_key,
                caller_oid=caller.object_id,
                tenant_id=getattr(caller, "tenant_id", ""),
                limit=limit,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
                shared_visibility=shared_visibility,
                request_id=request_id,
            )
        return cached

    response = _compute_blast_jobs_response(
        caller_oid=caller.object_id,
        tenant_id=getattr(caller, "tenant_id", ""),
        limit=limit,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        shared_visibility=shared_visibility,
        request_id=request_id,
    )
    jobs_list_cache_set(cache_key, response)
    return response


def _revalidate_blast_jobs_list(
    *,
    cache_key: str,
    caller_oid: str,
    tenant_id: str,
    limit: int,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    shared_visibility: bool,
    request_id: str,
) -> None:
    """Background stale-while-revalidate rebuild of one jobs-list cache entry.

    Single-flight is enforced by the caller via ``begin_jobs_list_revalidate``;
    this only releases the slot in its ``finally`` so a crash mid-build cannot
    wedge future revalidations. Failures are swallowed (logged) — the stale
    entry already went to the user and the next poll retries.
    """
    try:
        response = _compute_blast_jobs_response(
            caller_oid=caller_oid,
            tenant_id=tenant_id,
            limit=limit,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            shared_visibility=shared_visibility,
            request_id=request_id,
        )
        jobs_list_cache_set(cache_key, response)
    except Exception as exc:
        LOGGER.warning(
            "blast_jobs_list background revalidate failed: %s", type(exc).__name__
        )
    finally:
        end_jobs_list_revalidate(cache_key)


def _compute_blast_jobs_response(
    *,
    caller_oid: str,
    tenant_id: str,
    limit: int,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    shared_visibility: bool,
    request_id: str,
) -> dict[str, Any]:
    """Build the BLAST jobs-list response payload (no caching side effects).

    Extracted from the route so stale-while-revalidate can rebuild it from a
    background task without a live ``Request``. The caller identity is reduced
    to ``caller_oid`` / ``tenant_id`` and the request id is passed in for the
    response ``meta``.
    """
    jobs: list[dict[str, Any]] = []
    degraded: dict[str, Any] = {}
    try:
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        scoped_listing = bool(subscription_id or resource_group or cluster_name)
        if scoped_listing and hasattr(repo, "list_for_scope"):
            source_rows = repo.list_for_scope(
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
                limit=limit,
                include_payload=False,
            )
        elif shared_visibility and hasattr(repo, "list_all"):
            # Dev-stage owner-agnostic listing: Recent searches shows every
            # submitter's jobs. Gated by BLAST_JOBS_SHARED_VISIBILITY.
            source_rows = repo.list_all(limit=limit, include_payload=False)
        else:
            source_rows = repo.list_for_owner(
                caller_oid, limit=limit, include_payload=False
            )
        rows = [
            row
            for row in source_rows
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
        #
        # First gate the active rows against ARM cluster health: a stopped or
        # deleted cluster can't be refreshed via the K8s API, so we skip that
        # refresh (avoids a ~10 s timeout per job) and tag the row as stale
        # below so the SPA shows "status frozen — cluster stopped".
        blocked_refresh = _blocked_refresh_reasons(rows)
        for idx, row in enumerate(rows):
            if str(row.job_id) in blocked_refresh:
                continue
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
        if shared_visibility:
            # Child rows carry the parent's owner_oid, so group the parents by
            # owner and query each owner's children. Keeps split-job rollups
            # working for jobs submitted by other callers in dev mode.
            split_summaries: dict[str, dict[str, Any]] = {}
            parents_by_owner: dict[str, list[str]] = {}
            for row in rows:
                if not _local_list_row_may_have_split_children(row):
                    continue
                owner = str(getattr(row, "owner_oid", "") or "")
                parents_by_owner.setdefault(owner, []).append(row.job_id)
            for owner_oid, owned_parent_ids in parents_by_owner.items():
                split_summaries.update(
                    _split_child_summaries_from_repo(repo, owner_oid, owned_parent_ids)
                )
        else:
            split_summaries = _split_child_summaries_from_repo(
                repo,
                caller_oid,
                parent_ids,
            )
        for row in rows:
            health = blocked_refresh.get(str(row.job_id))
            jobs.append(
                _local_to_blast_job(
                    row,
                    split_children=split_summaries.get(row.job_id),
                    refresh_blocked_reason=str(health.get("reason")) if health else None,
                    cluster_power_state=(
                        str(health.get("power_state"))
                        if health and health.get("power_state")
                        else None
                    ),
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
        # Discover external `/v1/jobs` for this scope and upsert them into the
        # Table (cluster-shared, owner_oid=""), then merge the discovered rows
        # into the response. The shared service owns target resolution + detail
        # enrichment + the Table sync; the route keeps the tombstone filter +
        # response shaping + degraded-badge policy. The sync survives AKS
        # restarts and is the same path the Message Flow card relies on, so the
        # two views can never drift on how `/v1/jobs` jobs reach the Table.
        sync = collect_and_sync_external_jobs(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            tenant_id=tenant_id,
            seen_job_ids={str(job.get("job_id")) for job in jobs},
            detail_enrich_budget=min(
                _EXTERNAL_DETAIL_ENRICH_LIMIT, max(0, limit - len(jobs))
            ),
        )

        for ext_row in sync.rows:
            job_id = str(ext_row.get("job_id") or "")
            if job_id in sync.tombstoned_ids:
                # Soft-deleted in our Table; suppress from the list view
                # so the row stays gone after the user's delete click.
                continue
            jobs.append(_external_to_blast_job(ext_row))

        # Surface external_degraded only when EVERY target failed (none
        # reachable). A partial success — at least one cluster answered —
        # keeps the list usable and is not flagged, since the history view's
        # whole point is "show whatever jobs we can find across clusters".
        if not sync.any_target_ok and sync.target_failures:
            raise sync.target_failures[0]
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
                "external_degraded_message": _external_degraded_message(exc, reason),
            }

    jobs.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
    response: dict[str, Any] = {
        "jobs": jobs[:limit],
        "meta": build_meta(request_id=request_id),
    }
    if degraded and not jobs:
        response.update(degraded)
    if external_degraded:
        response.update(external_degraded)
    return response


@router.get("/jobs/by-accession/{accession}")
def blast_jobs_by_accession(
    request: Request,
    accession: str = Path(...),
    match: str = Query(default="base", pattern="^(base|exact)$"),
    limit: int = Query(default=10, ge=1, le=50),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """List the caller's accession-mode BLAST jobs that used ``accession`` as query.

    Powers the Sequence Detail "Your BLAST jobs for this accession" card. The
    lookup is strictly owner-scoped (``caller.object_id``) — a caller only ever
    sees their own jobs. Registered above ``/jobs/{job_id}`` so the literal
    ``by-accession`` segment is never captured as a ``job_id``.

    Read-only and additive: a jobstate failure degrades to
    ``200 { degraded: true, reason }`` so the Sequence Detail page never 500s
    because of this card.
    """
    accession = accession.strip()
    from api.services.blast.job_back_reference import accession_base

    acc_base = accession_base(accession)
    jobs: list[dict[str, Any]] = []
    degraded = False
    reason: str | None = None
    try:
        from api.services.blast.job_back_reference import find_jobs_for_accession
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        jobs = find_jobs_for_accession(
            repo,
            caller.object_id,
            accession,
            match=match,
            limit=limit,
        )
    except Exception as exc:
        LOGGER.warning("blast_jobs_by_accession failed: %s", type(exc).__name__)
        degraded = True
        reason = "jobstate_unavailable"
    LOGGER.info(
        "blast_jobs_by_accession match=%s count=%d degraded=%s",
        match,
        len(jobs),
        degraded,
    )
    return {
        "accession": accession,
        "accession_base": acc_base,
        "match": match,
        "count": len(jobs),
        "jobs": jobs,
        "degraded": degraded,
        "reason": reason,
        "meta": build_meta(request_id=request_id_from_scope(request)),
    }


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
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        state = repo.get(job_id)
        if state is not None:
            _assert_job_owner(state.owner_oid, caller)
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


# ---------------------------------------------------------------------------
# Sub-routers: the single-job read routes and the cancel/delete lifecycle
# routes live in sibling modules to keep this file focused on listing +
# the `/jobs/{job_id}` projection. They are merged onto ``router`` here so
# ``blast/__init__.py`` keeps including a single ``jobs.router``. The route
# functions are re-exported so existing ``from api.routes.blast.jobs import
# blast_job_*`` imports (and ``__init__`` re-exports) keep working.
# ---------------------------------------------------------------------------
from api.routes.blast.jobs_detail import (  # noqa: E402
    blast_job_citation as blast_job_citation,
)
from api.routes.blast.jobs_detail import (  # noqa: E402
    blast_job_events as blast_job_events,
)
from api.routes.blast.jobs_detail import (  # noqa: E402
    blast_job_execution_steps as blast_job_execution_steps,
)
from api.routes.blast.jobs_detail import (  # noqa: E402
    blast_job_query as blast_job_query,
)
from api.routes.blast.jobs_detail import (  # noqa: E402
    blast_job_queue as blast_job_queue,
)
from api.routes.blast.jobs_detail import router as _jobs_detail_router  # noqa: E402
from api.routes.blast.jobs_lifecycle import (  # noqa: E402
    blast_job_cancel as blast_job_cancel,
)
from api.routes.blast.jobs_lifecycle import (  # noqa: E402
    blast_job_delete as blast_job_delete,
)
from api.routes.blast.jobs_lifecycle import router as _jobs_lifecycle_router  # noqa: E402

router.include_router(_jobs_detail_router)
router.include_router(_jobs_lifecycle_router)

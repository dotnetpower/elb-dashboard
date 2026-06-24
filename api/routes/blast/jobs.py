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
from api.services.response_contracts import (
    build_meta,
    build_page,
    request_id_from_scope,
)
from api.services.state.time_index import (
    decode_cursor,
    encode_cursor,
    row_key,
    time_index_enabled,
)

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
    cursor: str = Query(default=""),
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
        cursor=cursor,
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
                cursor=cursor,
            )
        return cached

    # Cold cache (no entry, or past the stale ceiling with no in-flight
    # rebuild). The full enriched build fans out to per-active-job K8s status
    # refresh + external OpenAPI `/v1/jobs` discovery + Table sync, which on a
    # busy fleet was measured at p90 ~250 s (max ~20 min) — long enough that the
    # SPA's first poll showed the never-resolving "JOBS loading…" spinner.
    #
    # Serve a FAST local-Table-only payload first so first paint is instant,
    # then rebuild the enriched version in the background (stale-while-
    # revalidate, applied to the cold case). Guard: only short-circuit when the
    # fast build actually has local rows to show. With an empty local result the
    # enriched build is the ONLY source of jobs (external-OpenAPI-only
    # deployments, or a degraded local Table whose jobs live in the sibling), so
    # fall through to the full synchronous build instead of flashing an empty
    # list and hiding those jobs for a poll cycle.
    fast_response = _compute_blast_jobs_response(
        caller_oid=caller.object_id,
        tenant_id=getattr(caller, "tenant_id", ""),
        limit=limit,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        shared_visibility=shared_visibility,
        request_id=request_id,
        cursor=cursor,
        skip_enrichment=True,
    )
    if fast_response.get("jobs"):
        jobs_list_cache_set(cache_key, fast_response)
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
                cursor=cursor,
            )
        return fast_response

    response = _compute_blast_jobs_response(
        caller_oid=caller.object_id,
        tenant_id=getattr(caller, "tenant_id", ""),
        limit=limit,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        shared_visibility=shared_visibility,
        request_id=request_id,
        cursor=cursor,
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
    cursor: str = "",
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
            cursor=cursor,
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
    cursor: str = "",
    skip_enrichment: bool = False,
) -> dict[str, Any]:
    """Build the BLAST jobs-list response payload (no caching side effects).

    Extracted from the route so stale-while-revalidate can rebuild it from a
    background task without a live ``Request``. The caller identity is reduced
    to ``caller_oid`` / ``tenant_id`` and the request id is passed in for the
    response ``meta``.

    ``skip_enrichment`` produces a FAST local-Table-only payload: it skips the
    per-active-job K8s status refresh AND the external OpenAPI ``/v1/jobs``
    discovery + Table sync, which are the bulk of the cold-build latency. The
    cheap per-cluster ARM health gate still runs, so a frozen running row is
    still tagged ``stale`` ("cluster stopped"). The route uses it on a cold
    cache so the first poll returns last-known local rows instantly instead of
    blocking on the minutes-long enrichment fan-out; a full (enriched) rebuild
    then replaces the entry in the background. Statuses are at worst one
    background-rebuild cadence behind, and external rows appear on the next
    poll — the same eventual-consistency the stale-while-revalidate path
    already relies on.
    """
    jobs: list[dict[str, Any]] = []
    degraded: dict[str, Any] = {}
    # Fetch one extra row across every source so ``has_more`` in the pagination
    # envelope is honest without a server-side ordered index: if the merged set
    # exceeds ``limit`` there is at least one more page. The extra row is
    # dropped by the final ``jobs[:limit]`` slice and never reaches the client.
    fetch_limit = limit + 1
    scoped_listing = bool(subscription_id or resource_group or cluster_name)
    # Keyset pagination is index-only and owner/all-scope-only. The time-ordered
    # index keys on the immutable (owner_oid, created_at) pair; ``list_for_scope``
    # is a mutable-column scan (cluster_name/subscription_id/resource_group can be
    # rewritten by ``update()``), so a scoped listing can only serve the first
    # page and reports ``next_cursor=None``. A non-empty ``cursor`` therefore
    # only steers the owner/all branches, and only when the index flag is on.
    paginating = bool(cursor) and time_index_enabled() and not scoped_listing
    # Decode the keyset boundary once so external rows newer-than-or-equal to it
    # (i.e. already shown on a previous page) are dropped from a cursor page.
    # The local index page is already filtered by ``RowKey gt cursor``; without
    # this, the unbounded external /v1/jobs merge would re-add the newest jobs
    # on top of every subsequent page and break keyset pagination with dupes.
    cursor_boundary = decode_cursor(cursor) if paginating else ""
    try:
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        if scoped_listing and hasattr(repo, "list_for_scope"):
            source_rows = repo.list_for_scope(
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
                limit=fetch_limit,
                include_payload=False,
            )
        elif shared_visibility and hasattr(repo, "list_all"):
            # Dev-stage owner-agnostic listing: Recent searches shows every
            # submitter's jobs. Gated by BLAST_JOBS_SHARED_VISIBILITY.
            if paginating and hasattr(repo, "list_all_page"):
                source_rows, _ = repo.list_all_page(
                    limit=fetch_limit, include_payload=False, cursor=cursor
                )
            else:
                source_rows = repo.list_all(limit=fetch_limit, include_payload=False)
        else:
            if paginating and hasattr(repo, "list_owner_page"):
                source_rows, _ = repo.list_owner_page(
                    caller_oid,
                    limit=fetch_limit,
                    include_payload=False,
                    cursor=cursor,
                )
            else:
                source_rows = repo.list_for_owner(
                    caller_oid, limit=fetch_limit, include_payload=False
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
        # below so the SPA shows "status frozen — cluster stopped". This ARM
        # health probe is per-distinct-cluster (cached) and cheap, so it runs
        # even on the cold fast-paint path — it is what produces the honest
        # "stale" badge on a frozen running row.
        blocked_refresh = _blocked_refresh_reasons(rows)
        # ``skip_enrichment`` (cold-cache fast paint) bypasses only the
        # expensive per-active-job K8s status refresh below — the bulk of the
        # cold build latency — and serves last-known local status. The
        # background rebuild does the full refresh.
        if not skip_enrichment:
            for idx, row in enumerate(rows):
                if str(row.job_id) in blocked_refresh:
                    continue
                if (
                    str(getattr(row, "phase", "") or "").strip().casefold()
                    not in _K8S_REFRESH_PHASES
                ):
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
    if not skip_enrichment:
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
                    _EXTERNAL_DETAIL_ENRICH_LIMIT, max(0, fetch_limit - len(jobs))
                ),
                # #51: bound the external /v1/jobs fetch to ~one page instead of
                # pulling the full cluster list every poll. Discovered rows still
                # sync into the local Table, so the bounded local index (#50)
                # remains the source of truth for pagination.
                limit=fetch_limit,
            )

            for ext_row in sync.rows:
                job_id = str(ext_row.get("job_id") or "")
                if job_id in sync.tombstoned_ids:
                    # Soft-deleted in our Table; suppress from the list view
                    # so the row stays gone after the user's delete click.
                    continue
                if cursor_boundary:
                    # Cursor page: skip external rows at-or-newer-than the page
                    # boundary (RowKey <= cursor). The local index already
                    # filtered to RowKey gt cursor; these rows were shown on an
                    # earlier page, so re-merging them would duplicate.
                    try:
                        ext_key = row_key(str(ext_row.get("created_at") or ""), job_id)
                    except Exception:
                        ext_key = ""
                    if ext_key and ext_key <= cursor_boundary:
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
    has_more = len(jobs) > limit
    page_jobs = jobs[:limit]
    # Keyset cursor for the NEXT page: the (created_at, job_id) of the last row
    # actually shown. External OpenAPI rows are synced into the local Table, so
    # they re-enter the time index and the keyset stays the single source of
    # truth across both sources. Only emitted for owner/all listings with the
    # index flag on — scoped (mutable-column scan) listings stay first-page-only.
    next_cursor: str | None = None
    if has_more and page_jobs and not scoped_listing and time_index_enabled():
        last = page_jobs[-1]
        try:
            next_cursor = encode_cursor(
                row_key(
                    str(last.get("created_at") or ""),
                    str(last.get("job_id") or ""),
                )
            )
        except Exception:
            next_cursor = None
    response: dict[str, Any] = {
        "jobs": page_jobs,
        "page": build_page(
            limit=limit,
            returned=len(page_jobs),
            has_more=has_more,
            next_cursor=next_cursor,
        ),
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


def _maybe_recover_external_failure_error(repo: Any, state: Any) -> Any:
    """Self-heal a pre-existing external failed row that has no ``error_code``.

    The sync-time recovery in ``_sync_external_jobs_to_table`` only fires on the
    FAILED transition, so an external job that was already ``failed`` before
    that recovery shipped — or a submit-time failure (memory-fit / config
    rejection) that leaves no Storage ``FAILURE.txt`` for the detail-view
    enrichment to read — keeps the generic "no error detail" banner forever.
    On the single-job detail render we recover the real cause from the sibling
    ``/jobs/{id}`` detail ONCE and persist it into the indexed ``error_code``
    column so subsequent renders read it without another upstream call.

    Best-effort: only fires for an external-origin ``failed`` row with an empty
    ``error_code``; a sibling outage / unresolved endpoint degrades to the row
    unchanged (generic banner preserved). Never raises.
    """
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    if not isinstance(payload.get("external"), dict):
        return state
    if str(getattr(state, "status", "") or "").lower() != "failed":
        return state
    if (str(getattr(state, "error_code", "") or "")).strip():
        return state
    from api.services.blast.external_jobs import _recover_external_failure_error

    infrastructure = {
        "subscription_id": str(getattr(state, "subscription_id", "") or ""),
        "resource_group": str(getattr(state, "resource_group", "") or ""),
        "cluster_name": str(getattr(state, "cluster_name", "") or ""),
    }
    recovered = _recover_external_failure_error(str(state.job_id), infrastructure)
    if not recovered:
        return state
    try:
        repo.update(state.job_id, error_code=recovered)
    except Exception as exc:
        LOGGER.debug(
            "external failure error persist skipped job_id=%s: %s",
            state.job_id,
            type(exc).__name__,
        )
    # Reflect the recovered cause in this response. ``state`` is a mutable
    # ``JobState`` dataclass, so the attribute assignment always succeeds even
    # when the persist above failed (the in-memory value still feeds the
    # projection so the banner is correct for this render).
    state.error_code = recovered
    return state


@router.get("/jobs/{job_id}")
def blast_job_get(
    request: Request,
    job_id: str = Path(...),
    history: int = Query(default=0),
    include_database_metadata: bool = Query(default=True),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    cluster_name: str = Query(default=""),
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
            state = _maybe_recover_external_failure_error(repo, state)
            split_children = None
            if _local_list_row_may_have_split_children(state):
                split_children = _split_child_summary_from_repo(repo, state.job_id)
            out = _local_to_blast_job(
                state,
                split_children=split_children,
                include_database_metadata=include_database_metadata,
            )
            if not str(out.get("query_label") or "").strip():
                # External (OpenAPI / Service Bus) jobs remember their inline
                # FASTA defline label only in ephemeral OPS Redis, which is
                # wiped on every Container App revision restart — after which
                # the Run details header shows "Query ID: —". Recover the
                # identity durably from the query blob (detail view only; one
                # capped Storage read) and re-remember it so the next jobs-list
                # sync persists it back to the Table row.
                try:
                    from api.services.blast.job_state import derive_external_query_label

                    recovered_label = derive_external_query_label(job_id, caller)
                except HTTPException:
                    raise
                except Exception as exc:
                    LOGGER.debug(
                        "query label recovery skipped job_id=%s: %s",
                        job_id,
                        type(exc).__name__,
                    )
                    recovered_label = ""
                if recovered_label:
                    out["query_label"] = recovered_label
                    try:
                        from api.services.blast.external_query_labels import (
                            remember_query_label,
                        )

                        remember_query_label(job_id, recovered_label)
                    except Exception as exc:
                        LOGGER.debug(
                            "query label re-remember skipped job_id=%s: %s",
                            job_id,
                            type(exc).__name__,
                        )
            # Task 1: merge live sibling stats (db_version / blast_version /
            # run_seconds) for a COMPLETED external job whose stored row predates
            # capture — the sibling /v1/jobs record carries them but the
            # dashboard row does not. One best-effort call on the detail view
            # (never the list); gated to terminal-success so it never
            # double-fetches a failed job (whose error path already fetched it)
            # and only when the stats are actually missing.
            if (
                str(out.get("submission_source") or "")
                in ("servicebus", "external_api")
                and str(out.get("status") or "").lower() in ("completed", "succeeded")
                and not out.get("db_version")
            ):
                from api.services.blast.external_config import (
                    recall_sibling_stats,
                    remember_sibling_stats,
                )

                _stat_keys = (
                    "db_version",
                    "blast_version",
                    "run_seconds",
                    "queue_wait_seconds",
                    "elapsed_seconds",
                )
                # Hardening: serve from the OPS-Redis cache first so a stopped-
                # cluster job's detail does not re-pay a 10 s sibling timeout on
                # every open. Only fetch live on a cache miss.
                _cached = recall_sibling_stats(job_id)
                if _cached:
                    for _k in _stat_keys:
                        if out.get(_k) in (None, "") and _cached.get(_k) not in (None, ""):
                            out[_k] = _cached.get(_k)
                if not out.get("db_version"):
                    try:
                        from api.services import external_blast

                        _openapi_id = str(out.get("openapi_job_id") or "") or job_id
                        _sib = external_blast.get_job(
                            _openapi_id,
                            **{
                                k: v
                                for k, v in {
                                    "subscription_id": subscription_id,
                                    "resource_group": resource_group,
                                    "cluster_name": cluster_name,
                                }.items()
                                if v
                            },
                        )
                        if isinstance(_sib, dict):
                            _fetched: dict[str, Any] = {}
                            for _k in _stat_keys:
                                if _sib.get(_k) not in (None, ""):
                                    _fetched[_k] = _sib.get(_k)
                                    if out.get(_k) in (None, ""):
                                        out[_k] = _sib.get(_k)
                            if _fetched:
                                remember_sibling_stats(job_id, _fetched)
                    except Exception as exc:
                        LOGGER.debug(
                            "sibling stats merge skipped job_id=%s: %s",
                            job_id,
                            type(exc).__name__,
                        )
            if history:
                hist = repo.get_history(job_id, limit=200)
                out["history"] = hist
                # Derive the message lifecycle trace (enqueued → … →
                # completion_published, with dwell/latency metrics) from the
                # same history rows so the Run details tab can render where the
                # message is and how long each hop took.
                from api.services.blast.message_trace import derive_trace

                out["message_trace"] = derive_trace(hist)
            out["meta"] = build_meta(request_id=request_id_from_scope(request))
            return out
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_get failed: %s", type(exc).__name__)
        local_unavailable = exc

    try:
        from api.services import external_blast

        external_kwargs = {
            key: value
            for key, value in {
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
            }.items()
            if value
        }
        out = _external_to_blast_job(
            external_blast.get_job(job_id, **external_kwargs),
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

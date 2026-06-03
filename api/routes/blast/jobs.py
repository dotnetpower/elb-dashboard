"""/api/blast job listing and lifecycle routes.

Responsibility: /api/blast job listing and lifecycle routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_local_list_row_may_have_split_children`, `blast_jobs_list`,
`blast_job_execution_steps`, `blast_job_get`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _EXTERNAL_DETAIL_ENRICH_LIMIT,
    _EXTERNAL_NOT_ENABLED_REASONS,
    _assert_job_owner,
    _blocked_refresh_reasons,
    _exception_reason,
    _external_job_detail_or_row,
    _external_list_jobs_cached,
    _external_to_blast_job,
    _local_state_matches_job_scope,
    _local_to_blast_job,
    _payload_value,
    _queries_blob_path,
    _refresh_running_blast_state,
    _reset_external_jobs_cache,
    _split_child_summaries_from_repo,
    _split_child_summary_from_repo,
    _sync_external_jobs_to_table,
    blast_shared_visibility_enabled,
)
from api.services.blast.job_state import _K8S_REFRESH_PHASES
from api.services.blast.jobs_list_cache import (
    jobs_list_cache_get,
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
        shared_visibility=blast_shared_visibility_enabled(),
    )
    cached = _blast_jobs_list_cache_get(cache_key)
    if cached is not None:
        return cached

    jobs: list[dict[str, Any]] = []
    degraded: dict[str, Any] = {}
    try:
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        scoped_listing = bool(subscription_id or resource_group or cluster_name)
        shared_visibility = blast_shared_visibility_enabled()
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
                caller.object_id, limit=limit, include_payload=False
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
                caller.object_id,
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
                should_enrich_detail = bool(subscription_id or resource_group or cluster_name)
                if (
                    should_enrich_detail
                    and detail_budget > 0
                    and _external_list_row_needs_detail(ext_row)
                ):
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
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        summary = repo.get_summary(job_id)
        if summary is None:
            raise HTTPException(404, "job not found")
        _assert_job_owner(summary.owner_oid, caller)

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


@router.get("/jobs/{job_id}/citation")
def blast_job_citation(
    job_id: str = Path(...),
    format: str = Query(default="text", pattern="^(text|markdown|bibtex)$"),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return a copy-ready Methods paragraph / Markdown / BibTeX for the run.

    The citation is synthesised from the persisted provenance bundle alone, so
    this route performs no extra Azure data-plane calls. Storage URLs and SAS
    tokens are never emitted.
    """
    try:
        from api.services.blast.citation import build_citation
        from api.services.blast.provenance import build_blast_provenance
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        _assert_job_owner(state.owner_oid, caller)

        raw_payload = getattr(state, "payload", None)
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        provenance = payload.get("provenance")
        if not isinstance(provenance, dict):
            provenance = build_blast_provenance(job_id=job_id, payload=payload)
        job_title = getattr(state, "job_title", None) or payload.get("job_title")

        bundle = build_citation(
            job_id=job_id,
            provenance=provenance,
            job_title=job_title if isinstance(job_title, str) else None,
        )
        return {
            "job_id": job_id,
            "format": format,
            "citation": bundle.render(format),
            "rid": bundle.rid,
            "program": bundle.program,
            "blast_version": bundle.blast_version,
            "database": bundle.database,
            "database_snapshot": bundle.database_snapshot,
            "search_space": bundle.search_space,
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_citation failed: %s", type(exc).__name__)
        raise HTTPException(
            503,
            {
                "code": "citation_unavailable",
                "message": f"Could not build citation: {type(exc).__name__}",
            },
        ) from exc


@router.get("/jobs/{job_id}/events")
def blast_job_events(
    job_id: str = Path(...),
    limit: int = Query(default=200, ge=1, le=500),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api.services.blast.events import canonical_job_events
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        _assert_job_owner(state.owner_oid, caller)
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


# Hard cap on the original query FASTA the Edit search rehydration endpoint
# will return. Mirrors the BLAST submit dialog's practical limit: anything
# larger almost certainly belongs in the query_file field instead, and
# round-tripping multi-MiB FASTA through the SPA sessionStorage hits browser
# quotas.
_QUERY_EDIT_MAX_BYTES = 5 * 1024 * 1024


@router.get("/jobs/{job_id}/query")
def blast_job_query(
    job_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the original FASTA submitted with this job.

    The dashboard strips ``query_data`` from the persisted payload after
    uploading it to the workload Storage account (keeps the JobState row
    small). The Edit search button needs the original text to rehydrate
    the form, so this route streams the blob back through the api sidecar
    with a hard 5 MiB cap. No SAS token is ever issued to the browser.
    """
    from azure.core.exceptions import ResourceNotFoundError

    from api.services import get_credential
    from api.services.state_repo import get_state_repo
    from api.services.storage.blob_io import read_metadata_blob_bytes
    from api.services.storage.data import _blob_service

    try:
        repo = get_state_repo()
        state = repo.get(job_id)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning(
            "blast_job_query state lookup failed job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )
        raise HTTPException(
            503,
            {"code": "query_fetch_unavailable", "message": type(exc).__name__},
        ) from exc
    if state is None:
        raise HTTPException(404, "job not found")
    _assert_job_owner(state.owner_oid, caller)
    payload = state.payload if isinstance(state.payload, dict) else {}
    blob_path = _queries_blob_path(
        _payload_value(payload, "query_file", "query_blob_url")
    )
    if not blob_path:
        raise HTTPException(
            404,
            {
                "code": "query_not_persisted",
                "message": "no query file was recorded for this job",
            },
        )
    # Defensive guard: even though `query_file` is populated by our own
    # submit pipeline, a corrupted Table row could carry "../" or an
    # absolute path. Reject before reaching the Storage SDK.
    from api.services.storage.blob_paths import _validate_blob_path

    try:
        _validate_blob_path(blob_path)
    except ValueError as exc:
        LOGGER.warning(
            "blast_job_query rejected unsafe blob path job_id=%s: %s",
            job_id,
            exc,
        )
        raise HTTPException(
            422,
            {
                "code": "invalid_query_path",
                "message": "recorded query path is not safe to read",
            },
        ) from exc
    storage_account = state.storage_account or str(
        _payload_value(payload, "storage_account") or ""
    )
    if not storage_account:
        raise HTTPException(
            404,
            {
                "code": "query_not_persisted",
                "message": "no storage account was recorded for this job",
            },
        )
    try:
        blob_client = _blob_service(get_credential(), storage_account).get_blob_client(
            "queries", blob_path
        )
        raw = read_metadata_blob_bytes(
            blob_client,
            max_bytes=_QUERY_EDIT_MAX_BYTES,
            label="query.fa",
        )
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404,
            {
                "code": "query_blob_missing",
                "message": "the original query blob is no longer in storage",
            },
        ) from exc
    except ValueError as exc:
        # ``read_metadata_blob_bytes`` raises ValueError when the blob
        # exceeds ``max_bytes``. Surface as 413 so the SPA can degrade
        # cleanly (toast + open the form without the original FASTA).
        raise HTTPException(
            413,
            {
                "code": "query_too_large_for_edit",
                "message": (
                    f"query exceeds the {_QUERY_EDIT_MAX_BYTES}-byte cap; "
                    "cannot rehydrate the Edit search form"
                ),
                "max_bytes": _QUERY_EDIT_MAX_BYTES,
            },
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning(
            "blast_job_query blob fetch failed job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )
        raise HTTPException(
            503,
            {"code": "query_fetch_unavailable", "message": type(exc).__name__},
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            422,
            {
                "code": "query_not_utf8",
                "message": "stored query is not valid UTF-8",
            },
        ) from exc
    return {
        "job_id": job_id,
        "query_text": text,
        "size_bytes": len(raw),
        "max_bytes": _QUERY_EDIT_MAX_BYTES,
    }


@router.get("/jobs/{job_id}/queue")
def blast_job_queue(
    job_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api.services.blast.queue import queue_snapshot
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        _assert_job_owner(state.owner_oid, caller)
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
        repo.update(job_id, status="deleted", phase="deleted")
        _reset_external_jobs_cache()
        return {"job_id": job_id, "status": "deleted"}
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_delete failed: %s", exc)
        return {"job_id": job_id, "status": "deleted"}

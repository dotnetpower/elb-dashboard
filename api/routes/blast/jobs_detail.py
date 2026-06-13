"""/api/blast single-job read routes (execution steps, citation, events, query, queue).

Responsibility: Owner-scoped, read-only `/api/blast/jobs/{job_id}/...` detail routes that
project persisted job state without mutating it.
Edit boundaries: Keep HTTP validation and response shaping here; reusable domain logic stays
in `api/services/blast/*` and the shared helpers in `api/routes/_blast_shared.py`. The job
listing, `/jobs/{job_id}` projection, and lifecycle (cancel/delete) routes live in the sibling
`jobs.py` / `jobs_lifecycle.py` modules; this router is included onto `jobs.router`.
Key entry points: `blast_job_execution_steps`, `blast_job_citation`, `blast_job_events`,
`blast_job_query`, `blast_job_queue`.
Risky contracts: Every route enforces `require_caller` + `_assert_job_owner`. Never issue a
browser SAS token; `blast_job_query` streams the original FASTA through the api sidecar with a
hard byte cap.
Validation: `uv run pytest -q api/tests/test_blast_jobs_routes.py
api/tests/test_blast_results_routes.py api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _assert_job_owner,
    _payload_value,
    _queries_blob_path,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


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
    external_payload = (
        payload.get("external") if isinstance(payload.get("external"), dict) else None
    )
    blob_path = _queries_blob_path(
        _payload_value(payload, "query_file", "query_blob_url")
    )
    if not blob_path and external_payload is not None:
        # External (OpenAPI) jobs carry no query field on the job row: the
        # sibling elastic-blast-azure plane uploads the inline FASTA to
        # ``queries/<job_id>.fa`` and records nothing back. Reconstruct that
        # convention so Edit search can rehydrate the original query the same
        # way it does for dashboard-submitted jobs. Use the sibling's own job
        # id from the external payload (the route ``job_id`` matches it for
        # synced rows, but the payload value is authoritative).
        openapi_id = str(external_payload.get("job_id") or job_id).strip()
        if openapi_id:
            blob_path = f"{openapi_id}.fa"
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
    if not storage_account and external_payload is not None:
        # External jobs never populate infrastructure.storage_account but carry
        # the BLAST database as a full blob URL. Recover the account behind the
        # trusted-account gate so the MI Storage token is never sent to an
        # attacker-influenced foreign account (same gate the projection uses).
        from api.services.blast.db_metadata import extract_trusted_storage_account

        storage_account = extract_trusted_storage_account(
            str(getattr(state, "db", "") or "")
        ) or extract_trusted_storage_account(str(external_payload.get("db") or ""))
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

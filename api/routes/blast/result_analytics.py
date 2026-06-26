"""BLAST result analytics routes.

Responsibility: BLAST result analytics routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `blast_job_results_alignments`, `blast_job_results_taxonomy`,
`_empty_alignments_payload`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _ensure_job_read_allowed,
    _maybe_open_local_storage_access,
    _resolve_job_storage_account,
)
from api.routes.blast.result_helpers import (
    default_alignments_request,
    default_taxonomy_request,
    enqueue_result_artifact_backfill,
    read_ready_result_artifact,
    validate_result_blob_for_job,
)
from api.services.blast.result_analytics import (
    RESULTS_ALIGNMENTS_MAX_BYTES,
    RESULTS_ALIGNMENTS_MAX_HITS,
    RESULTS_DEFAULT_PAGE_SIZE,
    RESULTS_MAX_FILES,
    annotate_result_hit,
    enrich_taxonomy_with_lineage,
    list_parseable_result_blobs,
    result_hit_matches_filters,
    result_hit_rank_aggregates,
    result_hit_sort_key,
    rollup_subject_aggregates,
    rollup_taxonomy,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/jobs/{job_id}/results/alignments")
def blast_job_results_alignments(
    job_id: str = Path(..., min_length=1, max_length=128),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    storage_account: str = Query(...),
    blob_name: str = Query(default=""),
    max_alignments: int = Query(default=50, ge=1, le=500),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int | None = Query(default=None, ge=1, le=500),
    query_id: str = Query(default=""),
    subject_id: str = Query(default=""),
    organism: str = Query(default=""),
    min_identity: float = Query(default=0.0, ge=0.0, le=100.0),
    min_bitscore: float = Query(default=0.0, ge=0.0),
    max_evalue: float = Query(default=10.0, ge=0.0),
    min_query_cover: float = Query(default=0.0, ge=0.0, le=100.0),
    sort_by: str = Query(
        default="relevance", pattern=r"^(relevance|evalue|bitscore|pident|qcovs|length)$"
    ),
    sort_dir: str = Query(default="asc", pattern=r"^(asc|desc)$"),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return parsed alignments from result files, optionally filtered."""
    _ensure_job_read_allowed(job_id, caller)
    storage_account = _resolve_job_storage_account(job_id, storage_account)
    if default_alignments_request(
        blob_name=blob_name,
        max_alignments=max_alignments,
        page=page,
        page_size=page_size,
        query_id=query_id,
        subject_id=subject_id,
        organism=organism,
        min_identity=min_identity,
        min_bitscore=min_bitscore,
        max_evalue=max_evalue,
        min_query_cover=min_query_cover,
        sort_by=sort_by,
        sort_dir=sort_dir,
    ):
        artifact = read_ready_result_artifact(job_id, "result_alignments")
        if artifact is not None:
            return artifact
        enqueue_result_artifact_backfill(job_id, "result_alignments")
    from api.services import get_credential
    from api.services.blast.results_parser import parse_blast_result_content
    from api.services.storage.data import read_result_blob_text

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        subscription_id,
        resource_group,
        storage_account,
        context="blast_job_results_alignments",
    )

    target_blob = blob_name.strip()
    result_blobs: list[dict[str, Any]] = []
    try:
        if not target_blob:
            result_blobs = list_parseable_result_blobs(storage_account, job_id)
            if not result_blobs:
                return _empty_alignments_payload(
                    job_id=job_id,
                    target_blob="",
                    page=page,
                    page_size=page_size or max_alignments,
                    degraded_reason="no_result_files",
                    message="No result files",
                )
        else:
            validate_result_blob_for_job(target_blob, job_id)
            result_blobs = [{"name": target_blob}]
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("results alignments: list failed: %s", type(exc).__name__)
        return _empty_alignments_payload(
            job_id=job_id,
            target_blob=target_blob,
            page=page,
            page_size=page_size or max_alignments,
            degraded_reason="storage_unreachable",
            message="Result storage is unreachable from the API.",
        )

    all_hits: list[dict[str, Any]] = []
    parsed_files = 0
    read_failures = 0
    hit_limit_reached = False
    content_truncated = False
    blob_names: list[str] = []
    for blob_info in result_blobs[:RESULTS_MAX_FILES]:
        if len(all_hits) >= RESULTS_ALIGNMENTS_MAX_HITS:
            hit_limit_reached = True
            break
        blob_path = str(blob_info.get("name") or "")
        if not blob_path:
            continue
        try:
            content = read_result_blob_text(
                cred,
                storage_account,
                "results",
                blob_path,
                max_bytes=RESULTS_ALIGNMENTS_MAX_BYTES,
            )
            parsed_hits = parse_blast_result_content(content)
            if len(content) >= RESULTS_ALIGNMENTS_MAX_BYTES - 4:
                # The read filled the byte budget, so the BLAST output is
                # larger than the cap and parse_blast_result_content sees a
                # truncated tail (parse_blast_xml returns the hits before the
                # cut). Flag the result partial so the UI says so honestly.
                content_truncated = True
            remaining_hit_slots = RESULTS_ALIGNMENTS_MAX_HITS - len(all_hits)
            all_hits.extend(
                annotate_result_hit(hit, blob_path) for hit in parsed_hits[:remaining_hit_slots]
            )
            if len(parsed_hits) > remaining_hit_slots:
                hit_limit_reached = True
            parsed_files += 1
            blob_names.append(blob_path)
            if hit_limit_reached:
                break
        except Exception as exc:
            read_failures += 1
            LOGGER.warning(
                "results alignments: failed to parse %s: %s", blob_path, type(exc).__name__
            )

    if parsed_files == 0 and read_failures > 0:
        return {
            **_empty_alignments_payload(
                job_id=job_id,
                target_blob=target_blob,
                page=page,
                page_size=page_size or max_alignments,
                degraded_reason="all_reads_failed",
                message=f"Failed to read any of {read_failures} result file(s).",
            ),
            "blob_names": blob_names,
            "total_files": len(result_blobs),
            "read_failures": read_failures,
            "truncated": len(result_blobs) > RESULTS_MAX_FILES,
            "hit_limit_reached": False,
        }

    query_ids = sorted({str(h.get("qseqid", "")) for h in all_hits if h.get("qseqid")})

    filtered: list[dict[str, Any]] = []
    qid_filter = query_id.strip()
    subject_filter = subject_id.strip()
    organism_filter = organism.strip()
    for hit in all_hits:
        if result_hit_matches_filters(
            hit,
            query_id=qid_filter,
            subject_id=subject_filter,
            organism=organism_filter,
            min_identity=min_identity,
            min_bitscore=min_bitscore,
            max_evalue=max_evalue,
            min_query_cover=min_query_cover,
        ):
            filtered.append(hit)

    rank_aggregates = result_hit_rank_aggregates(filtered) if sort_by == "relevance" else None
    filtered.sort(key=lambda hit: result_hit_sort_key(hit, sort_by, sort_dir, rank_aggregates))
    effective_page_size = page_size or max_alignments or RESULTS_DEFAULT_PAGE_SIZE
    start = (page - 1) * effective_page_size
    end = start + effective_page_size
    page_hits = filtered[start:end]
    page_count = (len(filtered) + effective_page_size - 1) // effective_page_size

    return {
        "job_id": job_id,
        "blob_name": blob_names[0] if len(blob_names) == 1 else target_blob,
        "blob_names": blob_names,
        "alignments": page_hits,
        "total_hits": len(all_hits),
        "filtered_hits": len(filtered),
        "returned": len(page_hits),
        "query_ids": query_ids[:200],
        "subject_aggregates": rollup_subject_aggregates(filtered),
        "page": page,
        "page_size": effective_page_size,
        "pages": page_count,
        "files_parsed": parsed_files,
        "total_files": len(result_blobs),
        "read_failures": read_failures,
        "truncated": (
            len(result_blobs) > RESULTS_MAX_FILES or hit_limit_reached or content_truncated
        ),
        "hit_limit_reached": hit_limit_reached,
        "filters": {
            "query_id": qid_filter or None,
            "subject_id": subject_filter or None,
            "organism": organism_filter or None,
            "min_identity": min_identity,
            "min_bitscore": min_bitscore,
            "max_evalue": max_evalue,
            "min_query_cover": min_query_cover,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
        },
    }


@router.get("/jobs/{job_id}/results/taxonomy")
def blast_job_results_taxonomy(
    job_id: str = Path(..., min_length=1, max_length=128),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    storage_account: str = Query(...),
    blob_name: str = Query(default=""),
    query_id: str = Query(default=""),
    subject_id: str = Query(default=""),
    organism: str = Query(default=""),
    min_identity: float = Query(default=0.0, ge=0.0, le=100.0),
    min_bitscore: float = Query(default=0.0, ge=0.0),
    max_evalue: float = Query(default=10.0, ge=0.0),
    min_query_cover: float = Query(default=0.0, ge=0.0, le=100.0),
    include_lineage: bool = Query(default=False),
    lineage_taxid_limit: int = Query(default=20, ge=1, le=100),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Server-side organism rollup of the BLAST hits."""
    _ensure_job_read_allowed(job_id, caller)
    storage_account = _resolve_job_storage_account(job_id, storage_account)
    if default_taxonomy_request(
        blob_name=blob_name,
        query_id=query_id,
        subject_id=subject_id,
        organism=organism,
        min_identity=min_identity,
        min_bitscore=min_bitscore,
        max_evalue=max_evalue,
        min_query_cover=min_query_cover,
        include_lineage=include_lineage,
    ):
        artifact = read_ready_result_artifact(job_id, "result_taxonomy")
        if artifact is not None:
            return artifact
        enqueue_result_artifact_backfill(job_id, "result_taxonomy")
    from api.services import get_credential
    from api.services.blast.results_parser import parse_blast_result_content
    from api.services.storage.data import read_result_blob_text

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        subscription_id,
        resource_group,
        storage_account,
        context="blast_job_results_taxonomy",
    )

    target_blob = blob_name.strip()
    result_blobs: list[dict[str, Any]] = []
    if not target_blob:
        try:
            result_blobs = list_parseable_result_blobs(storage_account, job_id)
        except Exception as exc:
            LOGGER.warning("results taxonomy: list failed: %s", type(exc).__name__)
            return _empty_taxonomy_payload(
                job_id=job_id,
                degraded_reason="storage_unreachable",
            )
        if not result_blobs:
            return {
                "job_id": job_id,
                "organisms": [],
                "message": "No result files",
                "total_hits": 0,
                "files_parsed": 0,
                "total_files": 0,
                "read_failures": 0,
            }
    else:
        validate_result_blob_for_job(target_blob, job_id)
        result_blobs = [{"name": target_blob}]

    all_hits: list[dict[str, Any]] = []
    parsed_files = 0
    read_failures = 0
    content_truncated = False
    for blob_info in result_blobs[:RESULTS_MAX_FILES]:
        if len(all_hits) >= RESULTS_ALIGNMENTS_MAX_HITS:
            break
        blob_path = str(blob_info.get("name") or "")
        if not blob_path:
            continue
        try:
            content = read_result_blob_text(
                cred,
                storage_account,
                "results",
                blob_path,
                max_bytes=RESULTS_ALIGNMENTS_MAX_BYTES,
            )
            parsed_hits = parse_blast_result_content(content)
            if len(content) >= RESULTS_ALIGNMENTS_MAX_BYTES - 4:
                content_truncated = True
            remaining = RESULTS_ALIGNMENTS_MAX_HITS - len(all_hits)
            all_hits.extend(annotate_result_hit(hit, blob_path) for hit in parsed_hits[:remaining])
            parsed_files += 1
        except Exception as exc:
            read_failures += 1
            LOGGER.warning(
                "results taxonomy: failed to parse %s: %s",
                blob_path,
                type(exc).__name__,
            )

    if parsed_files == 0 and read_failures > 0:
        return _empty_taxonomy_payload(
            job_id=job_id,
            degraded_reason="all_reads_failed",
            message=f"Failed to read any of {read_failures} result file(s).",
            total_files=len(result_blobs),
            read_failures=read_failures,
        )

    qid_filter = query_id.strip()
    subject_filter = subject_id.strip()
    organism_filter = organism.strip()
    filtered = [
        hit
        for hit in all_hits
        if result_hit_matches_filters(
            hit,
            query_id=qid_filter,
            subject_id=subject_filter,
            organism=organism_filter,
            min_identity=min_identity,
            min_bitscore=min_bitscore,
            max_evalue=max_evalue,
            min_query_cover=min_query_cover,
        )
    ]

    organisms = rollup_taxonomy(filtered)
    lineage_meta = {
        "requested": include_lineage,
        "looked_up": 0,
        "name_resolved": 0,
        "failed": 0,
    }
    if include_lineage and organisms:
        organisms, lineage_meta = enrich_taxonomy_with_lineage(
            organisms, taxid_limit=lineage_taxid_limit
        )

    return {
        "job_id": job_id,
        "organisms": organisms,
        "total_hits": len(all_hits),
        "filtered_hits": len(filtered),
        "files_parsed": parsed_files,
        "total_files": len(result_blobs),
        "read_failures": read_failures,
        "truncated": len(result_blobs) > RESULTS_MAX_FILES or content_truncated,
        "lineage": lineage_meta,
    }


def _empty_alignments_payload(
    *,
    job_id: str,
    target_blob: str,
    page: int,
    page_size: int,
    degraded_reason: str,
    message: str,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "blob_name": target_blob,
        "blob_names": [],
        "alignments": [],
        "degraded": True,
        "degraded_reason": degraded_reason,
        "message": message,
        "total_hits": 0,
        "filtered_hits": 0,
        "returned": 0,
        "query_ids": [],
        "page": page,
        "page_size": page_size,
        "pages": 0,
        "files_parsed": 0,
        "total_files": 0,
        "read_failures": 0,
    }


def _empty_taxonomy_payload(
    *,
    job_id: str,
    degraded_reason: str,
    message: str | None = None,
    total_files: int = 0,
    read_failures: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "organisms": [],
        "degraded": True,
        "degraded_reason": degraded_reason,
        "total_hits": 0,
        "files_parsed": 0,
        "total_files": total_files,
        "read_failures": read_failures,
    }
    if message:
        payload["message"] = message
    return payload


__all__ = ["blast_job_results_alignments", "blast_job_results_taxonomy", "router"]

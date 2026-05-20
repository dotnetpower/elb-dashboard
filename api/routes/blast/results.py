"""/api/blast result file, analytics, and export routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _blob_not_found,
    _ensure_job_read_allowed,
    _external_result_files,
    _job_payload_for_file_preview,
    _job_query_blob_path,
    _maybe_open_local_storage_access,
    _queries_blob_path,
)
from api.services.blast_result_analytics import (
    RESULTS_AGGREGATE_MAX_BYTES,
    RESULTS_ALIGNMENTS_MAX_BYTES,
    RESULTS_ALIGNMENTS_MAX_HITS,
    RESULTS_DEFAULT_PAGE_SIZE,
    RESULTS_EXPORT_MAX_BYTES,
    RESULTS_MAX_FILES,
    InvalidResultBlobName,
    annotate_result_hit,
    enrich_taxonomy_with_lineage,
    list_parseable_result_blobs,
    result_hit_matches_filters,
    result_hit_rank_aggregates,
    result_hit_sort_key,
    rollup_subject_aggregates,
    rollup_taxonomy,
    validate_result_blob_name,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _validate_result_blob_name(blob_name: str, job_id: str) -> None:
    try:
        validate_result_blob_name(blob_name, job_id)
    except InvalidResultBlobName as exc:
        detail: dict[str, str] = {"code": exc.code}
        message = str(exc)
        if message:
            detail["message"] = message
        raise HTTPException(400, detail=detail) from exc


# --- Result download / aggregate / export ---
@router.get("/jobs/{job_id}/file")
def blast_job_file(
    job_id: str = Path(...),
    name: str = Query(...),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    storage_account: str = Query(...),
    max_bytes: int = Query(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Read a job file from storage (streamed through the api sidecar)."""
    try:
        from api.services import get_credential
        from api.services.storage_data import read_blob_text

        cred = get_credential()
        _maybe_open_local_storage_access(
            cred,
            subscription_id,
            resource_group,
            storage_account,
            context="blast_job_file",
        )
        name_raw = str(name).strip()
        basename = name_raw.rsplit("/", 1)[-1]
        requested_query_blob = _queries_blob_path(name)
        payload_query_blob = ""
        query_candidates: list[str] = []
        if name_raw in {"input.fa", "query.fa"}:
            payload_query_blob = _job_query_blob_path(job_id, caller)
            requested_query_blob = payload_query_blob or f"{job_id}/{name}"
            query_candidates = [
                requested_query_blob,
                f"uploads/{job_id}/query.fa",
                f"{job_id}/query.fa",
            ]
        explicit_query_ref = name_raw.startswith("queries/") or (
            name_raw.startswith(("az://", "http://", "https://")) and bool(requested_query_blob)
        )
        if requested_query_blob and (explicit_query_ref or name_raw in {"input.fa", "query.fa"}):
            if explicit_query_ref:
                payload_query_blob = _job_query_blob_path(job_id, caller)
                if (
                    requested_query_blob != payload_query_blob
                    and not requested_query_blob.startswith((f"{job_id}/", f"uploads/{job_id}/"))
                ):
                    raise HTTPException(403, "query blob is outside this job")
            container = "queries"
            blob_candidates = query_candidates or [requested_query_blob]
        elif basename == "elastic-blast.ini":
            container = "queries"
            requested_config_blob = _queries_blob_path(name_raw)
            explicit_config_ref = name_raw.startswith("queries/") or (
                name_raw.startswith(("az://", "http://", "https://"))
                and bool(requested_config_blob)
            )
            if explicit_config_ref and not requested_config_blob.startswith(
                (f"{job_id}/", f"uploads/{job_id}/")
            ):
                raise HTTPException(403, "config blob is outside this job")
            blob_candidates = [
                requested_config_blob if explicit_config_ref else "",
                f"{job_id}/elastic-blast.ini",
                f"uploads/{job_id}/elastic-blast.ini",
            ]
        else:
            container = "results"
            blob_candidates = [f"{job_id}/{name}" if not name.startswith(job_id) else name]
        content = ""
        selected_blob = ""
        last_not_found: BaseException | None = None
        seen: set[str] = set()
        for candidate in blob_candidates:
            blob_path = str(candidate or "").strip()
            if not blob_path or blob_path in seen:
                continue
            seen.add(blob_path)
            try:
                content = read_blob_text(
                    cred,
                    storage_account,
                    container=container,
                    blob_path=blob_path,
                    max_bytes=max_bytes,
                )
                selected_blob = blob_path
                break
            except Exception as exc:
                if not _blob_not_found(exc):
                    raise
                last_not_found = exc
        if not selected_blob:
            if basename == "elastic-blast.ini":
                payload = _job_payload_for_file_preview(job_id, caller)
                if payload:
                    from api.routes import blast as blast_package

                    content = blast_package._config_preview_from_payload(
                        job_id=job_id,
                        storage_account=storage_account,
                        payload=payload,
                    )
                    selected_blob = f"{job_id}/elastic-blast.ini"
            if not selected_blob:
                raise last_not_found or FileNotFoundError(name_raw)
        return {
            "job_id": job_id,
            "name": name,
            "content": content,
            "truncated": len(content) >= max_bytes,
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_file failed: %s", type(exc).__name__)
        from api.services import get_credential as _get_cred
        from api.services.storage_data import classify_storage_failure

        info = classify_storage_failure(_get_cred(), subscription_id, "", storage_account, exc)
        raise HTTPException(
            404 if info["degraded_reason"] == "not_found" else 503,
            detail={"code": info["degraded_reason"], "message": info["message"]},
        ) from exc


@router.get("/jobs/{job_id}/results")
def blast_job_results(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """List result blobs for a BLAST job from storage."""
    _ensure_job_read_allowed(job_id, caller)
    local_failure: dict[str, Any] | None = None
    try:
        if storage_account:
            from api.services import get_credential
            from api.services.storage_data import list_result_blobs

            cred = get_credential()
            _maybe_open_local_storage_access(
                cred,
                subscription_id,
                resource_group,
                storage_account,
                context="blast_job_results",
            )
            files = list_result_blobs(cred, storage_account, container="results", prefix=job_id)
            return {"job_id": job_id, "files": files, "results": files}
    except Exception as exc:
        LOGGER.warning("blast_job_results failed: %s", type(exc).__name__)
        from api.services import get_credential as _get_cred
        from api.services.storage_data import classify_storage_failure

        local_failure = classify_storage_failure(
            _get_cred(), subscription_id, resource_group, storage_account, exc
        )

    try:
        from api.services import external_blast

        files = _external_result_files(external_blast.get_job(job_id))
        if files:
            return {"job_id": job_id, "files": files, "results": files, "source": "external"}
    except Exception as exc:
        LOGGER.info("external blast result list unavailable: %s", type(exc).__name__)

    if local_failure:
        return {"job_id": job_id, "files": [], "results": [], **local_failure}
    return {"job_id": job_id, "files": [], "results": []}


@router.get("/jobs/{job_id}/results/aggregate")
def blast_job_results_aggregate(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    storage_account: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Parse result blobs and return aggregate statistics for analytics."""
    _ensure_job_read_allowed(job_id, caller)
    from api.services import get_credential
    from api.services.blast_results_parser import aggregate_blast_hits, parse_blast_result_content
    from api.services.storage_data import read_result_blob_text

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        subscription_id,
        resource_group,
        storage_account,
        context="blast_job_results_aggregate",
    )

    try:
        result_blobs = list_parseable_result_blobs(storage_account, job_id)
    except Exception as exc:
        LOGGER.warning("results aggregate: list_result_blobs failed: %s", type(exc).__name__)
        return {
            "job_id": job_id,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "storage_unreachable",
            "stats": None,
        }

    if not result_blobs:
        return {
            "job_id": job_id,
            "status": "no_results",
            "message": "No parseable BLAST result files found for this job.",
            "stats": None,
            "files_parsed": 0,
            "total_files": 0,
        }

    all_hits: list[dict[str, Any]] = []
    parsed_files = 0
    read_failures = 0
    for blob_info in result_blobs[:RESULTS_MAX_FILES]:
        try:
            content = read_result_blob_text(
                cred,
                storage_account,
                "results",
                blob_info["name"],
                max_bytes=RESULTS_AGGREGATE_MAX_BYTES,
            )
            all_hits.extend(parse_blast_result_content(content))
            parsed_files += 1
        except Exception as exc:
            read_failures += 1
            LOGGER.warning(
                "results aggregate: failed to parse %s: %s", blob_info["name"], type(exc).__name__
            )

    # If every blob read failed, surface that as a storage degradation rather
    # than "no hits" — a researcher staring at an empty analytics card needs
    # to know it's an infra issue, not a biology one.
    if parsed_files == 0 and read_failures > 0:
        return {
            "job_id": job_id,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "all_reads_failed",
            "message": (
                f"Failed to read any of {read_failures} result file(s). "
                "Storage may be unreachable or RBAC missing."
            ),
            "stats": None,
            "files_parsed": 0,
            "total_files": len(result_blobs),
            "read_failures": read_failures,
        }

    if not all_hits:
        return {
            "job_id": job_id,
            "status": "no_hits",
            "message": "No BLAST hits found in result files.",
            "stats": aggregate_blast_hits([]),
            "files_parsed": parsed_files,
            "total_files": len(result_blobs),
            "read_failures": read_failures,
            "truncated": len(result_blobs) > RESULTS_MAX_FILES,
        }

    try:
        stats = aggregate_blast_hits(all_hits)
    except Exception as exc:
        # Defensive: aggregate_blast_hits is pure-Python and well-tested,
        # but if an unexpected hit shape sneaks through (e.g. NaN in evalue),
        # report it as degraded rather than 500.
        LOGGER.warning("results aggregate: stats failed: %s", type(exc).__name__)
        return {
            "job_id": job_id,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "aggregation_failed",
            "stats": None,
            "files_parsed": parsed_files,
            "total_files": len(result_blobs),
            "read_failures": read_failures,
        }
    return {
        "job_id": job_id,
        "status": "ok",
        "stats": stats,
        "files_parsed": parsed_files,
        "total_files": len(result_blobs),
        "read_failures": read_failures,
        "truncated": len(result_blobs) > RESULTS_MAX_FILES,
    }


@router.get("/jobs/{job_id}/results/alignments")
def blast_job_results_alignments(
    job_id: str = Path(...),
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
    """Return parsed alignments from result files, optionally filtered.

    With no `blob_name`, all parseable result blobs under the job prefix are
    read up to the safety file cap. That keeps the Hits tab honest for split
    jobs: it reports the page being viewed rather than showing the first blob
    as if it represented the whole run.
    """
    _ensure_job_read_allowed(job_id, caller)
    from api.services import get_credential
    from api.services.blast_results_parser import parse_blast_result_content
    from api.services.storage_data import read_result_blob_text

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
                return {
                    "job_id": job_id,
                    "blob_name": "",
                    "blob_names": [],
                    "alignments": [],
                    "degraded": True,
                    "degraded_reason": "no_result_files",
                    "message": "No result files",
                    "total_hits": 0,
                    "filtered_hits": 0,
                    "returned": 0,
                    "query_ids": [],
                    "page": page,
                    "page_size": page_size or max_alignments,
                    "pages": 0,
                    "files_parsed": 0,
                    "total_files": 0,
                    "read_failures": 0,
                }
        else:
            _validate_result_blob_name(target_blob, job_id)
            result_blobs = [{"name": target_blob}]
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("results alignments: list failed: %s", type(exc).__name__)
        return {
            "job_id": job_id,
            "blob_name": target_blob,
            "blob_names": [],
            "alignments": [],
            "degraded": True,
            "degraded_reason": "storage_unreachable",
            "message": "Result storage is unreachable from the API.",
            "total_hits": 0,
            "filtered_hits": 0,
            "returned": 0,
            "query_ids": [],
            "page": page,
            "page_size": page_size or max_alignments,
            "pages": 0,
            "files_parsed": 0,
            "total_files": 0,
            "read_failures": 0,
        }

    all_hits: list[dict[str, Any]] = []
    parsed_files = 0
    read_failures = 0
    hit_limit_reached = False
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
            "job_id": job_id,
            "blob_name": target_blob,
            "blob_names": blob_names,
            "alignments": [],
            "degraded": True,
            "degraded_reason": "all_reads_failed",
            "message": f"Failed to read any of {read_failures} result file(s).",
            "total_hits": 0,
            "filtered_hits": 0,
            "returned": 0,
            "query_ids": [],
            "page": page,
            "page_size": page_size or max_alignments,
            "pages": 0,
            "files_parsed": 0,
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
        "truncated": len(result_blobs) > RESULTS_MAX_FILES or hit_limit_reached,
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
    job_id: str = Path(...),
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
    """Server-side organism rollup of the BLAST hits.

    Mirrors the NCBI "Taxonomy → Organism" report shape:

    .. code-block:: json

        {
          "organisms": [
            {"organism": "Monkeypox virus", "taxid": "10244",
             "count": 99, "best_evalue": 0.0, "top_bitscore": 854.0},
            ...
          ],
          "total_hits": 100,
          "files_parsed": 3,
          ...
        }

    The same query filters as ``/results/alignments`` are honoured so a
    narrowing applied on the Descriptions tab carries over to Taxonomy.
    Page size does NOT apply — taxonomy rollup is always over the
    *filtered* (not paginated) hit set.
    """
    _ensure_job_read_allowed(job_id, caller)
    from api.services import get_credential
    from api.services.blast_results_parser import parse_blast_result_content
    from api.services.storage_data import read_result_blob_text

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
            return {
                "job_id": job_id,
                "organisms": [],
                "degraded": True,
                "degraded_reason": "storage_unreachable",
                "total_hits": 0,
                "files_parsed": 0,
                "total_files": 0,
                "read_failures": 0,
            }
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
        _validate_result_blob_name(target_blob, job_id)
        result_blobs = [{"name": target_blob}]

    all_hits: list[dict[str, Any]] = []
    parsed_files = 0
    read_failures = 0
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
        return {
            "job_id": job_id,
            "organisms": [],
            "degraded": True,
            "degraded_reason": "all_reads_failed",
            "message": f"Failed to read any of {read_failures} result file(s).",
            "total_hits": 0,
            "files_parsed": 0,
            "total_files": len(result_blobs),
            "read_failures": read_failures,
        }

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
    lineage_meta = {"requested": include_lineage, "looked_up": 0, "failed": 0}
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
        "truncated": len(result_blobs) > RESULTS_MAX_FILES,
        "lineage": lineage_meta,
    }


@router.get("/jobs/{job_id}/results/download")
def blast_job_results_download(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    storage_account: str = Query(...),
    blob_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> StreamingResponse:
    """Stream a single result blob through the api sidecar."""
    _ensure_job_read_allowed(job_id, caller)
    _validate_result_blob_name(blob_name, job_id)
    from api.services import get_credential
    from api.services.storage_data import (
        result_media_type,
        safe_download_filename,
        stream_blob_bytes,
    )

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        subscription_id,
        resource_group,
        storage_account,
        context="blast_job_results_download",
    )
    filename = safe_download_filename(blob_name)
    return StreamingResponse(
        stream_blob_bytes(cred, storage_account, "results", blob_name),
        media_type=result_media_type(filename),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/jobs/{job_id}/results/export")
def blast_job_results_export(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    storage_account: str = Query(...),
    format: str = Query(default="csv", pattern=r"^(csv|tsv|json)$"),
    caller: CallerIdentity = Depends(require_caller),
) -> StreamingResponse:
    """Export all parsed hits for a job as CSV / TSV / JSON.

    Researchers paste the CSV into Excel / R / Python for downstream
    analysis, so the column set matches BLAST `-outfmt 6` plus the extras
    captured from `# Fields:` headers when available.
    """
    _ensure_job_read_allowed(job_id, caller)
    import csv
    import io
    import json

    from api.services import get_credential
    from api.services.blast_results_parser import (
        EXPORT_DEFAULT_COLUMNS,
        EXPORT_EXTRA_COLUMNS,
        parse_blast_result_content,
    )
    from api.services.storage_data import read_result_blob_text

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        subscription_id,
        resource_group,
        storage_account,
        context="blast_job_results_export",
    )

    try:
        result_blobs = list_parseable_result_blobs(storage_account, job_id)
    except Exception as exc:
        LOGGER.warning("results export: list_result_blobs failed: %s", type(exc).__name__)
        raise HTTPException(
            503,
            detail={"code": "storage_unreachable", "message": "Could not list result blobs."},
        ) from exc

    all_hits: list[dict[str, Any]] = []
    read_failures = 0
    for blob_info in result_blobs[:RESULTS_MAX_FILES]:
        try:
            content = read_result_blob_text(
                cred,
                storage_account,
                "results",
                blob_info["name"],
                max_bytes=RESULTS_EXPORT_MAX_BYTES,
            )
            all_hits.extend(
                annotate_result_hit(hit, str(blob_info["name"]))
                for hit in parse_blast_result_content(content)
            )
        except Exception:
            read_failures += 1
            LOGGER.debug("results export: failed to parse blob", exc_info=True)

    # If we had blobs to read but every read failed, the export would otherwise
    # be a misleading header-only CSV. Fail loudly instead.
    if result_blobs and read_failures == len(result_blobs[:RESULTS_MAX_FILES]):
        raise HTTPException(
            503,
            detail={
                "code": "all_reads_failed",
                "message": f"Failed to read any of {read_failures} result file(s).",
            },
        )

    if format == "json":
        body = json.dumps({"job_id": job_id, "hits": all_hits, "total": len(all_hits)}, default=str)
        return StreamingResponse(
            iter([body.encode("utf-8")]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{job_id}_results.json"'},
        )

    # CSV / TSV. Include extra columns only when at least one hit has them so
    # the file does not get a bunch of blank trailing columns for vanilla
    # `-outfmt 6` output.
    delimiter = "\t" if format == "tsv" else ","
    extras_present = [col for col in EXPORT_EXTRA_COLUMNS if any(col in hit for hit in all_hits)]
    columns = list(EXPORT_DEFAULT_COLUMNS) + extras_present
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, delimiter=delimiter, extrasaction="ignore")
    writer.writeheader()
    for hit in all_hits:
        writer.writerow(hit)
    ext = "tsv" if format == "tsv" else "csv"
    mime = "text/tab-separated-values" if format == "tsv" else "text/csv"
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8")]),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{job_id}_results.{ext}"'},
    )


@router.get("/jobs/{job_id}/results/{file_id}")
def blast_job_result_file(
    job_id: str = Path(...),
    file_id: str = Path(..., min_length=1, max_length=512, pattern=r"^[A-Za-z0-9._-]+$"),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> StreamingResponse:
    """Stream one result file by file_id through the api sidecar.

    Local result file ids are deterministic URL-safe encodings of blob names.
    External OpenAPI jobs keep their sibling-generated ids such as
    `result-001`. The browser never receives a SAS URL in either path.
    """
    _ensure_job_read_allowed(job_id, caller)
    try:
        from api.services.storage_data import (
            decode_blob_file_id,
            result_media_type,
            safe_download_filename,
            stream_blob_bytes,
        )

        blob_path = decode_blob_file_id(file_id)
        if blob_path is not None:
            if blob_path != job_id and not blob_path.startswith(f"{job_id}/"):
                raise HTTPException(
                    400,
                    detail={
                        "code": "invalid_file_id",
                        "message": "file_id does not belong to this job",
                    },
                )
            if not storage_account:
                raise HTTPException(
                    400,
                    detail={
                        "code": "missing_storage_account",
                        "message": "storage_account is required for local result file downloads.",
                    },
                )
            from api.services import get_credential

            cred = get_credential()
            _maybe_open_local_storage_access(
                cred,
                subscription_id,
                resource_group,
                storage_account,
                context="blast_job_result_file",
            )
            filename = safe_download_filename(blob_path)
            return StreamingResponse(
                stream_blob_bytes(cred, storage_account, "results", blob_path),
                media_type=result_media_type(filename),
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid_file_id", "message": str(exc)}) from exc
    except Exception as exc:
        LOGGER.warning("local result stream failed: %s", type(exc).__name__)

    try:
        from api.services import external_blast

        downloaded = external_blast.stream_file(job_id, file_id)
        return StreamingResponse(
            downloaded.chunks,
            media_type=downloaded.media_type,
            headers={"Content-Disposition": f'attachment; filename="{downloaded.filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning(
            "external result stream failed job_id=%s file_id=%s: %s",
            job_id,
            file_id,
            type(exc).__name__,
        )
        raise HTTPException(
            503,
            detail={
                "code": "result_stream_unavailable",
                "message": (
                    "Result file could not be streamed from local storage or external OpenAPI."
                ),
            },
        ) from exc

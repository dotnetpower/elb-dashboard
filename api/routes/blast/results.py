"""BLAST result-file, manifest, aggregate, and export routes.

Responsibility: BLAST result-file, manifest, aggregate, and export routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `blast_job_file`, `blast_job_results`, `blast_job_results_aggregate`,
`blast_job_results_download`, `blast_job_results_export`, `blast_job_result_file`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

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
    _resolve_job_storage_account,
)
from api.routes.blast.result_helpers import (
    enqueue_result_artifact_backfill,
    read_ready_result_artifact,
    result_artifact_state,
    validate_result_blob_for_job,
)
from api.services.blast.result_analytics import (
    RESULTS_EXPORT_MAX_BYTES,
    RESULTS_MAX_FILES,
    annotate_result_hit,
    list_parseable_result_blobs,
    read_result_blob_texts_parallel,
)
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()


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
    storage_account = _resolve_job_storage_account(job_id, storage_account)
    try:
        from api.services import get_credential
        from api.services.storage.data import read_blob_text

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

                    try:
                        content = blast_package._config_preview_from_payload(
                            job_id=job_id,
                            storage_account=storage_account,
                            payload=payload,
                        )
                        selected_blob = f"{job_id}/elastic-blast.ini"
                    except ValueError as exc:
                        raise HTTPException(
                            422,
                            detail={
                                "code": "invalid_config_payload",
                                "message": sanitise(str(exc))[:500],
                            },
                        ) from exc
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
        from api.services.storage.data import classify_storage_failure

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
    storage_account = _resolve_job_storage_account(job_id, storage_account)
    artifact = read_ready_result_artifact(job_id, "result_manifest")
    if artifact is not None:
        return artifact
    local_failure: dict[str, Any] | None = None
    try:
        if storage_account:
            from api.services import get_credential
            from api.services.storage.data import list_result_blobs

            cred = get_credential()
            _maybe_open_local_storage_access(
                cred,
                subscription_id,
                resource_group,
                storage_account,
                context="blast_job_results",
            )
            files = list_result_blobs(cred, storage_account, container="results", prefix=job_id)
            from api.services.blast.result_manifest import build_result_manifest

            return {
                "job_id": job_id,
                "files": files,
                "results": files,
                "manifest": build_result_manifest(job_id=job_id, files=files),
            }
    except Exception as exc:
        LOGGER.warning("blast_job_results failed: %s", type(exc).__name__)
        from api.services import get_credential as _get_cred
        from api.services.storage.data import classify_storage_failure

        local_failure = classify_storage_failure(
            _get_cred(), subscription_id, resource_group, storage_account, exc
        )

    try:
        from api.services import external_blast

        files = _external_result_files(external_blast.get_job(job_id))
        if files:
            from api.services.blast.result_manifest import build_result_manifest

            return {
                "job_id": job_id,
                "files": files,
                "results": files,
                "source": "external",
                "manifest": build_result_manifest(
                    job_id=job_id,
                    files=files,
                    source="external",
                ),
            }
    except Exception as exc:
        LOGGER.info("external blast result list unavailable: %s", type(exc).__name__)

    if local_failure:
        from api.services.blast.result_manifest import build_result_manifest

        return {
            "job_id": job_id,
            "files": [],
            "results": [],
            "manifest": build_result_manifest(
                job_id=job_id,
                files=[],
                degraded_reason=str(local_failure.get("degraded_reason") or "degraded"),
            ),
            **local_failure,
        }
    from api.services.blast.result_manifest import build_result_manifest

    return {
        "job_id": job_id,
        "files": [],
        "results": [],
        "manifest": build_result_manifest(job_id=job_id, files=[]),
    }


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
    storage_account = _resolve_job_storage_account(job_id, storage_account)
    artifact = read_ready_result_artifact(job_id, "result_aggregate")
    if artifact is not None:
        return artifact
    enqueue_result_artifact_backfill(job_id, "result_aggregate")
    from api.services import get_credential

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

    artifact_state = result_artifact_state(job_id, "result_aggregate")
    if not result_blobs:
        return {
            "job_id": job_id,
            "status": "no_results",
            "message": "No parseable BLAST result files found for this job.",
            "stats": None,
            "files_parsed": 0,
            "total_files": 0,
            **artifact_state,
            "source": "live_parse",
        }

    try:
        from api.services.blast.result_artifacts import build_result_aggregate_payload

        payload = build_result_aggregate_payload(job_id, storage_account)
    except Exception as exc:
        LOGGER.warning("results aggregate: stats failed: %s", type(exc).__name__)
        return {
            "job_id": job_id,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "aggregation_failed",
            "stats": None,
            "files_parsed": 0,
            "total_files": len(result_blobs),
            "read_failures": 0,
            **artifact_state,
            "source": "live_parse",
        }
    return {**payload, **artifact_state, "source": "live_parse"}


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
    storage_account = _resolve_job_storage_account(job_id, storage_account)
    validate_result_blob_for_job(blob_name, job_id)
    from api.services import get_credential
    from api.services.storage.data import (
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
    format: str = Query(
        default="csv",
        pattern=(
            r"^(csv|tsv|json|hit-table-text|hit-table-csv|json-seqalign|xml|text"
            r"|ncbi-hit-table-text|ncbi-hit-table-csv|ncbi-report-text)$"
        ),
    ),
    caller: CallerIdentity = Depends(require_caller),
) -> StreamingResponse:
    """Export all parsed hits or captured raw reports for a job.

    Hit-table exports are generated from parsed BLAST XML / tabular output.
    Raw `xml` / `text` exports stream the report format captured at submit
    time; the route does not try to synthesize pairwise text from XML. The
    `ncbi-*` formats synthesize an NCBI Web BLAST-style "Descriptions" table
    (per-subject aggregation) from the same parsed hits — see
    `api.services.blast.ncbi_report`.
    """
    _ensure_job_read_allowed(job_id, caller)
    storage_account = _resolve_job_storage_account(job_id, storage_account)
    import csv
    import io
    import json

    from api.services import get_credential
    from api.services.blast.results_parser import (
        EXPORT_DEFAULT_COLUMNS,
        EXPORT_EXTRA_COLUMNS,
        parse_blast_result_content,
    )

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

    export_format = _normalise_results_export_format(format)
    if export_format in {"xml", "text"}:
        return _export_raw_result_text(
            job_id=job_id,
            export_format=export_format,
            result_blobs=result_blobs,
            cred=cred,
            storage_account=storage_account,
        )

    all_hits: list[dict[str, Any]] = []
    read_failures = 0
    reads = read_result_blob_texts_parallel(
        storage_account,
        result_blobs[:RESULTS_MAX_FILES],
        max_bytes=RESULTS_EXPORT_MAX_BYTES,
    )
    for blob_path, content, read_exc in reads:
        if not blob_path:
            continue
        try:
            if read_exc is not None:
                raise read_exc
            all_hits.extend(
                annotate_result_hit(hit, blob_path)
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

    if export_format in {"ncbi-hit-table-text", "ncbi-hit-table-csv", "ncbi-report-text"}:
        return _export_ncbi_report(
            job_id=job_id,
            export_format=export_format,
            all_hits=all_hits,
        )

    if export_format in {"json", "json-seqalign"}:
        key = "seq_alignments" if export_format == "json-seqalign" else "hits"
        body = json.dumps(
            {"job_id": job_id, "format": export_format, key: all_hits, "total": len(all_hits)},
            default=str,
        )
        filename = f"{job_id}_{'seqalign' if export_format == 'json-seqalign' else 'results'}.json"
        return StreamingResponse(
            iter([body.encode("utf-8")]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # CSV / TSV. Include extra columns only when at least one hit has them so
    # the file does not get a bunch of blank trailing columns for vanilla
    # `-outfmt 6` output.
    delimiter = "\t" if export_format == "tsv" else ","
    extras_present = [col for col in EXPORT_EXTRA_COLUMNS if any(col in hit for hit in all_hits)]
    columns = list(EXPORT_DEFAULT_COLUMNS) + extras_present
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, delimiter=delimiter, extrasaction="ignore")
    writer.writeheader()
    for hit in all_hits:
        writer.writerow(hit)
    ext = "tsv" if export_format == "tsv" else "csv"
    mime = "text/tab-separated-values" if export_format == "tsv" else "text/csv"
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8")]),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{job_id}_results.{ext}"'},
    )


def _normalise_results_export_format(format_value: str) -> str:
    aliases = {
        "hit-table-text": "tsv",
        "hit-table-csv": "csv",
    }
    return aliases.get(format_value, format_value)


def _ncbi_report_header_fields(job_id: str) -> dict[str, Any]:
    """Best-effort job metadata for the NCBI report header.

    Reads the persisted job state / provenance bundle. Never raises — a missing
    state row degrades to neutral header values rather than failing the export.
    """
    fields: dict[str, Any] = {
        "program": "blast",
        "database": "",
        "job_title": None,
        "blast_version": None,
        "database_snapshot": None,
        "compatibility_note": None,
    }
    try:
        from api.services.blast.provenance import build_blast_provenance
        from api.services.state_repo import get_state_repo

        state = get_state_repo().get(job_id)
        if state is None:
            return fields
        raw_payload = getattr(state, "payload", None)
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        provenance = payload.get("provenance")
        if not isinstance(provenance, dict):
            provenance = build_blast_provenance(job_id=job_id, payload=payload)
        blast = provenance.get("blast") if isinstance(provenance.get("blast"), dict) else {}
        database = (
            provenance.get("database") if isinstance(provenance.get("database"), dict) else {}
        )
        precision = (
            provenance.get("precision") if isinstance(provenance.get("precision"), dict) else {}
        )
        fields["program"] = str(
            blast.get("program") or getattr(state, "program", None) or "blast"
        )
        fields["blast_version"] = blast.get("version")
        fields["database"] = str(
            database.get("name") or getattr(state, "db", None) or payload.get("db") or ""
        )
        fields["database_snapshot"] = database.get("snapshot")
        title = getattr(state, "job_title", None) or payload.get("job_title")
        fields["job_title"] = title if isinstance(title, str) else None
        note = precision.get("status") if isinstance(precision, dict) else None
        fields["compatibility_note"] = str(note) if note else None
    except Exception:
        LOGGER.debug("ncbi report header lookup failed", exc_info=True)
    return fields


def _export_ncbi_report(
    *,
    job_id: str,
    export_format: str,
    all_hits: list[dict[str, Any]],
) -> StreamingResponse:
    """Render the NCBI Web BLAST-style description table / report for a job."""
    from api.services.blast.ncbi_report import (
        aggregate_ncbi_rows,
        format_ncbi_hit_table,
        format_ncbi_report_text,
    )

    rows = aggregate_ncbi_rows(all_hits)

    if export_format == "ncbi-report-text":
        header = _ncbi_report_header_fields(job_id)
        body = format_ncbi_report_text(
            rows,
            rid=f"ELB-{job_id}",
            program=str(header["program"]),
            database=str(header["database"]),
            job_title=header["job_title"],
            blast_version=header["blast_version"],
            database_snapshot=header["database_snapshot"],
            compatibility_note=header["compatibility_note"],
        )
        filename = f"{job_id}_ncbi_report.txt"
        return StreamingResponse(
            iter([body.encode("utf-8")]),
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    delimiter = "\t" if export_format == "ncbi-hit-table-text" else ","
    body = format_ncbi_hit_table(rows, delimiter=delimiter)
    ext = "tsv" if export_format == "ncbi-hit-table-text" else "csv"
    mime = "text/tab-separated-values" if ext == "tsv" else "text/csv"
    filename = f"{job_id}_ncbi_hit_table.{ext}"
    return StreamingResponse(
        iter([body.encode("utf-8")]),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _export_raw_result_text(
    *,
    job_id: str,
    export_format: str,
    result_blobs: list[dict[str, Any]],
    cred: Any,
    storage_account: str,
) -> StreamingResponse:
    contents: list[tuple[str, str]] = []
    read_failures = 0
    reads = read_result_blob_texts_parallel(
        storage_account,
        result_blobs[:RESULTS_MAX_FILES],
        max_bytes=RESULTS_EXPORT_MAX_BYTES,
    )
    for blob_name, content, read_exc in reads:
        if not blob_name:
            continue
        if read_exc is not None:
            read_failures += 1
            LOGGER.debug("results raw export: failed to read blob", exc_info=True)
            continue
        if export_format == "xml" and not _looks_like_blast_xml(content):
            continue
        if export_format == "text" and _looks_like_blast_xml(content):
            continue
        contents.append((blob_name, content))

    if not contents:
        if read_failures:
            raise HTTPException(
                503,
                detail={
                    "code": "all_reads_failed",
                    "message": f"Failed to read any of {read_failures} result file(s).",
                },
            )
        raise HTTPException(
            409,
            detail={
                "code": "format_not_captured",
                "message": f"This job did not capture {export_format.upper()} output.",
            },
        )

    if export_format == "xml" and len(contents) > 1:
        raise HTTPException(
            409,
            detail={
                "code": "multiple_xml_reports",
                "message": (
                    "XML export requires one merged XML result; download files individually."
                ),
            },
        )

    if len(contents) == 1:
        body = contents[0][1]
    else:
        sections: list[str] = []
        for blob_name, content in contents:
            sections.append(f"# source_blob: {blob_name}\n{content.rstrip()}\n")
        body = "\n".join(sections)

    suffix = "xml" if export_format == "xml" else "txt"
    media_type = "application/xml" if export_format == "xml" else "text/plain"
    return StreamingResponse(
        iter([body.encode("utf-8")]),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{job_id}_results.{suffix}"'},
    )


def _looks_like_blast_xml(content: str) -> bool:
    stripped = content.lstrip("\ufeff \t\r\n")
    return stripped.startswith("<?xml") or stripped.startswith("<BlastOutput")


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
    storage_account = _resolve_job_storage_account(job_id, storage_account)
    try:
        from api.services.storage.data import (
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

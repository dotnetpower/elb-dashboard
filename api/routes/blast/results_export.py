"""BLAST result export routes.

The hit-table / JSON / NCBI-report / raw XML-text export endpoint and its
formatting helpers, split out of `api/routes/blast/results.py` so the
result-file/aggregate concerns and the export concern each own a
single-responsibility route module under the shared `blast_router`.

Responsibility: Serve `GET /jobs/{job}/results/export`, turning a job's parsed
    hits (or captured raw reports) into the requested CSV/TSV/JSON/NCBI/XML/text
    download.
Edit boundaries: HTTP validation + response shaping + format rendering only; hit
    parsing lives in `api/services/blast/results_parser.py`, NCBI aggregation in
    `api/services/blast/ncbi_report.py`, blob reads in
    `api/services/blast/result_analytics.py`.
Key entry points: `blast_job_results_export`.
Risky contracts: Every non-health `/api/*` route must enforce `require_caller`.
    When blobs exist but every read fails the export MUST 503 (not emit a
    misleading header-only file).
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
    api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _ensure_job_read_allowed,
    _maybe_open_local_storage_access,
    _resolve_job_storage_account,
)
from api.services.blast.result_analytics import (
    RESULTS_EXPORT_MAX_BYTES,
    RESULTS_MAX_FILES,
    annotate_result_hit,
    list_parseable_result_blobs,
    read_result_blob_texts_parallel,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/jobs/{job_id}/results/export")
def blast_job_results_export(
    job_id: str = Path(..., min_length=1, max_length=128),
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
        filename = f"{job_id}_{'seqalign' if export_format == 'json-seqalign' else 'results'}.json"
        return StreamingResponse(
            _stream_json_export(job_id, export_format, key, all_hits),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # CSV / TSV. Include extra columns only when at least one hit has them so
    # the file does not get a bunch of blank trailing columns for vanilla
    # `-outfmt 6` output.
    delimiter = "\t" if export_format == "tsv" else ","
    extras_present = [col for col in EXPORT_EXTRA_COLUMNS if any(col in hit for hit in all_hits)]
    columns = list(EXPORT_DEFAULT_COLUMNS) + extras_present
    ext = "tsv" if export_format == "tsv" else "csv"
    mime = "text/tab-separated-values" if export_format == "tsv" else "text/csv"
    return StreamingResponse(
        _stream_delimited_export(columns, delimiter, all_hits),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{job_id}_results.{ext}"'},
    )


def _stream_json_export(
    job_id: str, export_format: str, key: str, all_hits: list[dict[str, Any]]
) -> Iterator[bytes]:
    """Yield the JSON export incrementally so the full payload is never
    materialized twice (a 50K-hit export is ~50 MB; the previous
    ``json.dumps`` + ``.encode`` held two copies at once). ``all_hits`` is
    already in memory from parsing; this only avoids the duplicate
    serialization buffer."""
    import json

    head = (
        '{"job_id": '
        + json.dumps(job_id)
        + ', "format": '
        + json.dumps(export_format)
        + ', "'
        + key
        + '": ['
    )
    yield head.encode("utf-8")
    for index, hit in enumerate(all_hits):
        chunk = ("," if index else "") + json.dumps(hit, default=str)
        yield chunk.encode("utf-8")
    yield ('], "total": ' + str(len(all_hits)) + "}").encode("utf-8")


def _stream_delimited_export(
    columns: list[str], delimiter: str, all_hits: list[dict[str, Any]]
) -> Iterator[bytes]:
    """Yield CSV/TSV rows one at a time, reusing a single ``StringIO`` buffer
    so peak memory stays at one row instead of the whole file. ``all_hits`` is
    already parsed in memory; this removes the full-file ``StringIO`` copy."""
    import csv
    import io

    from api.services.blast.csv_safety import csv_safe_row

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, delimiter=delimiter, extrasaction="ignore")
    writer.writeheader()
    yield buf.getvalue().encode("utf-8")
    buf.seek(0)
    buf.truncate(0)
    for hit in all_hits:
        writer.writerow(csv_safe_row(hit))
        yield buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate(0)


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

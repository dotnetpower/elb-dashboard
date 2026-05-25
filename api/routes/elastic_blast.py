"""External ElasticBLAST API facade.

Responsibility: External ElasticBLAST API facade
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `ExternalBlastOptions`, `ExternalBlastSubmitRequest`,
`submit_external_blast_job`, `list_external_blast_jobs`, `get_external_blast_job`,
`list_external_blast_job_events`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from api.auth import CallerIdentity, require_caller
from api.services import external_blast
from api.services.blast.submit_payload import (
    canonical_submit_metadata,
    canonical_submit_snapshot,
    submit_contracts,
)

router = APIRouter(prefix="/api/v1/elastic-blast", tags=["external-elastic-blast"])
LOGGER = logging.getLogger(__name__)
MAX_QUERY_FASTA_CHARS = 10_000_000
_REQUIRE_CALLER = Depends(require_caller)


class ExternalBlastOptions(BaseModel):
    outfmt: Literal[5] = Field(5, description="Fixed to BLAST XML format 5")
    word_size: int = Field(28, ge=1)
    dust: bool = Field(True)
    evalue: float = Field(10.0, gt=0)
    max_target_seqs: int = Field(500, ge=1)


class ExternalBlastSubmitRequest(BaseModel):
    query_fasta: str = Field(..., min_length=1, max_length=MAX_QUERY_FASTA_CHARS)
    db: str = Field(..., min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._/-]+$")
    program: Literal[
        "blastn",
        "blastp",
        "blastx",
        "psiblast",
        "rpsblast",
        "rpstblastn",
        "tblastn",
        "tblastx",
    ] = Field("blastn")
    taxid: int | None = None
    is_inclusive: bool | None = None
    options: ExternalBlastOptions = Field(default_factory=ExternalBlastOptions)  # type: ignore[arg-type]
    priority: int = Field(50, ge=0, le=100)
    batch_len: int | None = Field(None, ge=1, le=1_000_000_000)
    idempotency_key: str | None = Field(None, min_length=1, max_length=256)
    external_correlation_id: str | None = Field(
        None,
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    resource_profile: str = Field(
        "standard", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    )

    @model_validator(mode="after")
    def validate_query_and_taxonomy(self) -> ExternalBlastSubmitRequest:
        from api.services.query_metadata import parse_fasta_metadata

        try:
            parse_fasta_metadata(self.query_fasta)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        if self.taxid is None and self.is_inclusive is not None:
            raise ValueError("is_inclusive requires taxid")
        if self.taxid is not None and self.is_inclusive is None:
            self.is_inclusive = True
        return self


_QUEUED_STATUSES = frozenset({"accepted", "created", "pending", "queued", "scheduled"})
_RUNNING_STATUSES = frozenset(
    {
        "dispatching",
        "finalizing",
        "in_progress",
        "inprogress",
        "reducing",
        "running",
        "splitting",
        "submitted",
        "submitting",
    }
)
_SUCCESS_STATUSES = frozenset({"complete", "completed", "success", "succeeded"})
_FAILED_STATUSES = frozenset({"canceled", "cancelled", "error", "failed", "failure", "timeout"})


def _public_status(value: Any, *, default: str = "queued") -> str:
    status = str(value or "").strip().casefold()
    if status in _QUEUED_STATUSES:
        return "queued"
    if status in _RUNNING_STATUSES:
        return "running"
    if status in _SUCCESS_STATUSES:
        return "success"
    if status in _FAILED_STATUSES:
        return "failed"
    return default


def _normalise_result_file(item: dict[str, Any], index: int) -> dict[str, Any]:
    filename = str(item.get("filename") or item.get("name") or f"blast_result_{index + 1}.xml")
    file_id = str(item.get("file_id") or item.get("id") or f"result-{index + 1:03d}")
    result_format = str(item.get("format") or _format_from_filename(filename))
    size_bytes = item.get("size_bytes") if item.get("size_bytes") is not None else item.get("size")
    out = dict(item)
    out.update(
        {
            "file_id": file_id,
            "filename": filename,
            "name": str(item.get("name") or filename),
            "format": result_format,
            "size_bytes": size_bytes,
            "size": size_bytes,
        }
    )
    return out


def _normalise_result_files(job: dict[str, Any]) -> None:
    result = job.get("result")
    if not isinstance(result, dict):
        return
    files = result.get("files")
    if not isinstance(files, list):
        result["files"] = []
        return
    result["files"] = [
        _normalise_result_file(item, index)
        for index, item in enumerate(files)
        if isinstance(item, dict)
    ]


def _format_from_filename(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith((".xml", ".xml.gz")):
        return "blast_xml"
    if lowered.endswith((".out", ".out.gz", ".tsv", ".txt")):
        return "blast_tabular"
    return "unknown"


def _normalise_external_job_payload(
    upstream: dict[str, Any],
    *,
    request_payload: dict[str, Any] | None = None,
    default_status: str = "queued",
) -> dict[str, Any]:
    payload = request_payload or {}
    out = dict(upstream)
    out["status"] = _public_status(out.get("status"), default=default_status)
    if out.get("submission_source") in (None, ""):
        out["submission_source"] = payload.get("submission_source") or "external_api"
    if out.get("external_correlation_id") in (None, ""):
        out["external_correlation_id"] = payload.get("external_correlation_id")
    if out.get("db_name") in (None, ""):
        out["db_name"] = out.get("db") or payload.get("db") or payload.get("database")
    if out.get("program") in (None, ""):
        out["program"] = payload.get("program")
    out.setdefault("blast_version", None)
    out.setdefault("db_version", None)
    _normalise_result_files(out)
    return out


@router.post("/submit", status_code=202)
def submit_external_blast_job(
    request: ExternalBlastSubmitRequest,
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    payload = request.model_dump(exclude_none=True)
    payload.update(
        canonical_submit_metadata(
            payload,
            submission_source="external_api",
            correlation_id=request.external_correlation_id,
        )
    )
    payload["canonical_request"] = canonical_submit_snapshot(payload)
    payload.update(submit_contracts(payload))
    from api.services.blast.provenance import build_blast_provenance

    payload["provenance"] = build_blast_provenance(
        job_id=str(payload["external_correlation_id"]),
        payload=payload,
    )
    LOGGER.info(
        "external BLAST submit accepted caller_oid=%s db=%s program=%s",
        caller.object_id,
        request.db,
        request.program,
    )
    del caller
    upstream = external_blast.submit_job(payload)
    return _normalise_external_job_payload(upstream, request_payload=payload)


@router.get("/jobs")
def list_external_blast_jobs(
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    """Forward to the external ElasticBLAST OpenAPI `/v1/jobs` listing.

    The dashboard's own `/api/blast/jobs` only surfaces locally-recorded job
    rows (from JobStateRepository / Azure Table Storage). Jobs submitted
    directly through the sibling OpenAPI service live in the cluster's
    ConfigMaps and are invisible to that route. This proxy lets the BLAST
    Jobs page join both sources.
    """
    LOGGER.info("external BLAST list requested caller_oid=%s", caller.object_id)
    del caller
    return external_blast.list_jobs()


@router.get("/jobs/{job_id}")
def get_external_blast_job(
    job_id: str = Path(..., min_length=6, max_length=12, pattern=r"^[a-f0-9]+$"),
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    LOGGER.info("external BLAST status requested caller_oid=%s job_id=%s", caller.object_id, job_id)
    del caller
    return _normalise_external_job_payload(
        external_blast.get_job(job_id),
        default_status="running",
    )


@router.get("/jobs/{job_id}/events")
def list_external_blast_job_events(
    job_id: str = Path(..., min_length=6, max_length=12, pattern=r"^[a-f0-9]+$"),
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    LOGGER.info("external BLAST events requested caller_oid=%s job_id=%s", caller.object_id, job_id)
    del caller
    try:
        from api.services.blast.events import canonical_job_events
        from api.services.state_repo import get_state_repo

        rows = get_state_repo().get_history(job_id, limit=200)
        if rows:
            return {"job_id": job_id, "events": canonical_job_events(rows)}
    except Exception as exc:
        LOGGER.info("external BLAST local events unavailable: %s", type(exc).__name__)
    detail = external_blast.get_job(job_id)
    status = str(detail.get("status") or detail.get("phase") or "unknown")
    return {
        "job_id": job_id,
        "events": [
            {
                "id": "current",
                "job_id": job_id,
                "event": status,
                "phase": status,
                "status": status,
                "timestamp": str(detail.get("updated_at") or detail.get("created_at") or ""),
                "payload": detail,
            }
        ],
    }


@router.get("/jobs/{job_id}/manifest")
def get_external_blast_job_manifest(
    job_id: str = Path(..., min_length=6, max_length=12, pattern=r"^[a-f0-9]+$"),
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    LOGGER.info(
        "external BLAST manifest requested caller_oid=%s job_id=%s",
        caller.object_id,
        job_id,
    )
    del caller
    from api.routes._blast_shared import _external_result_files
    from api.services.blast.result_manifest import build_result_manifest

    detail = external_blast.get_job(job_id)
    files = _external_result_files(detail)
    return build_result_manifest(job_id=job_id, files=files, source="external")


@router.get("/jobs/{job_id}/files/{file_id}")
def download_external_blast_file(
    job_id: str = Path(..., min_length=6, max_length=12, pattern=r"^[a-f0-9]+$"),
    file_id: str = Path(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"),
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> StreamingResponse:
    LOGGER.info(
        "external BLAST file requested caller_oid=%s job_id=%s file_id=%s",
        caller.object_id,
        job_id,
        file_id,
    )
    del caller
    downloaded = external_blast.stream_file(job_id, file_id)
    return StreamingResponse(
        downloaded.chunks,
        media_type=downloaded.media_type,
        headers={"Content-Disposition": f'attachment; filename="{downloaded.filename}"'},
    )

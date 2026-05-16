"""External ElasticBLAST API facade.

This router exposes the small direct-caller contract while the actual execution
is delegated to the sibling OpenAPI service.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.auth import CallerIdentity, require_caller
from api.services import external_blast

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
    options: ExternalBlastOptions = Field(default_factory=ExternalBlastOptions)
    priority: int = Field(50, ge=0, le=100)
    batch_len: int | None = Field(None, ge=1, le=1_000_000_000)
    idempotency_key: str | None = Field(None, min_length=1, max_length=256)
    resource_profile: str = Field(
        "standard", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    )


@router.post("/submit", status_code=202)
def submit_external_blast_job(
    request: ExternalBlastSubmitRequest,
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    payload = request.model_dump(exclude_none=True)
    payload["submission_source"] = "external_api"
    LOGGER.info(
        "external BLAST submit accepted caller_oid=%s db=%s program=%s",
        caller.object_id,
        request.db,
        request.program,
    )
    del caller
    return external_blast.submit_job(payload)


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
    return external_blast.get_job(job_id)


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

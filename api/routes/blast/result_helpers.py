"""Shared helpers for BLAST result routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from api.services.blast_result_analytics import InvalidResultBlobName, validate_result_blob_name

LOGGER = logging.getLogger(__name__)


def read_ready_result_artifact(job_id: str, artifact_type: str) -> dict[str, Any] | None:
    try:
        from api.services.job_artifacts import read_result_analytics_artifact

        payload = read_result_analytics_artifact(job_id, artifact_type)
        if payload is not None:
            return {**payload, "artifact_state": "ready", "source": "artifact"}
    except Exception as exc:
        LOGGER.info(
            "result artifact unavailable job_id=%s type=%s: %s",
            job_id,
            artifact_type,
            type(exc).__name__,
        )
    return None


def result_artifact_state(job_id: str, artifact_type: str) -> dict[str, Any]:
    try:
        from api.services.job_artifacts import artifact_state_payload

        return artifact_state_payload(job_id, artifact_type) or {"artifact_state": "missing"}
    except Exception as exc:
        LOGGER.info(
            "result artifact state unavailable job_id=%s type=%s: %s",
            job_id,
            artifact_type,
            type(exc).__name__,
        )
        return {"artifact_state": "unavailable"}


def enqueue_result_artifact_backfill(job_id: str, artifact_type: str) -> None:
    try:
        from api.services.job_artifacts import artifact_build_should_enqueue
        from api.tasks.blast_artifacts import finalize_job_artifacts

        if not artifact_build_should_enqueue(job_id, [artifact_type]):
            return
        finalize_job_artifacts.apply_async(kwargs={"job_id": job_id})
    except Exception as exc:
        LOGGER.info(
            "result artifact backfill enqueue skipped job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )


def default_alignments_request(
    *,
    blob_name: str,
    max_alignments: int,
    page: int,
    page_size: int | None,
    query_id: str,
    subject_id: str,
    organism: str,
    min_identity: float,
    min_bitscore: float,
    max_evalue: float,
    min_query_cover: float,
    sort_by: str,
    sort_dir: str,
) -> bool:
    return (
        not blob_name.strip()
        and max_alignments == 50
        and page == 1
        and page_size is None
        and not query_id.strip()
        and not subject_id.strip()
        and not organism.strip()
        and min_identity == 0.0
        and min_bitscore == 0.0
        and max_evalue == 10.0
        and min_query_cover == 0.0
        and sort_by == "relevance"
        and sort_dir == "asc"
    )


def default_taxonomy_request(
    *,
    blob_name: str,
    query_id: str,
    subject_id: str,
    organism: str,
    min_identity: float,
    min_bitscore: float,
    max_evalue: float,
    min_query_cover: float,
    include_lineage: bool,
) -> bool:
    return (
        not blob_name.strip()
        and not query_id.strip()
        and not subject_id.strip()
        and not organism.strip()
        and min_identity == 0.0
        and min_bitscore == 0.0
        and max_evalue == 10.0
        and min_query_cover == 0.0
        and not include_lineage
    )


def validate_result_blob_for_job(blob_name: str, job_id: str) -> None:
    try:
        validate_result_blob_name(blob_name, job_id)
    except InvalidResultBlobName as exc:
        detail: dict[str, str] = {"code": exc.code}
        message = str(exc)
        if message:
            detail["message"] = message
        raise HTTPException(400, detail=detail) from exc


__all__ = [
    "default_alignments_request",
    "default_taxonomy_request",
    "enqueue_result_artifact_backfill",
    "read_ready_result_artifact",
    "result_artifact_state",
    "validate_result_blob_for_job",
]

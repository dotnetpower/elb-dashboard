"""Resolve an NCBI accession to FASTA text for the BLAST submit pipeline.

Responsibility: One-call helper for `_normalise_blast_submit_body` to turn a
user-supplied accession (+ optional 1-based subrange) into FASTA text that the
existing inline-query upload path can stage to ``queries/uploads/{job_id}/``.
Edit boundaries: Bridge layer only — NCBI specifics live in
`api.services.ncbi`; storage/upload lives in `_upload_inline_query_for_submit`.
Key entry points: `resolve_accession_to_fasta`.
Risky contracts: Raises `HTTPException(422)` for caller-fixable validation
errors and `HTTPException(503)` for NCBI outages so the submit route surfaces
a stable error code. The FASTA returned here is fed straight to the existing
upload helper — it must NEVER be larger than the NCBI byte cap enforced by
`fetch_nuccore_fasta`.
Validation: `uv run pytest -q api/tests/test_blast_submit_accession.py`.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def _coerce_subrange(value: Any, *, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise HTTPException(
            422,
            detail={
                "code": "ncbi_accession_invalid",
                "message": f"{field_name} must be a positive integer",
            },
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            422,
            detail={
                "code": "ncbi_accession_invalid",
                "message": f"{field_name} must be a positive integer",
            },
        ) from exc
    if parsed <= 0 or parsed > 10**10:
        raise HTTPException(
            422,
            detail={
                "code": "ncbi_accession_invalid",
                "message": f"{field_name} must be between 1 and 10^10",
            },
        )
    return parsed


def resolve_accession_to_fasta(
    accession: str,
    *,
    seq_start: Any = None,
    seq_stop: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Return ``(fasta_text, metadata)`` for an NCBI nucleotide accession.

    ``metadata`` is the dict that should be merged into the submit's
    ``query_metadata`` so the job snapshot records that the query came from a
    public sequence rather than a user upload.
    """
    from api.services.ncbi import (
        NcbiRateLimited,
        NcbiResponseTooLarge,
        NcbiServiceUnavailable,
        fetch_nuccore_fasta,
        normalise_accession,
    )

    try:
        canonical = normalise_accession(accession)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "ncbi_accession_invalid", "message": str(exc)},
        ) from exc

    start_norm = _coerce_subrange(seq_start, field_name="query_accession_seq_start")
    stop_norm = _coerce_subrange(seq_stop, field_name="query_accession_seq_stop")
    if (start_norm is None) ^ (stop_norm is None):
        raise HTTPException(
            422,
            detail={
                "code": "ncbi_accession_invalid",
                "message": (
                    "query_accession_seq_start and query_accession_seq_stop "
                    "must both be provided or both omitted"
                ),
            },
        )

    try:
        fasta_text = fetch_nuccore_fasta(
            canonical, seq_start=start_norm, seq_stop=stop_norm
        )
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "ncbi_accession_invalid", "message": str(exc)},
        ) from exc
    except NcbiResponseTooLarge as exc:
        # Not retryable — same accession will always blow the cap. The
        # caller must supply an explicit sub-range. This matches the
        # frontend confirm dialog in ``SequenceDetail.launchBlast``.
        raise HTTPException(
            422,
            detail={
                "code": "ncbi_query_too_large",
                "message": (
                    f"{exc!s}. Supply query_accession_seq_start / "
                    "query_accession_seq_stop to BLAST a sub-range."
                ),
            },
        ) from exc
    except NcbiRateLimited as exc:
        # Our own token bucket is saturated — NCBI itself is healthy. Map
        # to 429 with a short Retry-After so the SPA retries quickly
        # instead of waiting the 30 s window we use for genuine outages.
        raise HTTPException(
            429,
            detail={
                "code": "ncbi_rate_limited",
                "message": str(exc),
                "retryable": True,
                "retry_after_seconds": 1,
            },
        ) from exc
    except NcbiServiceUnavailable as exc:
        raise HTTPException(
            503,
            detail={
                "code": "ncbi_lookup_unavailable",
                "message": str(exc),
                "retryable": True,
                "retry_after_seconds": 30,
            },
        ) from exc

    metadata: dict[str, Any] = {
        "query_source": "ncbi_accession",
        "query_accession": canonical,
    }
    if start_norm is not None and stop_norm is not None:
        metadata["query_accession_seq_start"] = start_norm
        metadata["query_accession_seq_stop"] = stop_norm
    return fasta_text, metadata

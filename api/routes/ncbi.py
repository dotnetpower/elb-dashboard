"""NCBI nuccore HTTP routes.

Responsibility: Expose the dashboard-internal NCBI nuccore lookup endpoints
that back the BLAST result accession deep-link and the "submit by accession"
flow.
Edit boundaries: HTTP validation + response shaping only. All NCBI calls go
through `api.services.ncbi`.
Key entry points: `get_nuccore_summary`, `get_nuccore_genbank`,
`get_nuccore_fasta`.
Risky contracts: Every route enforces `require_caller` and converts
`NcbiServiceUnavailable` to a 503 with a stable error code so the SPA can
render a retry hint.
Validation: `uv run pytest -q api/tests/test_ncbi_nuccore.py`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import PlainTextResponse

from api._http_utils import NCBI_LOOKUP_RESPONSES
from api.auth import CallerIdentity, require_caller

router = APIRouter(prefix="/api/ncbi", tags=["ncbi"])


# Per-caller (object_id) sub-bucket on top of the shared process-wide
# NCBI rate limiter. Stops a single operator scripting against
# `/api/ncbi/nuccore/...` from draining the entire process token bucket
# and starving every other caller's BLAST submit `resolve_accession_to_fasta`
# path. The window is intentionally generous (30 req/min) — interactive
# Sequence Detail browsing easily fits, but a tight script loop trips it.
_CALLER_LIMIT_PER_MIN = 30
_CALLER_BUCKETS: dict[str, list[float]] = {}
_CALLER_BUCKETS_GUARD: Any = None  # lazy threading.Lock to avoid import cost


def _check_caller_quota(caller: CallerIdentity) -> None:
    """Refuse with 429 ``caller_throttled`` when this oid is over budget.

    Distinct from ``ncbi_rate_limited`` (the shared bucket) so the SPA /
    third-party caller can tell "I am being throttled" from "the
    dashboard's NCBI quota is saturated".
    """
    global _CALLER_BUCKETS_GUARD
    import threading
    import time

    if _CALLER_BUCKETS_GUARD is None:
        _CALLER_BUCKETS_GUARD = threading.Lock()
    oid = (caller.object_id or "").strip() or "anonymous"
    now = time.monotonic()
    window = 60.0
    with _CALLER_BUCKETS_GUARD:
        bucket = _CALLER_BUCKETS.setdefault(oid, [])
        # Evict timestamps older than the window.
        cutoff = now - window
        # Single-pass filter; `bucket` stays sorted ascending.
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= _CALLER_LIMIT_PER_MIN:
            retry_after = max(1, int(window - (now - bucket[0])))
            raise HTTPException(
                429,
                detail={
                    "code": "caller_throttled",
                    "message": (
                        f"NCBI lookups: per-caller limit of "
                        f"{_CALLER_LIMIT_PER_MIN}/min exceeded. Retry shortly."
                    ),
                    "retryable": True,
                    "retry_after_seconds": retry_after,
                },
            )
        bucket.append(now)


def _reset_caller_quota_for_tests() -> None:
    """Test hook \u2014 drop every per-caller bucket so test order is stable."""
    global _CALLER_BUCKETS_GUARD
    import threading

    if _CALLER_BUCKETS_GUARD is None:
        _CALLER_BUCKETS_GUARD = threading.Lock()
    with _CALLER_BUCKETS_GUARD:
        _CALLER_BUCKETS.clear()


_ACCESSION_PATH = Path(
    ...,
    min_length=1,
    max_length=64,
    description="NCBI nucleotide accession (with optional version suffix).",
)


@router.get("/nuccore/{accession}", responses=NCBI_LOOKUP_RESPONSES)
def get_nuccore_summary(
    accession: str = _ACCESSION_PATH,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.services.ncbi import (
        NcbiRateLimited,
        NcbiServiceUnavailable,
        fetch_nuccore_summary,
    )

    _check_caller_quota(caller)
    try:
        return fetch_nuccore_summary(accession)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "ncbi_accession_invalid", "message": str(exc)},
        ) from exc
    except NcbiRateLimited as exc:
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


@router.get("/nuccore/{accession}/genbank", responses=NCBI_LOOKUP_RESPONSES)
def get_nuccore_genbank(
    accession: str = _ACCESSION_PATH,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.services.ncbi import (
        NcbiRateLimited,
        NcbiServiceUnavailable,
        fetch_nuccore_genbank,
    )

    _check_caller_quota(caller)
    try:
        return fetch_nuccore_genbank(accession)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "ncbi_accession_invalid", "message": str(exc)},
        ) from exc
    except NcbiRateLimited as exc:
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


@router.get(
    "/nuccore/{accession}/fasta",
    response_class=PlainTextResponse,
    responses=NCBI_LOOKUP_RESPONSES,
)
def get_nuccore_fasta(
    accession: str = _ACCESSION_PATH,
    seq_start: int | None = Query(
        default=None,
        ge=1,
        le=10**10,
        description="1-based inclusive start position (optional).",
    ),
    seq_stop: int | None = Query(
        default=None,
        ge=1,
        le=10**10,
        description="1-based inclusive end position (optional).",
    ),
    caller: CallerIdentity = Depends(require_caller),
) -> PlainTextResponse:
    from api.services.ncbi import (
        NcbiRateLimited,
        NcbiResponseTooLarge,
        NcbiServiceUnavailable,
        fetch_nuccore_fasta,
    )

    _check_caller_quota(caller)
    try:
        text = fetch_nuccore_fasta(
            accession, seq_start=seq_start, seq_stop=seq_stop
        )
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "ncbi_accession_invalid", "message": str(exc)},
        ) from exc
    except NcbiResponseTooLarge as exc:
        raise HTTPException(
            422,
            detail={
                "code": "ncbi_query_too_large",
                "message": (
                    f"{exc!s}. Supply seq_start / seq_stop to fetch a sub-range."
                ),
            },
        ) from exc
    except NcbiRateLimited as exc:
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
    return PlainTextResponse(
        text,
        media_type="text/x-fasta",
        headers={"Cache-Control": "private, max-age=900"},
    )

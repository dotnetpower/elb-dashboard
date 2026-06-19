"""NCBI nuccore HTTP routes.

Responsibility: Expose the dashboard-internal NCBI nuccore lookup endpoints
that back the BLAST result accession deep-link and the "submit by accession"
flow.
Edit boundaries: HTTP validation + response shaping only. All NCBI calls go
through `api.services.ncbi`. Per-caller quota helpers (charge/refund) live
here because they wrap the route ↔ service handshake.
Key entry points: `get_nuccore_summary`, `get_nuccore_genbank`,
`get_nuccore_fasta`.
Risky contracts: Every route enforces `require_caller` and converts
`NcbiServiceUnavailable` to a 503 with a stable error code so the SPA can
render a retry hint. The per-caller bucket is reserved BEFORE the shared
NCBI bucket attempt and REFUNDED when the shared bucket throttles — see
`_charge_caller_quota` / `_refund_caller_quota`.
Validation: `uv run pytest -q api/tests/test_ncbi_nuccore.py`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import PlainTextResponse

from api._http_utils import NCBI_LOOKUP_RESPONSES
from api.auth import CallerIdentity, is_dev_bypass_caller, require_caller

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ncbi", tags=["ncbi"])


# Per-caller (object_id) sub-bucket on top of the shared process-wide
# NCBI rate limiter. Stops a single operator scripting against
# `/api/ncbi/nuccore/...` from draining the entire process token bucket
# and starving every other caller's BLAST submit `resolve_accession_to_fasta`
# path. The window is intentionally generous (30 req/min) — interactive
# Sequence Detail browsing easily fits, but a tight script loop trips it.
_CALLER_LIMIT_PER_MIN = 30
# Soft cap on distinct keys we track. Each key carries a 30-slot deque of
# floats (~600 bytes including dict overhead). At 4096 keys this is ~2.4 MB,
# bounded for the lifetime of a long-running sidecar. When the cap is
# exceeded we evict the LRU entry — that caller will simply get a fresh
# bucket on their next request.
_CALLER_BUCKETS_MAX_KEYS = 4096
# OrderedDict so eviction is LRU. Initialised at module load (cheap;
# `OrderedDict()` + `threading.Lock()` are sub-microsecond).
_CALLER_BUCKETS: OrderedDict[str, deque[float]] = OrderedDict()
_CALLER_BUCKETS_GUARD: threading.Lock = threading.Lock()


def _caller_bucket_key(caller: CallerIdentity) -> str:
    """Stable per-caller key that does not collide for dev-bypass / empty-oid.

    Critique #10: every dev-bypass caller resolves to the same
    ``DEV_BYPASS_OID``, so historically all local developers shared one
    30 req/min bucket. Now we namespace dev-bypass identities by
    ``upn`` (which `_dev_bypass_identity` always sets) so two browser
    tabs / two developers do not starve each other.

    For real callers with an empty oid we DO NOT silently fall back to
    a shared "anonymous" bucket — the route raises 401 instead (see
    `_check_caller_quota`).
    """
    if is_dev_bypass_caller(caller):
        # Different upn → different bucket. The synthetic upn is
        # "dev-bypass@local" by default but tests / multi-window dev
        # can override via the AUTH_DEV_BYPASS_UPN env var (future
        # extension) — at minimum every CallerIdentity already carries
        # a distinct upn for callers that override it.
        upn = (caller.upn or "").strip() or "dev-bypass@local"
        return f"dev-bypass:{upn}"
    return (caller.object_id or "").strip()


def _evict_expired_locked(bucket: deque[float], cutoff: float) -> None:
    """Pop timestamps older than ``cutoff`` from the LEFT (O(1) each)."""
    while bucket and bucket[0] < cutoff:
        bucket.popleft()


def _check_caller_quota(caller: CallerIdentity) -> str:
    """Reserve a quota slot and return the bucket key for refund.

    Distinct from ``ncbi_rate_limited`` (the shared bucket) so the SPA /
    third-party caller can tell "I am being throttled" from "the
    dashboard's NCBI quota is saturated". Returns the bucket key so the
    route can refund the slot via `_refund_caller_quota` if the shared
    bucket subsequently throttles — a refused upstream request must not
    consume the caller's quota (critique #13).
    """
    key = _caller_bucket_key(caller)
    if not key:
        # Real caller with an empty object_id claim. Falling back to
        # "anonymous" would put every such caller into one shared
        # bucket (critique #10). 401 instead so the missing claim is
        # surfaced explicitly.
        raise HTTPException(
            401,
            detail={
                "code": "missing_caller_identity",
                "message": "Caller object_id claim is required for NCBI quota accounting.",
            },
        )
    now = time.monotonic()
    cutoff = now - 60.0
    with _CALLER_BUCKETS_GUARD:
        bucket = _CALLER_BUCKETS.get(key)
        if bucket is None:
            bucket = deque(maxlen=_CALLER_LIMIT_PER_MIN)
            _CALLER_BUCKETS[key] = bucket
            # Soft cap eviction — LRU.
            while len(_CALLER_BUCKETS) > _CALLER_BUCKETS_MAX_KEYS:
                evicted, _ = _CALLER_BUCKETS.popitem(last=False)
                LOGGER.debug("NCBI caller bucket evicted (lru): key=%s...", evicted[:8])
        else:
            # Touch for LRU.
            _CALLER_BUCKETS.move_to_end(key)
        _evict_expired_locked(bucket, cutoff)
        if len(bucket) >= _CALLER_LIMIT_PER_MIN:
            retry_after = max(1, int(60.0 - (now - bucket[0])))
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
        # Garbage-collect the dict entry when the bucket is empty after
        # eviction. Without this, every unique key sticks in the dict
        # forever even after the rate-limit window expires (critique #11).
        # (We only reach this branch when the entry was just appended
        # to, so len > 0 — but cover the case below in `_refund` where
        # a refund drops the last entry.)
    return key


def _refund_caller_quota(key: str) -> None:
    """Pop the most-recent timestamp for ``key`` after a refused upstream.

    Critique #13: per-caller quota was being charged before the shared
    NCBI bucket attempt. If the shared bucket throttles, the user gets
    a 429 but has already lost a quota slot for a request that never
    reached NCBI. Refunding here restores accounting parity — a tight
    client retrying on 429 only pays one slot per round-trip that
    actually charged NCBI.
    """
    if not key:
        return
    with _CALLER_BUCKETS_GUARD:
        bucket = _CALLER_BUCKETS.get(key)
        if not bucket:
            return
        # Pop the most recent timestamp — that is the slot we just
        # charged in `_check_caller_quota`. (Mid-flight other requests
        # from the same caller may have appended later timestamps; in
        # that rare interleaving we refund the newest, which is the
        # most generous outcome and still bounded by the deque maxlen.)
        bucket.pop()
        if not bucket:
            _CALLER_BUCKETS.pop(key, None)


def _reset_caller_quota_for_tests() -> None:
    """Test hook — drop every per-caller bucket so test order is stable."""
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

    bucket_key = _check_caller_quota(caller)
    try:
        return fetch_nuccore_summary(accession)
    except ValueError as exc:
        # Validation failure never reached NCBI — refund the slot.
        _refund_caller_quota(bucket_key)
        raise HTTPException(
            422,
            detail={"code": "ncbi_accession_invalid", "message": str(exc)},
        ) from exc
    except NcbiRateLimited as exc:
        # Shared bucket throttled — the request never reached NCBI, so
        # refund the per-caller slot (critique #13).
        _refund_caller_quota(bucket_key)
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

    bucket_key = _check_caller_quota(caller)
    try:
        return fetch_nuccore_genbank(accession)
    except ValueError as exc:
        _refund_caller_quota(bucket_key)
        raise HTTPException(
            422,
            detail={"code": "ncbi_accession_invalid", "message": str(exc)},
        ) from exc
    except NcbiRateLimited as exc:
        _refund_caller_quota(bucket_key)
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

    bucket_key = _check_caller_quota(caller)
    try:
        text = fetch_nuccore_fasta(
            accession, seq_start=seq_start, seq_stop=seq_stop
        )
    except ValueError as exc:
        _refund_caller_quota(bucket_key)
        raise HTTPException(
            422,
            detail={"code": "ncbi_accession_invalid", "message": str(exc)},
        ) from exc
    except NcbiResponseTooLarge as exc:
        _refund_caller_quota(bucket_key)
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
        _refund_caller_quota(bucket_key)
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


# ---------------------------------------------------------------------------
# Discovery — back the New Search "Generate query" modal (organism/keyword
# search → candidate accessions, then a chosen accession → gene/CDS features).
# ---------------------------------------------------------------------------
_SEARCH_QUERY = Query(
    ...,
    min_length=1,
    max_length=200,
    description="Free-text organism/keyword/accession Entrez query for db=nuccore.",
)
_SEARCH_LIMIT = Query(
    default=10,
    ge=1,
    le=25,
    description="Maximum candidate records to return.",
)
_FEATURES_LIMIT = Query(
    default=1000,
    ge=1,
    le=1000,
    description="Maximum gene features to return.",
)


def _raise_ncbi_http_error(exc: Exception, bucket_key: str) -> None:
    """Map an NCBI service exception to the stable HTTP error the SPA expects.

    Shared by the discovery routes (search / features). Mirrors the per-route
    mapping used by the nuccore fetch routes above: validation → 422, our own
    rate limiter → 429 (with a refund, the request never reached NCBI),
    over-cap → 422, upstream outage → 503.
    """
    from api.services.ncbi import (
        NcbiRateLimited,
        NcbiResponseTooLarge,
        NcbiServiceUnavailable,
    )

    if isinstance(exc, ValueError):
        _refund_caller_quota(bucket_key)
        raise HTTPException(
            422, detail={"code": "ncbi_query_invalid", "message": str(exc)}
        ) from exc
    if isinstance(exc, NcbiResponseTooLarge):
        _refund_caller_quota(bucket_key)
        raise HTTPException(
            422,
            detail={
                "code": "ncbi_query_too_large",
                "message": f"{exc!s}. Try a smaller record.",
            },
        ) from exc
    if isinstance(exc, NcbiRateLimited):
        _refund_caller_quota(bucket_key)
        raise HTTPException(
            429,
            detail={
                "code": "ncbi_rate_limited",
                "message": str(exc),
                "retryable": True,
                "retry_after_seconds": 1,
            },
        ) from exc
    if isinstance(exc, NcbiServiceUnavailable):
        raise HTTPException(
            503,
            detail={
                "code": "ncbi_lookup_unavailable",
                "message": str(exc),
                "retryable": True,
                "retry_after_seconds": 30,
            },
        ) from exc
    raise exc


@router.get("/search", responses=NCBI_LOOKUP_RESPONSES)
def search_nuccore_route(
    q: str = _SEARCH_QUERY,
    limit: int = _SEARCH_LIMIT,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Search ``db=nuccore`` by organism/keyword/accession and return candidates."""
    from api.services.ncbi.search import search_nuccore

    bucket_key = _check_caller_quota(caller)
    try:
        return search_nuccore(q, limit=limit)
    except Exception as exc:
        _raise_ncbi_http_error(exc, bucket_key)
        raise  # unreachable — _raise_ncbi_http_error always raises


@router.get("/nuccore/{accession}/features", responses=NCBI_LOOKUP_RESPONSES)
def get_nuccore_features(
    accession: str = _ACCESSION_PATH,
    limit: int = _FEATURES_LIMIT,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the gene/CDS features (name + 1-based coordinates) of a record."""
    from api.services.ncbi.search import fetch_feature_table

    bucket_key = _check_caller_quota(caller)
    try:
        return fetch_feature_table(accession, limit=limit)
    except Exception as exc:
        _raise_ncbi_http_error(exc, bucket_key)
        raise  # unreachable — _raise_ncbi_http_error always raises

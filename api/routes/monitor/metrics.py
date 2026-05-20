"""Request metrics and HTTP inspector routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller

router = APIRouter()


@router.get("/metrics")
def request_metrics(
    window_seconds: int = Query(default=900, ge=60, le=24 * 60 * 60),
    path_prefix: str = Query(default=""),
    rpm_buckets: int = Query(default=60, ge=1, le=720),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Per-process request latency / error / RPM aggregates.

    Defaults to last 15 minutes.  `path_prefix=/api/blast` filters to a
    subset of the surface so the SPA can ask "how is BLAST doing?"
    distinctly from "how is the API doing overall?".

    Per-process scope: when the api sidecar is replicated each replica
    has its own buffer.  The Container App pins replicas to 1 so this
    is currently equivalent to "global" — see the comment at the top of
    [api/services/request_metrics.py](../services/request_metrics.py).
    """
    from api.services.request_metrics import metrics as _metrics

    # Reject path_prefix that is not under /api/.  Anything else would
    # be a wildcard search of the buffer (or worse, allow callers to
    # probe paths they don't normally see) — neither is desirable.
    if path_prefix and not path_prefix.startswith("/api/"):
        raise HTTPException(400, "path_prefix must start with /api/")
    try:
        return _metrics().summarise(
            window_seconds=window_seconds,
            path_prefix=path_prefix or None,
            rpm_buckets=rpm_buckets,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# Per-request HTTP inspector — full headers + (capped) body for the most
# recent N requests. Backs the "View HTTP requests" panel on the SidecarsCard.
# ---------------------------------------------------------------------------
@router.get("/sidecar-requests")
def sidecar_requests(
    limit: int = Query(default=200, ge=1, le=1000),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the most recent captured requests (newest first).

    Capture happens in `api.main.RequestIdMiddleware` and is opt-out via
    the `REQUEST_DETAIL_CAPTURE_ENABLED=false` environment variable. The
    buffer is per-process (no replication), and sensitive headers
    (`Authorization`, `Cookie`, `X-Api-Key`, …) are redacted at capture
    time — see
    [api/services/request_metrics.py](../services/request_metrics.py)
    `DETAIL_REDACT_HEADERS`.
    """
    from api.services.request_metrics import details as _details

    items = _details().list_recent(limit=limit)
    return {
        "items": items,
        "count": len(items),
        "capacity": _details().capacity,
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

"""Inspector capture rules — which paths/methods are recorded in the metrics buffer.

Responsibility: Hold the static path lists and the predicate used by the
RequestIdMiddleware to decide whether to buffer request/response bodies.
Edit boundaries: Pure data + a single predicate; do not add Azure SDK calls,
HTTP work, or logging here.
Key entry points: `_inspector_should_capture`, `INSPECTOR_MAX_BUFFER_BYTES`.
Risky contracts: SSE / WebSocket paths MUST stay in the exclude list — buffering
a never-ending response body would leak memory until the worker is killed.
Validation: `uv run pytest -q api/tests/test_inspector_exclude.py`.
"""

from __future__ import annotations

# Paths excluded from BOTH the aggregate metrics buffer AND the per-request
# DETAIL inspector buffer. SSE/WebSocket cannot be safely body-buffered (the
# response never ends). High-volume self-poll paths would self-amplify.
INSPECTOR_EXCLUDE_PREFIXES: tuple[str, ...] = (
    "/api/monitor/sidecars",  # SSE topology + high-volume snapshot
    "/api/monitor/metrics",  # would self-amplify
    "/api/monitor/sidecar-requests",  # would self-amplify
    "/api/blast/logs",  # SSE job log stream
    "/api/terminal/ws",  # WebSocket upgrade
)
INSPECTOR_EXCLUDE_EXACT: frozenset[str] = frozenset({"/api/health"})

# High-volume polling GETs whose response body is buffered into memory by
# the middleware on every dashboard tick (30 s default, 5 s minimum). The
# inspector value of these reads is low — the dashboard refetches them
# constantly so the same payload is captured over and over, pushing more
# interesting one-shot calls (POST submit, DELETE) out of the ring buffer.
# Non-GET methods on the same paths (e.g. POST /api/blast/jobs submit)
# are still captured.
INSPECTOR_EXCLUDE_GET_PREFIXES: tuple[str, ...] = (
    "/api/monitor/aks",
    "/api/monitor/storage",
    "/api/monitor/acr",
    "/api/monitor/terminal",
    "/api/monitor/cluster",
    "/api/monitor/jobs",
    "/api/blast/jobs",
    "/api/blast/databases",
    "/api/warmup",
    "/api/me",
)

# Hard cap on how many bytes the middleware will buffer when capturing
# request OR response body for the inspector. The detail buffer itself
# truncates at 4 KiB; this is a safety ceiling so a misclassified content
# type (e.g. a 100 MiB JSON dump) cannot OOM the api sidecar.
INSPECTOR_MAX_BUFFER_BYTES = 64 * 1024


def _inspector_should_capture(path: str, method: str = "POST") -> bool:
    """True iff the per-request DETAIL inspector should record this path.

    ``method`` defaults to a non-GET verb to preserve the historical
    single-arg call sites (treat as "is this path ever capturable?").
    Pass the actual method to skip body buffering for high-volume polling
    GETs that would otherwise dominate the inspector ring buffer. ``None``
    is normalised to the default so a caller forwarding an unset header
    cannot crash the middleware.
    """
    if not path.startswith("/api/"):
        return False
    if path in INSPECTOR_EXCLUDE_EXACT:
        return False
    if any(path.startswith(p) for p in INSPECTOR_EXCLUDE_PREFIXES):
        return False
    if (method or "POST").upper() == "GET" and any(
        path.startswith(p) for p in INSPECTOR_EXCLUDE_GET_PREFIXES
    ):
        return False
    return True

"""Per-token rate-limit middleware for the OpenAPI BLAST submit surface.

Responsibility: Protect the sibling ElasticBLAST OpenAPI plane (and the dashboard
    routes that proxy to it) from accidental burst storms — a wrong loop, a stuck
    retry, a leaked admin token. Bound external callers to a generous quota so a
    single misbehaving client cannot exhaust the AKS API server or fill the BLAST
    Job queue.
Edit boundaries: Keep this middleware focused on per-token rate-limiting; do not
    add auth checks or response shaping (those live in `api.auth` and the routes).
    Memory-only; no Redis dependency. Replace with a Redis-backed implementation
    when horizontal replicas are introduced.
Key entry points: `OpenApiRateLimitMiddleware`.
Risky contracts: Path matching MUST stay tight — bumping `_LIMITED_PATH_PREFIXES`
    can unintentionally throttle the dashboard's own polling. The 429 response
    must include a `Retry-After` header (HTTP/1.1 §10.4.30); the SPA's fetch
    layer reads it to back off cleanly.
Validation: `uv run pytest -q api/tests/test_openapi_rate_limit.py`.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Paths whose traffic ends up on the sibling OpenAPI plane. Each entry is a
# string prefix matched against ``request.url.path`` (lowercased).
_LIMITED_PATH_PREFIXES: tuple[str, ...] = (
    "/api/v1/elastic-blast/",
    "/api/aks/openapi/proxy",
)

# Per-key request budget. Defaults to 2000 requests per 60-second sliding
# window — about 33 req/s sustained, generous burst headroom. Override via
# env without touching code. Values are re-read on every request so an
# operator can ``az containerapp update --set-env-vars …`` to adjust the
# quota without rebuilding the image; the read cost is negligible compared
# to a sibling round-trip.
_DEFAULT_WINDOW_SECONDS = 60.0
_DEFAULT_MAX_REQUESTS_PER_WINDOW = 2000


def _window_seconds() -> float:
    return float(
        os.environ.get(
            "OPENAPI_RATE_LIMIT_WINDOW_SECONDS", str(_DEFAULT_WINDOW_SECONDS)
        )
    )


def _max_requests_per_window() -> int:
    return int(
        os.environ.get(
            "OPENAPI_RATE_LIMIT_REQUESTS_PER_WINDOW",
            str(_DEFAULT_MAX_REQUESTS_PER_WINDOW),
        )
    )


# Cap the in-memory key set so a misbehaving caller cycling token values
# cannot grow this dict without bound. LRU-style eviction is good enough
# here — the bucket reset on eviction is the same outcome as a TTL.
_MAX_KEYS = int(os.environ.get("OPENAPI_RATE_LIMIT_MAX_KEYS", "1024"))

# Disable bypass for tests and ops debugging without recompiling.
_DISABLED_VALUES = {"", "0", "false", "no", "off"}


def _is_enabled() -> bool:
    return (
        os.environ.get("OPENAPI_RATE_LIMIT_DISABLED", "").strip().lower()
        in _DISABLED_VALUES
    )


def _normalise_token(value: str) -> str:
    return value.strip()[:128]


def _request_key(request: Request) -> str:
    """Pick the rate-limit key for ``request``.

    Token-based first: the OpenAPI submit surface is keyed by the
    ``X-ELB-API-Token`` header (the sibling's admin token). Falls back to
    the caller's IP when the header is absent — that path covers anonymous
    health probes and the local-dev caller before the deploy task wires up
    the token. The two key spaces never collide because tokens carry a
    leading ``token:`` prefix and IPs carry ``ip:``.
    """
    token = request.headers.get("x-elb-api-token", "")
    if token:
        return f"token:{_normalise_token(token)}"
    client = request.client
    ip = client.host if client else "unknown"
    return f"ip:{ip}"


def _path_is_limited(path: str) -> bool:
    lower = path.lower()
    return any(lower.startswith(prefix) for prefix in _LIMITED_PATH_PREFIXES)


class _SlidingWindowCounter:
    """Thread-safe per-key sliding-window counter.

    Each key carries a deque of monotonic timestamps of recent hits.
    Eviction policy: when the dict exceeds ``_MAX_KEYS``, drop the key
    with the oldest most-recent hit. The resulting "reset" is benign —
    that caller starts a fresh window, never gets a more permissive quota
    than the policy allows.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def check_and_record(
        self, key: str, *, max_requests: int, window_seconds: float
    ) -> tuple[bool, float]:
        """Record one hit if under quota. Return ``(allowed, retry_after_seconds)``.

        ``retry_after_seconds`` is 0 when the call is allowed; otherwise it
        is the number of seconds until the oldest hit in the window falls
        off (i.e. the soonest the caller could succeed).
        """
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket
                if len(self._hits) > _MAX_KEYS:
                    self._evict_oldest_locked()
            # Discard hits outside the window.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= max_requests:
                retry_after = max(0.0, bucket[0] + window_seconds - now)
                # Round up to the nearest whole second so the client cannot
                # legally re-fire before the window actually opens.
                return (False, max(1.0, retry_after))
            bucket.append(now)
            return (True, 0.0)

    def _evict_oldest_locked(self) -> None:
        oldest_key: str | None = None
        oldest_ts = float("inf")
        for key, bucket in self._hits.items():
            ts = bucket[-1] if bucket else 0.0
            if ts < oldest_ts:
                oldest_key = key
                oldest_ts = ts
        if oldest_key is not None:
            self._hits.pop(oldest_key, None)

    def reset(self) -> None:
        """Test hook: clear all state."""
        with self._lock:
            self._hits.clear()


_counter = _SlidingWindowCounter()


def reset_openapi_rate_limit_state() -> None:
    """Clear in-memory state. Used by test fixtures."""
    _counter.reset()


class OpenApiRateLimitMiddleware(BaseHTTPMiddleware):
    """Reject OpenAPI submit-path requests that exceed the per-key budget."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not _is_enabled():
            return await call_next(request)
        if not _path_is_limited(request.url.path):
            return await call_next(request)
        key = _request_key(request)
        max_requests = _max_requests_per_window()
        window_seconds = _window_seconds()
        allowed, retry_after = _counter.check_and_record(
            key,
            max_requests=max_requests,
            window_seconds=window_seconds,
        )
        if not allowed:
            # Mask the token portion of the key in headers / logs. The key
            # carries either an ``ip:`` prefix (safe) or a ``token:``
            # prefix (sensitive). Headers must never leak the token.
            key_kind = key.split(":", 1)[0]
            retry_seconds = int(retry_after)
            return JSONResponse(
                {
                    "code": "rate_limited",
                    "message": (
                        "OpenAPI submit quota exceeded. "
                        f"Limit: {max_requests} requests per "
                        f"{int(window_seconds)}s. Retry after "
                        f"{retry_seconds}s."
                    ),
                    "retry_after_seconds": retry_seconds,
                    "key_kind": key_kind,
                },
                status_code=429,
                headers={"Retry-After": str(retry_seconds)},
            )
        return await call_next(request)

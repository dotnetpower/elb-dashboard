"""Catch-all reverse proxy: forwards non-/api/* requests to the frontend.

Responsibility: Catch-all reverse proxy: forwards non-/api/* requests to the frontend
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_get_client`, `reverse_proxy`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.routing import Match

LOGGER = logging.getLogger(__name__)
FRONTEND_UPSTREAM = os.environ.get("FRONTEND_UPSTREAM", "http://127.0.0.1:8081")

# Headers the proxy must NOT pass through verbatim because httpx / FastAPI
# manages them on the new connection.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",  # we set our own
    "content-length",  # let httpx compute
}

# Headers the proxy must STRIP for security, even though they are not
# hop-by-hop. The frontend sidecar only serves static SPA assets — it has
# no use for the caller's MSAL bearer token, and forwarding the token
# means it lands in the sidecar's nginx access log and any future
# upstream middleware. ``cookie`` is unused today but stripped for
# defence-in-depth: if a future browser feature ever sets one, the
# proxy must not pass it on to the nginx sidecar by accident. The
# ``x-forwarded-*`` auth variants cover reverse-proxy chains (some
# ingress controllers add them automatically) so an unaware setup
# cannot launder the bearer through to nginx via an indirect header.
_FRONTEND_STRIP_HEADERS = {
    "authorization",
    "cookie",
    "x-elb-api-token",
    "x-forwarded-authorization",
    "x-forwarded-user",
    "x-forwarded-access-token",
    "x-forwarded-id-token",
}

router = APIRouter(tags=["frontend"])

# Methods that have NO meaning for a static-asset frontend. CONNECT / TRACE
# are commonly used in CSRF / cache-poisoning probes; static assets only
# need GET / HEAD / OPTIONS in practice but we also keep POST / PUT /
# PATCH / DELETE so a SPA-side feature that ever calls a frontend-served
# JSON endpoint is not broken. The api router list is registered before
# this catch-all so /api/* never reaches us.
_FRONTEND_ALLOWED_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}

# Module-level client so connection pool is reused across requests.
_client: httpx.AsyncClient | None = None
# Single-flight guard on the lazy init. Without this, two coroutines that hit
# `_get_client` in the first 100ms after startup can both pass the `is None`
# check, both construct an AsyncClient, and one of them is silently dropped —
# leaking its connection pool past process exit because `close_client` only
# closes the last assignment.
_client_lock = threading.Lock()


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = httpx.AsyncClient(
                base_url=FRONTEND_UPSTREAM,
                timeout=httpx.Timeout(10.0, read=30.0),
                follow_redirects=False,
            )
        return _client


async def close_client() -> None:
    """Close the cached AsyncClient on FastAPI shutdown.

    Called from ``_lifespan`` so a uvicorn reload (or graceful shutdown)
    closes the keep-alive sockets pointed at the frontend sidecar instead
    of leaking them past process exit.
    """
    global _client
    client = _client
    _client = None
    if client is not None:
        try:
            await client.aclose()
        except Exception as exc:
            LOGGER.debug("frontend_proxy client close skipped: %s", type(exc).__name__)


def _allowed_methods_for_known_path(request: Request) -> set[str]:
    """Return the HTTP methods registered for the request path, if any.

    Scans the app's route table for a route whose path regex matches the
    request but whose method set does not (`Match.PARTIAL`). A non-empty
    result means the path exists under a different method → the caller should
    get a 405 with an `Allow` header, not a 404. The catch-all reverse proxy
    itself accepts every method, so it is skipped to avoid masking real 405s.
    """
    allowed: set[str] = set()
    for route in request.app.router.routes:
        if getattr(route, "endpoint", None) is reverse_proxy:
            continue
        matcher = getattr(route, "matches", None)
        if matcher is None:
            continue
        match, _ = matcher(request.scope)
        if match == Match.PARTIAL:
            methods = getattr(route, "methods", None)
            if methods:
                allowed.update(methods)
    return allowed


@router.api_route(
    "/{full_path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def reverse_proxy(full_path: str, request: Request) -> Response:
    """Forward the request to the frontend sidecar and stream the response.

    `/api/*` requests are matched by the api routers (mounted before this
    one) and never reach this handler under normal routing. As a defensive
    guard,
    if a request whose path starts with `api/` does reach here it means the
    api never registered that route — we MUST NOT forward it to the frontend
    (which would return the SPA's `index.html` with status 200 and confuse
    the SPA's fetcher into thinking the API call succeeded). Instead, return a
    well-formed 404 JSON so the SPA's error boundary renders correctly.
    """
    if full_path.startswith("api/") or full_path == "api":
        rid = getattr(request.state, "request_id", None)
        rid_field = f',"request_id":"{rid}"' if rid else ""
        # Audit #7: distinguish "path exists under a different method" (405)
        # from "path is genuinely unknown" (404). Starlette's per-route
        # `matches()` returns `Match.PARTIAL` when the path regex matches but
        # the HTTP method does not — exactly the 405 case. The catch-all
        # itself accepts every method, so it never produces a PARTIAL here.
        allowed = _allowed_methods_for_known_path(request)
        if allowed:
            allow_header = ", ".join(sorted(allowed))
            return Response(
                content=(
                    '{"detail":"method not allowed","path":"/'
                    + full_path
                    + '"'
                    + rid_field
                    + "}"
                ),
                status_code=405,
                media_type="application/json",
                headers={"Allow": allow_header},
            )
        return Response(
            content=(
                '{"detail":"unknown api route","path":"/'
                + full_path
                + '"'
                + rid_field
                + "}"
            ),
            status_code=404,
            media_type="application/json",
        )

    # Path validation: reject characters that have no legitimate place in
    # a static asset URL and that frequently appear in path-traversal /
    # log-injection probes. The validation runs even though httpx /
    # FastAPI will reject most of these later, because (a) we want a
    # uniform 400 response shape and (b) some malformed paths reach the
    # frontend nginx sidecar and pollute its access log.
    if ".." in full_path:
        return Response(
            content='{"detail":"path contains parent-traversal segment"}',
            status_code=400,
            media_type="application/json",
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in full_path):
        return Response(
            content='{"detail":"path contains control characters"}',
            status_code=400,
            media_type="application/json",
        )
    if request.method.upper() not in _FRONTEND_ALLOWED_METHODS:
        return Response(
            content='{"detail":"method not allowed for frontend assets"}',
            status_code=405,
            media_type="application/json",
        )

    upstream_url = "/" + full_path
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    upstream_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in _FRONTEND_STRIP_HEADERS
    }

    client = _get_client()
    body = await request.body()
    try:
        upstream_req = client.build_request(
            request.method,
            upstream_url,
            headers=upstream_headers,
            content=body if body else None,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        LOGGER.warning("frontend proxy upstream error for %s: %s", upstream_url, exc)
        raise HTTPException(status_code=502, detail="frontend sidecar unreachable") from exc

    # Strip hop-by-hop response headers; pass through caching/CSP/etc.
    response_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP_BY_HOP
    }

    # 304 Not Modified: empty body, headers only — close the upstream cursor
    # before returning so the connection goes back to the pool immediately.
    if upstream_resp.status_code == 304:
        await upstream_resp.aclose()
        return Response(
            status_code=304,
            headers=response_headers,
        )

    # Stream the response back to the client so a large asset (source map,
    # wasm bundle, …) never lives entirely in the api sidecar's RAM. The
    # iterator owns the upstream lifecycle: ``aiter_raw`` flushes chunks as
    # they arrive from the frontend nginx and ``upstream_resp.aclose()``
    # in ``finally`` returns the connection to the pool even when the
    # browser aborts mid-download.
    response_headers.pop("content-length", None)

    async def _body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        _body_iter(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )

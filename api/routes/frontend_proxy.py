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

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

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


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=FRONTEND_UPSTREAM,
            timeout=httpx.Timeout(10.0, read=30.0),
            follow_redirects=False,
        )
    return _client


@router.api_route(
    "/{full_path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def reverse_proxy(full_path: str, request: Request) -> Response:
    """Forward the request to the frontend sidecar and stream the response.

    `/api/*` requests are matched by the api routers (mounted before this one)
    and never reach this handler under normal routing. As a defensive guard,
    if a request whose path starts with `api/` does reach here it means the
    api never registered that route — we MUST NOT forward it to the frontend
    (which would return the SPA's `index.html` with status 200 and confuse
    the SPA's fetcher into thinking the API call succeeded). Instead, return a
    well-formed 404 JSON so the SPA's error boundary renders correctly.
    """
    if full_path.startswith("api/") or full_path == "api":
        return Response(
            content='{"detail":"unknown api route","path":"/' + full_path + '"}',
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
    try:
        body = await request.body()
        upstream_resp = await client.request(
            request.method,
            upstream_url,
            headers=upstream_headers,
            content=body if body else None,
        )
    except httpx.RequestError as exc:
        LOGGER.warning("frontend proxy upstream error for %s: %s", upstream_url, exc)
        raise HTTPException(status_code=502, detail="frontend sidecar unreachable") from exc

    # Strip hop-by-hop response headers; pass through caching/CSP/etc.
    response_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP_BY_HOP
    }

    # 304 Not Modified: empty body, headers only.
    if upstream_resp.status_code == 304:
        return Response(
            status_code=304,
            headers=response_headers,
        )

    # Read the entire body once. We previously branched on Content-Length to
    # stream large bodies, but httpx already buffers the response when we use
    # AsyncClient.request(...) (which is non-streaming). The streaming branch
    # was therefore dead code AND was returning empty bodies for any response
    # whose Content-Length header was 0 or absent (the spec allows that for
    # chunked responses). Always return the full bytes — SPA assets are at
    # most ~1.2 MiB which fits comfortably in memory.
    body_bytes = upstream_resp.content
    # Drop any stale content-length — ASGI sets it from the actual byte length.
    response_headers.pop("content-length", None)
    return Response(
        content=body_bytes,
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )

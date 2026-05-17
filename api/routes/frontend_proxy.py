"""Catch-all reverse proxy: forwards non-/api/* requests to the frontend
sidecar at FRONTEND_UPSTREAM (default http://127.0.0.1:8081).

Streams the response body so large SPA assets do not buffer in memory.
Lives in `api/` so the SPA and the API share one origin (no CORS
preflight) and one MSAL redirect URI.
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

router = APIRouter(tags=["frontend"])

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

    upstream_url = "/" + full_path
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    upstream_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}

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

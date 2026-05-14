"""FastAPI application entrypoint for the `api` sidecar.

Routing order is significant:
  1. Specific `/api/*` route groups.
  2. Catch-all reverse proxy that forwards everything else to the
     `frontend` sidecar at 127.0.0.1:8081.
"""

from __future__ import annotations

import logging
import os
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from api_app import __version__
from api_app.routes import (
    arm,
    frontend_proxy,
    health,
    me,
    monitor,
    resources,
    stubs,
    terminal_legacy,
    terminal_ws,
)

LOGGER = logging.getLogger(__name__)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Stamp every request with an X-Request-Id (in & out) and log a one-line
    completion record. Lets us correlate SPA errors with backend traces in
    Application Insights without having to enable per-request body capture.
    """

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or secrets.token_hex(8)
        request.state.request_id = rid
        t0 = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            LOGGER.exception(
                "req_failed rid=%s method=%s path=%s elapsed=%.0fms err=%s",
                rid, request.method, request.url.path, elapsed_ms, type(exc).__name__,
            )
            raise
        elapsed_ms = (time.monotonic() - t0) * 1000
        response.headers["x-request-id"] = rid
        # Skip noisy /api/health probe logs (they fire every 10s).
        if request.url.path != "/api/health":
            LOGGER.info(
                "req rid=%s method=%s path=%s status=%d elapsed=%.0fms",
                rid, request.method, request.url.path, response.status_code, elapsed_ms,
            )
        return response


def create_app() -> FastAPI:
    app = FastAPI(
        title="ElasticBLAST Control Plane API",
        version=__version__,
        docs_url="/api/docs" if os.environ.get("ENABLE_DOCS", "false").lower() == "true" else None,
        redoc_url=None,
    )

    # Per-request id + timing logging.
    app.add_middleware(RequestIdMiddleware)

    # ---- /api/* routers (must be registered BEFORE the catch-all) ----
    app.include_router(health.router, prefix="/api")
    app.include_router(me.router, prefix="/api")
    app.include_router(monitor.router, prefix="/api/monitor")
    app.include_router(arm.router)  # carries /api/arm prefix
    app.include_router(resources.router)  # carries /api/resources prefix
    app.include_router(terminal_ws.router)  # WebSocket + ticket + health
    app.include_router(terminal_legacy.router)  # /api/terminal/{vm}/* → 410 Gone
    app.include_router(stubs.resources_router)  # legacy stub (no routes; harmless)
    app.include_router(stubs.aks_router)
    app.include_router(stubs.blast_router)
    app.include_router(stubs.warmup_router)
    app.include_router(stubs.audit_router)

    # ---- Catch-all reverse proxy to the `frontend` sidecar ----
    app.include_router(frontend_proxy.router)

    # Make sure unhandled errors return JSON rather than a traceback HTML.
    @app.exception_handler(StarletteHTTPException)
    async def http_exc_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, str):
            payload = {"detail": detail}
        else:
            payload = detail if isinstance(detail, dict) else {"detail": str(detail)}
        return JSONResponse(payload, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    LOGGER.info("api sidecar started, version=%s", __version__)
    return app


app = create_app()

"""FastAPI application entrypoint for the `api` sidecar.

Adding a route?  Read [AGENTS.md](../AGENTS.md) "Backend route map" first.

Routing order is significant:
  1. Specific `/api/*` route groups.
  2. Catch-all reverse proxy that forwards everything else to the
     `frontend` sidecar at 127.0.0.1:8081.

Any new `/api/*` router MUST be `app.include_router(...)`-ed **before** the
`frontend_proxy.router` line below — otherwise the catch-all serves
`index.html` for the new path and the route is silently shadowed.

Auth contract:
  * Every `/api/*` route validates the MSAL bearer via `Depends(require_caller)`
    in `api.auth` (except `/api/health`).
  * The WebSocket upgrade in `api.routes.terminal_ws` does the same check.
  * Azure SDK calls go through `api.services.*` under the shared user-assigned
    Managed Identity `id-elb-control` (see `.github/copilot-instructions.md` §5).
"""

from __future__ import annotations

import logging
import os
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from api import __version__
from api.routes import (
    arm,
    frontend_proxy,
    health,
    me,
    monitor,
    resources,
    storage,
    stubs,
    tasks,
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

    # Body size limit — reject payloads > 10 MiB.  Uvicorn's
    # --limit-concurrency and --limit-max-requests handle connection-level
    # limits; this catches oversized JSON bodies before they hit route
    # handlers.  Streaming uploads (query files) bypass this because they
    # use chunked transfer encoding and never buffer the full body.
    _MAX_BODY = int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(10 * 1024 * 1024)))

    @app.middleware("http")
    async def body_size_guard(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and int(cl) > _MAX_BODY:
            return JSONResponse(
                {"code": "payload_too_large", "message": f"body exceeds {_MAX_BODY} bytes"},
                status_code=413,
            )
        return await call_next(request)

    # CORS — only needed for local dev where SPA (:8090) and API (:8080)
    # run on different origins.  In production both live behind the same
    # ingress so this is a no-op.
    cors_origins = os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    cors_origins = [o.strip() for o in cors_origins if o.strip()]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ---- /api/* routers (must be registered BEFORE the catch-all) ----
    app.include_router(health.router, prefix="/api")
    app.include_router(me.router, prefix="/api")
    app.include_router(monitor.router, prefix="/api/monitor")
    app.include_router(arm.router)  # carries /api/arm prefix
    app.include_router(resources.router)  # carries /api/resources prefix
    app.include_router(storage.router)  # carries /api/storage prefix
    app.include_router(terminal_ws.router)  # WebSocket + ticket + health
    app.include_router(terminal_legacy.router)  # /api/terminal/{vm}/* → 410 Gone
    app.include_router(tasks.router)  # GET /api/tasks/{id} — Celery task status
    app.include_router(stubs.resources_router)  # legacy stub (no routes; harmless)
    app.include_router(stubs.aks_router)
    app.include_router(stubs.acr_build_router)
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

    # Background cgroup reporter — publishes this sidecar's CPU/MEM into
    # the in-revision Redis (db 2) every REPORT_INTERVAL seconds. The
    # `/api/monitor/sidecars` endpoint reads them back. Disabled when the
    # sidecar isn't running on cgroup v2 (e.g. non-Linux dev laptops).
    if os.environ.get("SIDECAR_REPORTER_DISABLED", "").lower() != "true":
        try:
            from api.services.cgroup_reporter import start_in_thread

            sidecar_name = os.environ.get("SIDECAR_NAME", "api")
            start_in_thread(sidecar_name)
        except Exception as exc:  # noqa: BLE001 — reporter must not crash startup
            LOGGER.warning("cgroup reporter not started: %s", exc)

    LOGGER.info("api sidecar started, version=%s", __version__)
    return app


app = create_app()

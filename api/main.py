"""FastAPI api sidecar entrypoint and router wiring.

Responsibility: Compose the FastAPI app from helpers in `api.app.*` and wire
every router with its prefix.
Edit boundaries: Keep this module thin — middleware logic lives in
`api.app.middleware`, lifespan in `api.app.lifespan`, inspector rules in
`api.app.inspector`. Add new routers here; do not add HTTP behaviour.
Key entry points: `create_app`, `app`. `_inspector_should_capture` is re-exported
for back-compat with tests that import it from `api.main`.
Risky contracts: Preserve the existing import surface (`app`, `create_app`,
`RequestIdMiddleware`, `_inspector_should_capture`, `_lifespan`) — both production
runners and tests rely on it.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from api import __version__

# Import celery_app eagerly so it is registered as the current/default
# Celery instance BEFORE any route handler imports `api.tasks.*` (whose
# `@shared_task` decorators bind to the current Celery app at call time).
# Without this guard, `task.delay()` resolves `current_app` to a phantom
# default Celery app and the produced message lands in a queue the worker
# doesn't subscribe to → tasks silently never run. See `api/tasks/__init__.py`.
from api import celery_app as _celery_app  # noqa: F401
from api.app.global_exception_logging import install_global_exception_hooks
from api.app.inspector import _inspector_should_capture  # noqa: F401  - back-compat re-export
from api.app.lifespan import _lifespan
from api.app.middleware import RequestIdMiddleware
from api.routes import (
    acr,
    aks,
    arm,
    audit,
    blast,
    client_log,
    elastic_blast,
    frontend_proxy,
    health,
    me,
    monitor,
    operations,
    resources,
    settings,
    storage,
    tasks,
    terminal_legacy,
    terminal_ws,
    upgrade,
    warmup,
)

LOGGER = logging.getLogger(__name__)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)

# Silence verbose third-party loggers regardless of LOG_LEVEL — at DEBUG these
# dump full HTTP request/response headers on every Azure SDK call and were the
# single biggest CPU + log-volume drain during local dev. Override with
# AZURE_LOG_LEVEL=DEBUG when you genuinely need wire-level traces.
_azure_log_level = os.environ.get("AZURE_LOG_LEVEL", "WARNING").upper()
for _name in (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.identity._internal.decorators",
    "azure.identity._credentials.default",
    "urllib3.connectionpool",
    "httpx",
    "watchfiles",
):
    logging.getLogger(_name).setLevel(_azure_log_level)


def create_app() -> FastAPI:
    install_global_exception_hooks()
    app = FastAPI(
        title="ElasticBLAST Control Plane API",
        version=__version__,
        docs_url="/api/docs" if os.environ.get("ENABLE_DOCS", "false").lower() == "true" else None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # Best-effort Azure Monitor OpenTelemetry init. No-op when
    # APPLICATIONINSIGHTS_CONNECTION_STRING is unset; safe to call before
    # routes are registered so FastAPI instrumentation sees every endpoint.
    try:
        from api.app.telemetry import init_telemetry

        init_telemetry(role=os.environ.get("SIDECAR_NAME", "api"), app=app)
    except Exception:  # pragma: no cover - defensive
        LOGGER.debug("telemetry init skipped", exc_info=True)

    # Body size limit — reject payloads > 10 MiB.  Uvicorn's
    # --limit-concurrency and --limit-max-requests handle connection-level
    # limits; this catches oversized JSON bodies before they hit route
    # handlers.  Streaming uploads (query files) bypass this because they
    # use chunked transfer encoding and never buffer the full body.
    _MAX_BODY = int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(10 * 1024 * 1024)))
    if _MAX_BODY > 100 * 1024 * 1024:
        raise ValueError("MAX_REQUEST_BODY_BYTES must be <= 100 MiB")

    @app.middleware("http")
    async def body_size_guard(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        cl = request.headers.get("content-length")
        if cl and int(cl) > _MAX_BODY:
            return JSONResponse(
                {"code": "payload_too_large", "message": f"body exceeds {_MAX_BODY} bytes"},
                status_code=413,
            )
        return await call_next(request)

    # Per-request id + timing logging. Registered after the body-size guard so
    # it wraps guard-short-circuited 413 responses as well as route responses.
    app.add_middleware(RequestIdMiddleware)

    # Per-token rate-limit for the OpenAPI BLAST submit surface. Default
    # 2000 req / 60s sliding window, keyed by `X-ELB-API-Token` (or caller
    # IP when the header is absent). Only `/api/v1/elastic-blast/*` and
    # `/api/aks/openapi/proxy` are throttled — dashboard polling, health
    # probes, monitor endpoints, etc. are unaffected.
    from api.app.openapi_rate_limit import OpenApiRateLimitMiddleware

    app.add_middleware(OpenApiRateLimitMiddleware)

    # CORS — only needed for local dev where SPA (:8090) and API (:8080)
    # run on different origins.  In production both live behind the same
    # ingress so this is a no-op.
    cors_origins = os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    cors_origins = [o.strip() for o in cors_origins if o.strip()]
    # `*` combined with `allow_credentials=True` is an OWASP-listed
    # misconfiguration: Starlette emits `Access-Control-Allow-Origin: *`
    # but browsers refuse to send credentials to a wildcard origin, so
    # the effective behaviour is broken at best and a CSRF amplifier at
    # worst (the server still echoes any request body and reads cookies
    # if downgraded to a non-credentialed flow). Refuse the combination
    # at boot — the deploy must list each trusted origin explicitly.
    if "*" in cors_origins:
        raise RuntimeError(
            "CORS_ALLOW_ORIGINS='*' is not allowed because allow_credentials=True. "
            "List the trusted origins explicitly (comma-separated)."
        )
    # ``null`` is the literal origin for sandboxed iframes / data: / file:
    # contexts. Allowing it with ``allow_credentials=True`` would let any
    # such context send authenticated requests — a textbook CSRF surface.
    if "null" in [o.lower() for o in cors_origins]:
        raise RuntimeError(
            "CORS_ALLOW_ORIGINS contains 'null' which is a sandboxed-iframe origin; "
            "this combination with allow_credentials=True is forbidden."
        )
    # Light syntactic validation: every entry must be a scheme://host string.
    # Catches typos like ``localhost:8090`` (no scheme) that would otherwise
    # silently disable CORS for the intended origin.
    for origin in cors_origins:
        if "://" not in origin or origin.endswith("://"):
            raise RuntimeError(
                f"CORS_ALLOW_ORIGINS entry {origin!r} is not a valid scheme://host origin"
            )
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
    app.include_router(elastic_blast.router)  # external /api/v1/elastic-blast facade
    app.include_router(terminal_ws.router)  # WebSocket + ticket + health
    app.include_router(terminal_legacy.router)  # /api/terminal/{vm}/* → 410 Gone
    app.include_router(tasks.router)  # GET /api/tasks/{id} — Celery task status
    app.include_router(operations.router)  # GET /api/operations/{id} — operation status
    app.include_router(aks.aks_router)
    app.include_router(acr.acr_build_router)
    app.include_router(blast.blast_router)
    app.include_router(warmup.warmup_router)
    app.include_router(audit.audit_router)
    app.include_router(client_log.router)
    app.include_router(upgrade.router)
    app.include_router(settings.settings_router)

    # ---- Catch-all reverse proxy to the `frontend` sidecar ----
    app.include_router(frontend_proxy.router)

    # Make sure unhandled errors return JSON rather than a traceback HTML.
    @app.exception_handler(StarletteHTTPException)
    async def http_exc_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, str):
            payload = {"detail": detail}
        else:
            payload = detail if isinstance(detail, dict) else {"detail": str(detail)}
        # Preserve route-supplied headers (e.g. Retry-After on 429) — Starlette
        # carries them on the exception but the default JSONResponse otherwise
        # drops them.
        headers = getattr(exc, "headers", None) or None
        return JSONResponse(payload, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors: list[dict[str, object]] = []
        for error in exc.errors():
            item = dict(error)
            ctx = item.get("ctx")
            if isinstance(ctx, dict):
                item["ctx"] = {str(key): str(value) for key, value in ctx.items()}
            errors.append(item)
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        LOGGER.exception(
            "unhandled_request_exception rid=%s method=%s path=%s err=%s",
            rid,
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        return JSONResponse(
            {"detail": "internal server error", "request_id": rid},
            status_code=500,
        )

    # Background cgroup reporter — publishes this sidecar's CPU/MEM into
    # the in-revision Redis (db 2) every REPORT_INTERVAL seconds. The
    # `/api/monitor/sidecars` endpoint reads them back. Disabled when the
    # sidecar isn't running on cgroup v2 (e.g. non-Linux dev laptops).
    if os.environ.get("SIDECAR_REPORTER_DISABLED", "").lower() != "true":
        try:
            from api.services.cgroup_reporter import start_in_thread

            sidecar_name = os.environ.get("SIDECAR_NAME", "api")
            start_in_thread(sidecar_name)
        except Exception as exc:
            LOGGER.warning("cgroup reporter not started: %s", exc)

    LOGGER.info("api sidecar started, version=%s", __version__)
    return app


app = create_app()

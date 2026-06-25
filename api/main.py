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
from typing import Any

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
from api.app.logging_config import configure_logging
from api.app.middleware import RequestIdMiddleware
from api.app.security_headers import SecurityHeadersMiddleware
from api.routes import (
    acr,
    aks,
    arm,
    audit,
    blast,
    client_log,
    diagnostics,
    elastic_blast,
    frontend_proxy,
    health,
    me,
    monitor,
    ncbi,
    notifications,
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

configure_logging()

LOGGER = logging.getLogger(__name__)


def _error_detail_text(detail: Any) -> str | None:
    """Render an exception detail into a short, sanitised span attribute.

    ``detail`` may be a string or the structured ``dict`` our routes raise
    (``{"code": ..., "message": ...}``). We prefer the ``message`` (falling
    back to ``code`` then the whole dict) and run it through ``sanitise`` so
    no token / SAS / subscription id is ever written to telemetry. Returns
    ``None`` for an empty detail so the caller skips the attribute.
    """
    from api.services.sanitise import sanitise

    if detail is None:
        return None
    if isinstance(detail, dict):
        text = str(detail.get("message") or detail.get("code") or detail)
    else:
        text = str(detail)
    cleaned = sanitise(text).strip()
    return cleaned[:512] or None


def _annotate_error_span_safe(
    *,
    status_code: int,
    error_type: str,
    detail: str | None,
    request_id: str | None,
) -> None:
    """Best-effort wrapper around ``telemetry.annotate_error_span``.

    Importing telemetry lazily keeps the OpenTelemetry dependency off the
    import path for unit tests / telemetry-disabled local runs, and the
    broad ``except`` guarantees a telemetry hiccup can never turn a clean
    4xx/5xx response into a 500.
    """
    try:
        from api.app.telemetry import annotate_error_span

        annotate_error_span(
            status_code=status_code,
            error_type=error_type,
            detail=detail,
            request_id=request_id,
        )
    except Exception:
        return


def _document_common_error_responses(schema: dict[str, Any]) -> None:
    """Augment every operation with the common error responses it can return.

    Audit #10: generated operations documented only their `200`/`422`
    responses, so a reader could not tell that any authenticated route may
    answer `401`/`403`, that path lookups may `404`, or that Azure-backed
    operations may surface a `5xx`. This registers a shared `ErrorResponse`
    schema and adds `401`/`403`/`404`/`500` entries to each operation that
    does not already declare them. It is documentation only — runtime
    behaviour is unchanged — so any explicitly-authored response wins via
    `setdefault`.
    """
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas.setdefault(
        "ErrorResponse",
        {
            "type": "object",
            "title": "ErrorResponse",
            "properties": {
                "detail": {
                    "type": "string",
                    "description": "Human-readable, sanitised error summary.",
                },
                "request_id": {
                    "type": "string",
                    "description": (
                        "Correlation id echoed from the `X-Request-ID` "
                        "response header for support and log lookup."
                    ),
                },
            },
            "required": ["detail"],
        },
    )
    error_ref = {
        "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
    }
    common = {
        "401": {
            "description": "Missing or invalid bearer token.",
            "content": error_ref,
        },
        "403": {
            "description": "Authenticated caller lacks the required role.",
            "content": error_ref,
        },
        "404": {
            "description": "Resource or route not found.",
            "content": error_ref,
        },
        "500": {
            "description": "Unexpected server or upstream Azure error.",
            "content": error_ref,
        },
    }
    paths = schema.get("paths", {})
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "put", "post", "delete", "patch"}:
                continue
            if not isinstance(operation, dict):
                continue
            responses = operation.setdefault("responses", {})
            for status_code, body in common.items():
                responses.setdefault(status_code, body)


def _install_openapi_security_scheme(app: FastAPI) -> None:
    """Declare a bearer-JWT security scheme and apply it globally in the spec.

    Every domain route enforces an MSAL bearer token at runtime, but the
    generated OpenAPI document carried no `securitySchemes`, so tooling and
    readers could not tell the API was authenticated. This injects the
    `BearerAuth` scheme and a global `security` requirement. OpenAPI `security`
    is documentation only — it never changes runtime enforcement — so the
    genuinely-anonymous probes (`/api/health*`) keep working unchanged.
    """
    from fastapi.openapi.utils import get_openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Microsoft Entra ID (MSAL) access token. Send as "
                "`Authorization: Bearer <token>`."
            ),
        }
        schema["security"] = [{"BearerAuth": []}]
        _document_common_error_responses(schema)
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def create_app() -> FastAPI:
    install_global_exception_hooks()
    # `/api/docs` (Swagger UI) and `/openapi.json` (the machine-readable spec)
    # travel together: in production `ENABLE_DOCS` is unset, so both are
    # disabled and the api never serves its internal route inventory to an
    # anonymous caller. Enabling docs locally re-exposes both. Runtime auth is
    # enforced regardless of the spec's visibility, so hiding it strips no
    # caller's access (no persona regression).
    _docs_enabled = os.environ.get("ENABLE_DOCS", "false").lower() == "true"
    app = FastAPI(
        title="ElasticBLAST Control Plane API",
        version=__version__,
        docs_url="/api/docs" if _docs_enabled else None,
        openapi_url="/openapi.json" if _docs_enabled else None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    _install_openapi_security_scheme(app)

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
        # Audit P2 #12: `allow_methods=["*"]` and `allow_headers=["*"]` echo
        # back any method or header the browser requests, which is fine
        # behind a same-origin ingress but unnecessarily generous in
        # cross-origin development. Gated behind `STRICT_CORS` per
        # charter §12a Rule 4 — default OFF preserves the existing
        # behaviour. The narrow defaults cover every method and header
        # the dashboard SPA actually sends today (GET/POST/PUT/DELETE +
        # Authorization/Content-Type/x-client-request-id); operators
        # with custom flows can override via `STRICT_CORS_ALLOW_METHODS`
        # / `STRICT_CORS_ALLOW_HEADERS` (comma-separated).
        if os.environ.get("STRICT_CORS", "").lower() == "true":
            methods_raw = os.environ.get(
                "STRICT_CORS_ALLOW_METHODS",
                "GET,POST,PUT,DELETE,OPTIONS",
            )
            headers_raw = os.environ.get(
                "STRICT_CORS_ALLOW_HEADERS",
                "Authorization,Content-Type,x-client-request-id",
            )
            allow_methods = [m.strip() for m in methods_raw.split(",") if m.strip()]
            allow_headers = [h.strip() for h in headers_raw.split(",") if h.strip()]
        else:
            allow_methods = ["*"]
            allow_headers = ["*"]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=allow_methods,
            allow_headers=allow_headers,
        )

    # Baseline security response headers (HSTS, nosniff, frame-deny, referrer,
    # permissions) + `Server` banner masking. Added last so it is the
    # outermost middleware and stamps every response — API JSON, the
    # body-size-guard 413 short-circuit, and the catch-all reverse-proxied
    # SPA assets alike. CSP stays behind the default-OFF `STRICT_CSP` gate.
    app.add_middleware(SecurityHeadersMiddleware)

    # ---- /api/* routers (must be registered BEFORE the catch-all) ----
    app.include_router(health.router, prefix="/api")
    app.include_router(me.router, prefix="/api")
    app.include_router(monitor.router, prefix="/api/monitor")
    app.include_router(ncbi.router)  # carries /api/ncbi prefix
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
    app.include_router(notifications.notifications_router)
    app.include_router(client_log.router)
    app.include_router(upgrade.router)
    app.include_router(settings.settings_router)
    app.include_router(diagnostics.router)  # carries /api/diagnostics prefix

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
        # Audit #15: stamp the per-request correlation id into the body so a
        # client that only logs the JSON (not headers) can still report it.
        # `setdefault` so a route that already carries its own `request_id`
        # in a dict detail is never clobbered. The same id is on the
        # `x-request-id` response header via RequestIdMiddleware.
        rid = getattr(_request.state, "request_id", None)
        if rid and isinstance(payload, dict):
            payload.setdefault("request_id", rid)
        # Attach the failure reason to the request span so App Insights shows
        # *why* a 4xx/5xx happened (the FastAPI instrumentor leaves 4xx spans
        # bare). Sanitise first — a detail can carry an upstream URL/SAS.
        _annotate_error_span_safe(
            status_code=exc.status_code,
            error_type=f"http_{exc.status_code}",
            detail=_error_detail_text(detail),
            request_id=rid,
        )
        # Preserve route-supplied headers (e.g. Retry-After on 429) — Starlette
        # carries them on the exception but the default JSONResponse otherwise
        # drops them.
        headers = getattr(exc, "headers", None) or None
        return JSONResponse(payload, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors: list[dict[str, object]] = []
        for error in exc.errors():
            item = dict(error)
            ctx = item.get("ctx")
            if isinstance(ctx, dict):
                item["ctx"] = {str(key): str(value) for key, value in ctx.items()}
            errors.append(item)
        rid = getattr(request.state, "request_id", None)
        body: dict[str, object] = {"detail": errors}
        if rid:
            body["request_id"] = rid
        # Surface the offending field locations (NOT the submitted values,
        # which could be sensitive) on the span so a 422 is diagnosable in
        # App Insights.
        locations = ".".join(
            str(part) for err in errors for part in (err.get("loc") or ()) if part != "body"
        )
        _annotate_error_span_safe(
            status_code=422,
            error_type="validation_error",
            detail=locations[:512] or None,
            request_id=rid,
        )
        return JSONResponse(body, status_code=422)

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
        _annotate_error_span_safe(
            status_code=500,
            error_type=type(exc).__name__,
            detail=_error_detail_text(str(exc)),
            request_id=None if rid == "-" else rid,
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

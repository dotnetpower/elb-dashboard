"""FastAPI api sidecar entrypoint and router wiring.

Responsibility: FastAPI api sidecar entrypoint and router wiring
Edit boundaries: Keep changes scoped to this module responsibility and update nearby tests.
Key entry points: `_inspector_should_capture`, `_decode_jwt_upn`, `_extract_client_ip`,
`RequestIdMiddleware`, `create_app`
Risky contracts: Keep imports lightweight and preserve existing public contracts.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from api import __version__

# Import celery_app eagerly so it is registered as the current/default
# Celery instance BEFORE any route handler imports `api.tasks.*` (whose
# `@shared_task` decorators bind to the current Celery app at call time).
# Without this guard, `task.delay()` resolves `current_app` to a phantom
# default Celery app and the produced message lands in a queue the worker
# doesn't subscribe to → tasks silently never run. See `api/tasks/__init__.py`.
from api import celery_app as _celery_app  # noqa: F401
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


# Paths excluded from BOTH the aggregate metrics buffer AND the per-request
# DETAIL inspector buffer. SSE/WebSocket cannot be safely body-buffered (the
# response never ends). High-volume self-poll paths would self-amplify.
_INSPECTOR_EXCLUDE_PREFIXES: tuple[str, ...] = (
    "/api/monitor/sidecars",  # SSE topology + high-volume snapshot
    "/api/monitor/metrics",  # would self-amplify
    "/api/monitor/sidecar-requests",  # would self-amplify
    "/api/blast/logs",  # SSE job log stream
    "/api/terminal/ws",  # WebSocket upgrade
)
_INSPECTOR_EXCLUDE_EXACT: frozenset[str] = frozenset({"/api/health"})

# High-volume polling GETs whose response body is buffered into memory by
# the middleware on every dashboard tick (30 s default, 5 s minimum). The
# inspector value of these reads is low — the dashboard refetches them
# constantly so the same payload is captured over and over, pushing more
# interesting one-shot calls (POST submit, DELETE) out of the ring buffer.
# Non-GET methods on the same paths (e.g. POST /api/blast/jobs submit)
# are still captured.
_INSPECTOR_EXCLUDE_GET_PREFIXES: tuple[str, ...] = (
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
_INSPECTOR_MAX_BUFFER_BYTES = 64 * 1024


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
    if path in _INSPECTOR_EXCLUDE_EXACT:
        return False
    if any(path.startswith(p) for p in _INSPECTOR_EXCLUDE_PREFIXES):
        return False
    if (method or "POST").upper() == "GET" and any(
        path.startswith(p) for p in _INSPECTOR_EXCLUDE_GET_PREFIXES
    ):
        return False
    return True


def _decode_jwt_upn(authz: str | None) -> str | None:
    """Best-effort caller extraction for the inspector — NOT auth.

    Just base64-decodes the JWT payload (no signature verify) and pulls
    `upn` / `preferred_username`. The route's own `require_caller`
    dependency is the real auth gate; this is for display only.
    """
    if not authz or not authz.lower().startswith("bearer "):
        return None
    parts = authz.split(" ", 1)[1].strip().split(".")
    if len(parts) < 2:
        return None
    try:
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return None
    upn = payload.get("upn") or payload.get("preferred_username")
    return str(upn)[:128] if upn else None


def _extract_client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return None


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Stamp every request with an X-Request-Id (in & out) and log a one-line
    completion record. Lets us correlate SPA errors with backend traces in
    Application Insights without having to enable per-request body capture.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        rid = request.headers.get("x-request-id") or secrets.token_hex(8)
        request.state.request_id = rid
        t0 = time.monotonic()
        path = request.url.path
        method = request.method

        # Per-request DETAIL inspector — capture up to N bytes of the
        # request body for the metrics buffer. Skipped for SSE/WebSocket/
        # health/metrics and for high-volume polling GETs (see
        # _INSPECTOR_EXCLUDE_GET_PREFIXES).
        #
        # Memory contract: the previous version did
        #   raw = await request.body()
        #   captured_request_body = raw[:_INSPECTOR_MAX_BUFFER_BYTES]
        #   request._receive = closure-over-raw
        # which kept *two* copies of the body in RAM (the captured slice
        # and the raw closure body) for the lifetime of the route. We now
        # keep a single ``raw_body`` and slice lazily only when the
        # metrics emitter actually asks for it, so multi-MB uploads
        # consume body-size + zero on the inspector side instead of 2x.
        capture = os.environ.get(
            "REQUEST_DETAIL_CAPTURE_ENABLED", "true"
        ).lower() != "false" and _inspector_should_capture(path, method)
        raw_body: bytes | None = None
        if capture and method in {"POST", "PUT", "PATCH", "DELETE"}:
            try:
                raw_body = await request.body()

                async def _replay_receive(_body: bytes = raw_body) -> dict[str, Any]:
                    return {"type": "http.request", "body": _body, "more_body": False}

                request._receive = _replay_receive  # type: ignore[attr-defined]
            except Exception:
                raw_body = None

        def _captured_body_bytes() -> bytes | None:
            if raw_body is None:
                return None
            # Slice the prefix here — single allocation, only when an
            # emitter (success or error branch) actually consumes it.
            if len(raw_body) <= _INSPECTOR_MAX_BUFFER_BYTES:
                return raw_body
            return raw_body[:_INSPECTOR_MAX_BUFFER_BYTES]

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            LOGGER.exception(
                "req_failed rid=%s method=%s path=%s elapsed=%.0fms err=%s",
                rid,
                method,
                path,
                elapsed_ms,
                type(exc).__name__,
            )
            # Record the failure so the metrics endpoint sees it as an
            # error sample. status=0 marks "exception, no response".
            try:
                from api.services.request_metrics import metrics as _metrics

                if (
                    path.startswith("/api/")
                    and path != "/api/health"
                    and not path.startswith("/api/monitor/sidecars")
                    and not path.startswith("/api/monitor/metrics")
                ):
                    _metrics().record(path=path, status=0, duration_ms=elapsed_ms)
            except Exception:  # noqa: S110
                pass
            # And surface the failure in the inspector buffer too.
            if capture:
                try:
                    from api.services.request_metrics import record_detail as _rd

                    _rd(
                        request_id=rid,
                        method=method,
                        path=path,
                        status=0,
                        duration_ms=elapsed_ms,
                        caller=_decode_jwt_upn(request.headers.get("authorization")),
                        client_ip=_extract_client_ip(request),
                        request_headers=list(request.headers.items()),
                        request_body=_captured_body_bytes(),
                        request_content_type=request.headers.get("content-type"),
                        response_headers=[],
                        response_body=None,
                        response_content_type=None,
                        response_size_bytes=None,
                    )
                except Exception:  # noqa: S110
                    pass
            raise

        elapsed_ms = (time.monotonic() - t0) * 1000

        # Buffer response body for the inspector (and rebuild the response
        # with the buffered bytes so the client still sees it). Capped so a
        # misclassified content type can't OOM us.
        captured_response_body: bytes | None = None
        captured_response_size: int | None = None
        if capture:
            try:
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > _INSPECTOR_MAX_BUFFER_BYTES:
                        # Drain the rest into a sentinel — keep client
                        # whole, but don't keep buffering for the inspector.
                        break
                # If we broke out early, drain remainder into chunks too so
                # the client still gets the full payload.
                if total > _INSPECTOR_MAX_BUFFER_BYTES:
                    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                        if isinstance(chunk, str):
                            chunk = chunk.encode("utf-8")
                        chunks.append(chunk)
                full = b"".join(chunks)
                captured_response_body = full[:_INSPECTOR_MAX_BUFFER_BYTES]
                captured_response_size = len(full)
                # Rebuild response with the buffered body. Content-Length
                # is recomputed by Starlette from the body.
                headers = dict(response.headers)
                headers.pop("content-length", None)
                response = Response(
                    content=full,
                    status_code=response.status_code,
                    headers=headers,
                    media_type=response.media_type,
                )
            except Exception:
                captured_response_body = None

        response.headers["x-request-id"] = rid
        # Skip noisy /api/health probe logs (they fire every 10s).
        if path != "/api/health":
            log_level = logging.INFO
            if response.status_code >= 500:
                log_level = logging.ERROR
            elif response.status_code >= 400:
                log_level = logging.WARNING
            LOGGER.log(
                log_level,
                "req rid=%s method=%s path=%s status=%d elapsed=%.0fms",
                rid,
                method,
                path,
                response.status_code,
                elapsed_ms,
            )
        # Record into the latency/error ring buffer.  Skip:
        #   - /api/health (10s probe, would bias rate)
        #   - /api/monitor/sidecars/* (SSE topology poll, also high-volume)
        #   - /api/monitor/metrics (would self-amplify)
        #   - non-/api paths (frontend asset proxy)
        try:
            if (
                path.startswith("/api/")
                and path != "/api/health"
                and not path.startswith("/api/monitor/sidecars")
                and not path.startswith("/api/monitor/metrics")
            ):
                from api.services.request_metrics import metrics as _metrics

                _metrics().record(
                    path=path,
                    status=response.status_code,
                    duration_ms=elapsed_ms,
                )
        except Exception:  # noqa: S110
            pass
        # Per-request DETAIL inspector record (full headers + body).
        if capture:
            try:
                from api.services.request_metrics import record_detail as _rd

                _rd(
                    request_id=rid,
                    method=method,
                    path=path,
                    status=response.status_code,
                    duration_ms=elapsed_ms,
                    caller=_decode_jwt_upn(request.headers.get("authorization")),
                    client_ip=_extract_client_ip(request),
                    request_headers=list(request.headers.items()),
                    request_body=_captured_body_bytes(),
                    request_content_type=request.headers.get("content-type"),
                    response_headers=list(response.headers.items()),
                    response_body=captured_response_body,
                    response_content_type=response.headers.get("content-type"),
                    response_size_bytes=captured_response_size,
                )
            except Exception:  # noqa: S110
                pass
        # Emit a UI animation event for the SidecarsCard topology graph.
        # Health probes and the SSE/snapshot endpoints themselves are
        # excluded so the dashboard's own polling doesn't generate
        # phantom traffic. See api.services.event_emitter.
        if path != "/api/health" and not path.startswith("/api/monitor/sidecars"):
            from api.services.event_emitter import (
                ROW_HTTP,
                ROW_TERM,
            )
            from api.services.event_emitter import (
                emit as _emit_event,
            )

            _emit_event(ROW_TERM if path.startswith("/api/terminal") else ROW_HTTP)
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan — currently only used to drain the sidecar SSE
    broadcaster cleanly on shutdown so subscribers see an EOF instead of
    a half-closed socket. See `api.routes.monitor._SidecarBroadcaster`.
    """
    # Warm the managed-identity credential at startup so the first
    # bearer-authed call (auth + Storage/Tables/AKS) does not block on a
    # cold IMDS / `az login` token fetch. The fetch happens off-loop so
    # uvicorn keeps accepting connections while the token is being
    # acquired; any failure is logged at debug only — the first real
    # request will retry the token fetch through the normal path.
    try:
        import asyncio

        from api.services import get_credential

        async def _prime() -> None:
            try:
                credential = await asyncio.to_thread(get_credential)
                # ``DefaultAzureCredential.get_token`` is the actual cold
                # path; the credential object itself is lazy.
                await asyncio.to_thread(
                    credential.get_token,
                    "https://management.azure.com/.default",
                )
            except Exception as exc:
                LOGGER.debug(
                    "credential warm-up skipped: %s", type(exc).__name__
                )

        # Keep a reference so the task is not GC'd while pending — ruff
        # RUF006 catches the "fire-and-forget without retention" pattern
        # that otherwise lets the asyncio loop cancel the task early.
        app.state._cred_warmup_task = asyncio.create_task(_prime())
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("credential warm-up scheduling skipped: %s", type(exc).__name__)
    try:
        from api.services.blast.db_metadata import (
            start_invalidate_subscriber,
            stop_invalidate_subscriber,
        )

        start_invalidate_subscriber()
    except Exception as exc:
        LOGGER.warning(
            "blast db metadata invalidate subscriber start failed: %s",
            type(exc).__name__,
        )
        stop_invalidate_subscriber = None  # type: ignore[assignment]
    try:
        yield
    finally:
        try:
            from api.routes.monitor import _SIDECAR_BROADCASTER

            await _SIDECAR_BROADCASTER.close()
        except Exception as exc:
            LOGGER.warning("sidecar broadcaster shutdown failed: %s", exc)
        try:
            from api.routes.frontend_proxy import close_client

            await close_client()
        except Exception as exc:
            LOGGER.debug("frontend_proxy close skipped: %s", type(exc).__name__)
        try:
            from api.services.httpx_pool import close_all_clients

            close_all_clients()
        except Exception as exc:
            LOGGER.debug("httpx_pool close skipped: %s", type(exc).__name__)
        if stop_invalidate_subscriber is not None:
            try:
                stop_invalidate_subscriber()
            except Exception as exc:
                LOGGER.debug(
                    "blast db metadata invalidate subscriber stop failed: %s",
                    type(exc).__name__,
                )


def create_app() -> FastAPI:
    app = FastAPI(
        title="ElasticBLAST Control Plane API",
        version=__version__,
        docs_url="/api/docs" if os.environ.get("ENABLE_DOCS", "false").lower() == "true" else None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # Body size limit — reject payloads > 10 MiB.  Uvicorn's
    # --limit-concurrency and --limit-max-requests handle connection-level
    # limits; this catches oversized JSON bodies before they hit route
    # handlers.  Streaming uploads (query files) bypass this because they
    # use chunked transfer encoding and never buffer the full body.
    _MAX_BODY = int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(10 * 1024 * 1024)))

    @app.middleware("http")
    async def body_size_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]],
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
        except Exception as exc:
            LOGGER.warning("cgroup reporter not started: %s", exc)

    LOGGER.info("api sidecar started, version=%s", __version__)
    return app


app = create_app()

"""Per-request id, timing, metrics, and inspector middleware.

Responsibility: Stamp every request with X-Request-Id, log one-line completion,
record into the latency/error ring buffer, and (when capture is enabled) buffer
request/response body into the per-request DETAIL inspector.
Edit boundaries: HTTP middleware only. Inspector-path rules live in
`api.app.inspector`; jwt/IP helpers in `api.app.jwt_utils`. Do not import
Azure SDK here — request_metrics + event_emitter are the only allowed sinks.
Key entry points: `RequestIdMiddleware`.
Risky contracts: The response `X-Request-Id` header must match the completion
log `rid` field for client/operator correlation. Body buffering is capped at
`INSPECTOR_MAX_BUFFER_BYTES`; SSE/WebSocket paths must already be excluded by
`_inspector_should_capture` because a streaming response body cannot be safely
buffered.
Validation: `uv run pytest -q api/tests/test_request_metrics_detail.py`.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from api.app.inspector import (
    INSPECTOR_MAX_BUFFER_BYTES,
    _inspector_should_capture,
    _inspector_should_record,
)
from api.app.jwt_utils import _decode_jwt_oid, _decode_jwt_upn, _extract_client_ip
from api.services.sanitise import redact_oid

LOGGER = logging.getLogger("api.app.middleware")


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

        # Per-request DETAIL inspector — always record lightweight request
        # metadata by default, but only buffer request/response bodies when
        # REQUEST_DETAIL_CAPTURE_ENABLED is explicitly true. SSE/WebSocket/
        # health/self-poll paths are skipped entirely; high-volume polling GETs
        # record metadata only.
        #
        # Memory contract: the previous version did
        #   raw = await request.body()
        #   captured_request_body = raw[:INSPECTOR_MAX_BUFFER_BYTES]
        #   request._receive = closure-over-raw
        # which kept *two* copies of the body in RAM (the captured slice
        # and the raw closure body) for the lifetime of the route. We now
        # keep a single ``raw_body`` and slice lazily only when the
        # metrics emitter actually asks for it, so multi-MB uploads
        # consume body-size + zero on the inspector side instead of 2x.
        detail_capture_setting = os.environ.get("REQUEST_DETAIL_CAPTURE_ENABLED")
        detail_capture_value = (detail_capture_setting or "metadata").lower()
        record_detail_sample = (
            detail_capture_value not in {"0", "false", "no", "off"}
            and _inspector_should_record(path)
        )
        capture = (
            record_detail_sample
            and detail_capture_value in {"1", "true", "yes", "on"}
            and _inspector_should_capture(path, method)
        )
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
            if len(raw_body) <= INSPECTOR_MAX_BUFFER_BYTES:
                return raw_body
            return raw_body[:INSPECTOR_MAX_BUFFER_BYTES]

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            # Audit P3 #26: hash the caller's `oid` claim into the
            # completion line so log shippers / KQL can count per-user
            # traffic without inspecting raw OIDs. `redact_oid(None)`
            # returns None — formatted as the literal `None` by the
            # logger — so anonymous (no-bearer) requests still produce a
            # parseable token. Best-effort; never raises.
            caller_hash = redact_oid(
                _decode_jwt_oid(request.headers.get("authorization"))
            )
            LOGGER.exception(
                "req_failed rid=%s method=%s path=%s elapsed=%.0fms err=%s caller_hash=%s",
                rid,
                method,
                path,
                elapsed_ms,
                type(exc).__name__,
                caller_hash,
            )
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
            if record_detail_sample:
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
                    if total > INSPECTOR_MAX_BUFFER_BYTES:
                        break
                if total > INSPECTOR_MAX_BUFFER_BYTES:
                    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                        if isinstance(chunk, str):
                            chunk = chunk.encode("utf-8")
                        chunks.append(chunk)
                full = b"".join(chunks)
                captured_response_body = full[:INSPECTOR_MAX_BUFFER_BYTES]
                captured_response_size = len(full)
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
        if path != "/api/health":
            log_level = logging.INFO
            if response.status_code >= 500:
                log_level = logging.ERROR
            elif response.status_code >= 400:
                log_level = logging.WARNING
            # Audit P3 #26: same `caller_hash` token as the failure path
            # so success / failure can join on the same field. `redact_oid`
            # returns `None` for anonymous requests, which formats as the
            # literal `None` — operators get a consistent token shape.
            caller_hash = redact_oid(
                _decode_jwt_oid(request.headers.get("authorization"))
            )
            LOGGER.log(
                log_level,
                "req rid=%s method=%s path=%s status=%d elapsed=%.0fms caller_hash=%s",
                rid,
                method,
                path,
                response.status_code,
                elapsed_ms,
                caller_hash,
            )
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
        if record_detail_sample:
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

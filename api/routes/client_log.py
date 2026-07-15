"""Browser client error log ingestion route.

Responsibility: Browser client error log ingestion route
Edit boundaries: Keep HTTP validation and log shaping here; do not persist user data or call
Azure SDKs.
Key entry points: `ClientLogPayload`, `client_log`
Risky contracts: Auth-gated by default; the `ALLOW_ANONYMOUS_CLIENT_LOG`
default-OFF guard opts into accepting unauthenticated pre-login error reports.
Always sanitise client-controlled text before logging and cap payload sizes.
Validation: `uv run pytest -q api/tests/test_client_log.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, Field

from api.auth import AuthError, CallerIdentity, require_caller
from api.services.sanitise import redact_oid, sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/client-log", tags=["client-log"])


class ClientLogPayload(BaseModel):
    """Client-controlled browser error report with tight field caps."""

    level: Literal["error", "warning", "info"] = "error"
    source: str = Field(default="browser", min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1000)
    stack: str | None = Field(default=None, max_length=6000)
    component_stack: str | None = Field(default=None, max_length=6000)
    url: str | None = Field(default=None, max_length=2048)
    user_agent: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, max_length=64)


def _one_line(value: str | None, *, limit: int) -> str:
    cleaned = sanitise(value or "")[:limit]
    return " ".join(cleaned.split())


def _strict_redaction_enabled() -> bool:
    return os.environ.get("STRICT_CLIENT_LOG_REDACTION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _caller_label(caller: CallerIdentity | None) -> str:
    if caller is None:
        return "anonymous"
    if _strict_redaction_enabled():
        return redact_oid(caller.object_id) or "anonymous"
    return caller.upn or caller.object_id


def _safe_client_url(value: str | None) -> str:
    if not value or not _strict_redaction_enabled():
        return _one_line(value, limit=512)
    try:
        parsed = urlsplit(value)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        return _one_line(f"{origin}{parsed.path}", limit=512)
    except ValueError:
        return _one_line(value.split("?", 1)[0].split("#", 1)[0], limit=512)


async def _client_log_caller(
    authorization: str | None = Header(default=None),
    x_elb_api_token: str | None = Header(default=None, alias="X-ELB-API-Token"),
) -> CallerIdentity | None:
    """Resolve the caller, optionally tolerating anonymous reports.

    Audit #14: pre-login browser failures (MSAL redirect/login errors) carry
    no bearer token, so an auth-required route silences exactly the telemetry
    an operator most wants. Behind the default-OFF `ALLOW_ANONYMOUS_CLIENT_LOG`
    guard (charter §12a Rule 4 — default OFF preserves the auth-required
    contract) the route accepts a missing/invalid token and logs the report as
    `caller=anonymous`. A valid token, when present, is still honoured so
    authenticated reports keep their caller label.

    ``x_elb_api_token`` is forwarded so an M2M automation caller can post a
    client-log entry with the shared token (universal M2M path — see
    :func:`api.auth.require_caller`).
    """
    if os.environ.get("ALLOW_ANONYMOUS_CLIENT_LOG", "").lower() != "true":
        return await require_caller(
            authorization=authorization, x_elb_api_token=x_elb_api_token
        )
    if not authorization and not x_elb_api_token:
        return None
    try:
        return await require_caller(
            authorization=authorization, x_elb_api_token=x_elb_api_token
        )
    except AuthError:
        return None


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
def client_log(
    payload: ClientLogPayload,
    response: Response,
    caller: CallerIdentity | None = Depends(_client_log_caller),
) -> Response:
    """Write a browser-side app error into the api sidecar log stream."""

    log_level = {
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "info": logging.INFO,
    }[payload.level]
    caller_label = _caller_label(caller)
    LOGGER.log(
        log_level,
        "client_app_%s source=%s caller=%s url=%s client_request_id=%s "
        "message=%s stack=%s component_stack=%s user_agent=%s",
        payload.level,
        _one_line(payload.source, limit=64),
        _one_line(caller_label, limit=128),
        _safe_client_url(payload.url),
        _one_line(payload.request_id, limit=64),
        _one_line(payload.message, limit=1000),
        _one_line(payload.stack, limit=2000),
        _one_line(payload.component_stack, limit=2000),
        _one_line(payload.user_agent, limit=256),
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response

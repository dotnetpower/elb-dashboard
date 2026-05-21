"""Browser client error log ingestion route.

Responsibility: Browser client error log ingestion route
Edit boundaries: Keep HTTP validation and log shaping here; do not persist user data or call
Azure SDKs.
Key entry points: `ClientLogPayload`, `client_log`
Risky contracts: Keep the route auth-gated, sanitise client-controlled text before logging, and
cap payload sizes.
Validation: `uv run pytest -q api/tests/test_client_log.py`.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from api.auth import CallerIdentity, require_caller
from api.services.sanitise import sanitise

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


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
def client_log(
    payload: ClientLogPayload,
    response: Response,
    caller: CallerIdentity = Depends(require_caller),
) -> Response:
    """Write a browser-side app error into the api sidecar log stream."""

    log_level = {
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "info": logging.INFO,
    }[payload.level]
    caller_label = caller.upn or caller.object_id
    LOGGER.log(
        log_level,
        "client_app_%s source=%s caller=%s url=%s client_request_id=%s "
        "message=%s stack=%s component_stack=%s user_agent=%s",
        payload.level,
        _one_line(payload.source, limit=64),
        _one_line(caller_label, limit=128),
        _one_line(payload.url, limit=512),
        _one_line(payload.request_id, limit=64),
        _one_line(payload.message, limit=1000),
        _one_line(payload.stack, limit=2000),
        _one_line(payload.component_stack, limit=2000),
        _one_line(payload.user_agent, limit=256),
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response

"""Live Wall sidecar log routes.

Responsibility: Issue short-lived log stream tickets and expose sanitized
  recent/SSE log tails for the six control-plane sidecars.
Edit boundaries: Keep file reading and redaction in `api.services.sidecar_logs`;
  this module owns HTTP auth, validation, and SSE framing only.
Key entry points: `logs_ticket`, `logs_recent`, `logs_events`
Risky contracts: Browser EventSource cannot send bearer headers, so SSE access
  must stay ticket-based and tickets must be single-use.
Validation: `uv run pytest -q api/tests/test_sidecar_logs.py`.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from api.auth import CallerIdentity, require_caller
from api.services import sse_ticket
from api.services.sidecar_logs import (
    SIDECAR_CONTAINERS,
    SidecarContainer,
    end_offset,
    read_lines_since,
    read_recent_lines,
)

router = APIRouter()

_LOG_TICKET_TTL_SEC = 30
_LOG_POLL_INTERVAL_SEC = 1.0
_LOG_HEARTBEAT_INTERVAL_SEC = 25.0


@dataclass(frozen=True)
class _LogTicket:
    owner_oid: str
    expires_at: float
    # Audit P0 #2: optional client-binding hashes captured at issue time —
    # see `api/services/sse_ticket.py` for the strict-mode contract.
    ip_hash: str | None = None
    ua_hash: str | None = None


_log_tickets: dict[str, _LogTicket] = {}
_log_tickets_lock = asyncio.Lock()


@router.post("/logs/ticket")
async def logs_ticket(
    request: Request, caller: CallerIdentity = Depends(require_caller)
) -> dict[str, object]:
    """Validate the bearer and issue a single-use SSE ticket.

    When `STRICT_SSE_TICKET_BINDING=true` (audit P0 #2 #3) the issue path
    also rejects foreign Origins with 403 and captures hashed client IP +
    User-Agent on the ticket. Default OFF preserves legacy behaviour per
    charter §12a Rule 4.
    """
    sse_ticket.enforce_issue_origin(request)
    token = secrets.token_urlsafe(24)
    now = time.time()
    async with _log_tickets_lock:
        for key in [key for key, value in _log_tickets.items() if value.expires_at <= now]:
            _log_tickets.pop(key, None)
        _log_tickets[token] = _LogTicket(
            owner_oid=caller.object_id,
            expires_at=now + _LOG_TICKET_TTL_SEC,
            ip_hash=sse_ticket.client_ip_hash(request),
            ua_hash=sse_ticket.user_agent_hash(request),
        )
    return {"ticket": token, "expires_at": int(now + _LOG_TICKET_TTL_SEC)}


@router.get("/logs/{container}/recent")
async def logs_recent(
    container: str,
    tail: int = Query(default=200, ge=1, le=2_000),
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, object]:
    """Return a bounded sanitized recent tail for one sidecar."""
    sidecar = _parse_container(container)
    lines = await asyncio.to_thread(read_recent_lines, sidecar, tail=tail)
    return {"container": sidecar, "lines": lines}


@router.get("/logs/{container}/events")
async def logs_events(
    container: str,
    request: Request,
    ticket: str | None = Query(default=None),
) -> Response:
    """Server-Sent Events stream for one sidecar log tail.

    Ticket gating returns **HTTP 204** (not 401) when the ticket is
    missing, already consumed, or expired. Per the HTML spec, browsers
    must NOT auto-reconnect EventSource on 204, which terminates the
    phantom retry loop after an SSE drop: the browser closes the
    connection cleanly, the frontend's onerror handler fires and runs
    its own bounded retry path with a fresh ticket. The previous 401
    response triggered native EventSource auto-retry against the same
    URL whose ticket had just been popped, generating 1+ phantom 401
    per drop and flooding App Insights with red Dependency failures.
    """
    sidecar = _parse_container(container)
    if (await _consume_log_ticket(ticket, request)) is None:
        return Response(status_code=204)

    async def event_stream() -> AsyncGenerator[str, None]:
        yield ": ready\n\n"
        for line in await asyncio.to_thread(read_recent_lines, sidecar, tail=60):
            yield _sse("line", line)
        offset = await asyncio.to_thread(end_offset, sidecar)
        last_heartbeat = time.monotonic()
        while True:
            lines, offset = await asyncio.to_thread(read_lines_since, sidecar, offset)
            for line in lines:
                yield _sse("line", line)
            now = time.monotonic()
            if now - last_heartbeat >= _LOG_HEARTBEAT_INTERVAL_SEC:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            await asyncio.sleep(_LOG_POLL_INTERVAL_SEC)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _consume_log_ticket(
    token: str | None, request: Request | None = None
) -> _LogTicket | None:
    """Pop and validate a logs SSE ticket. Returns None if missing/invalid/expired.

    Callers that surface a hard error (e.g. tests, future authenticated
    paths) should compare for ``None`` and respond with their own status
    code. The SSE route uses 204 — see ``logs_events`` for the reason.

    When `STRICT_SSE_TICKET_BINDING=true` the function additionally
    rejects tickets whose IP / User-Agent hashes do not match the values
    captured at issue time. `request` is optional only so legacy callers
    keep working when binding is off; production routes always pass it.
    """
    if not token:
        return None
    async with _log_tickets_lock:
        entry = _log_tickets.pop(token, None)
    if entry is None:
        return None
    if entry.expires_at <= time.time():
        return None
    if request is not None and not sse_ticket.binding_matches(
        request=request,
        ticket_ip_hash=entry.ip_hash,
        ticket_ua_hash=entry.ua_hash,
    ):
        return None
    return entry


def _parse_container(container: str) -> SidecarContainer:
    if container not in SIDECAR_CONTAINERS:
        raise HTTPException(404, "unknown sidecar container")
    return cast(SidecarContainer, container)


def _sse(event: str, payload: object) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"

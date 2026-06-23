"""Real-time "jobs changed" SSE route (Message Flow / Blast Jobs / AKS Jobs).

Responsibility: Issue short-lived single-use SSE tickets and stream a
    ``jobs-changed`` Server-Sent Event to the browser the instant any job row
    changes, so the Message Flow card, the Blast Jobs list, and the AKS card
    Jobs all refetch without waiting out their poll interval. The events come
    from ``api.services.jobs_events_bus``, which is fed by the same cache-
    invalidation funnel every job producer already calls — so this works whether
    or not the Service Bus integration is enabled (a direct dashboard submit
    fires the same event as a queue drain).
Edit boundaries: HTTP auth, ticket lifecycle, and SSE framing only. The fan-out
    registry lives in ``api.services.jobs_events_bus``; do NOT add
    ``Depends(require_caller)`` to the stream endpoint — EventSource cannot send
    bearer headers, so it stays ticket-gated (charter §12a Rule 5).
Key entry points: ``jobs_events_ticket``, ``jobs_events_stream``.
Risky contracts: Default-ON with an env kill-switch ``JOBS_EVENTS_SSE_DISABLED``
    — this is an additive UX feature (polling is the guaranteed fallback, so it
    never revokes access), not a security guard, so charter §12a Rule 4's
    default-OFF discipline does not bind it; setting the kill-switch makes the
    ticket endpoint return ``{"enabled": false}`` and the stream return 204 so
    the browser falls back to polling. Tickets are single-use, TTL ≤ 30s, and
    bound to the caller's IP + User-Agent under ``STRICT_SSE_TICKET_BINDING``
    (same contract as the logs SSE). The stream endpoint returns 204 (not 401)
    on a missing/expired/consumed ticket so EventSource stops auto-reconnecting.
Validation: ``uv run pytest -q api/tests/test_jobs_events_route.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse

from api.auth import CallerIdentity, require_caller
from api.services import jobs_events_bus, sse_ticket

router = APIRouter()

_TICKET_TTL_SEC = 30
_HEARTBEAT_INTERVAL_SEC = 25.0
_ON_VALUES = {"1", "true", "yes", "on"}


def _sse_enabled() -> bool:
    """Default-ON with a kill-switch. Read at call time for tests.

    This is a purely additive UX feature (polling is the guaranteed fallback, so
    it can never revoke access — charter §12a Rule 4's default-OFF discipline
    targets security *guards*, not features) and the SSE transport is already
    proven by the logs/sidecars streams in the same topology, so it ships ON.
    Set ``JOBS_EVENTS_SSE_DISABLED=true`` to turn it off (env kill-switch) if an
    operator ever needs to shed the always-on connections.
    """
    return os.environ.get("JOBS_EVENTS_SSE_DISABLED", "").strip().lower() not in _ON_VALUES


@dataclass(frozen=True)
class _Ticket:
    owner_oid: str
    expires_at: float
    ip_hash: str | None = None
    ua_hash: str | None = None


_tickets: dict[str, _Ticket] = {}
_tickets_lock = asyncio.Lock()


@router.post("/jobs-events/ticket")
async def jobs_events_ticket(
    request: Request, caller: CallerIdentity = Depends(require_caller)
) -> dict[str, object]:
    """Validate the bearer and issue a single-use SSE ticket.

    Returns ``{"enabled": false}`` when the feature gate is off so the SPA keeps
    polling and never opens the stream. When on, mirrors the logs ticket: origin
    check + IP/UA binding captured under ``STRICT_SSE_TICKET_BINDING``.
    """
    if not _sse_enabled():
        return {"enabled": False}
    sse_ticket.enforce_issue_origin(request)
    token = secrets.token_urlsafe(24)
    now = time.time()
    async with _tickets_lock:
        for key in [k for k, v in _tickets.items() if v.expires_at <= now]:
            _tickets.pop(key, None)
        _tickets[token] = _Ticket(
            owner_oid=caller.object_id,
            expires_at=now + _TICKET_TTL_SEC,
            ip_hash=sse_ticket.client_ip_hash(request),
            ua_hash=sse_ticket.user_agent_hash(request),
        )
    return {"enabled": True, "ticket": token, "expires_at": int(now + _TICKET_TTL_SEC)}


@router.get("/jobs-events")
async def jobs_events_stream(
    request: Request, ticket: str | None = Query(default=None)
) -> Response:
    """Server-Sent Events stream that emits ``jobs-changed`` on any job row change.

    Returns 204 when the gate is off or the ticket is missing/expired/consumed —
    per the HTML spec the browser does NOT auto-reconnect EventSource on 204, so
    the SPA's own bounded retry path (with a fresh ticket) drives reconnection
    instead of a phantom native retry loop.
    """
    if not _sse_enabled():
        return Response(status_code=204)
    if (await _consume_ticket(ticket, request)) is None:
        return Response(status_code=204)

    sub = jobs_events_bus.register()

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            yield ": ready\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        sub.queue.get(), timeout=_HEARTBEAT_INTERVAL_SEC
                    )
                except TimeoutError:
                    # Idle keepalive — also surfaces a broken pipe so the finally
                    # below can prune this subscriber promptly.
                    yield ": heartbeat\n\n"
                    continue
                yield _sse("jobs-changed", event)
        finally:
            jobs_events_bus.unregister(sub)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _consume_ticket(
    token: str | None, request: Request | None = None
) -> _Ticket | None:
    """Pop and validate a jobs-events ticket. Returns None if missing/invalid/expired.

    Single-use (popped on first read). Under ``STRICT_SSE_TICKET_BINDING`` the
    IP/UA hashes must match the values captured at issue time (same contract as
    the logs SSE).
    """
    if not token:
        return None
    async with _tickets_lock:
        entry = _tickets.pop(token, None)
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


def _sse(event: str, payload: object) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"

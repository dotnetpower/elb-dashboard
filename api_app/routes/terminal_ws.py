"""WebSocket proxy for the browser terminal session.

Browser opens `wss://<api-host>/api/terminal/ws?token=<one-time-ticket>` (the
token is acquired from `POST /api/terminal/ticket`, which validates the
caller's MSAL bearer token and stores a short-lived single-use ticket in
process memory). Then this handler:

1. Validates the ticket.
2. Opens a WebSocket connection to the terminal sidecar's loopback
   `ttyd` at TERMINAL_UPSTREAM/ws.
3. Duplex-copies bytes between the browser WebSocket and the upstream
   WebSocket until either side closes.

The ticket flow is necessary because browser WebSocket APIs cannot send
custom Authorization headers (see https://github.com/whatwg/websockets/issues/16).
The api validates the bearer once via the regular HTTP path
(`POST /api/terminal/ticket` requires a valid token) and exchanges it for a
ticket the browser can then put in the URL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from api_app.auth import CallerIdentity, require_caller

LOGGER = logging.getLogger(__name__)

TERMINAL_UPSTREAM = os.environ.get("TERMINAL_UPSTREAM", "http://127.0.0.1:7681")
TICKET_TTL_SECONDS = 30  # Browser must redeem within 30 s.

router = APIRouter(prefix="/api/terminal", tags=["terminal"])


@dataclass(frozen=True)
class _Ticket:
    owner_oid: str
    expires_at: float


# Process-local one-shot ticket store. Container Apps replica is pinned
# to 1, so this is safe; if scale-out is ever introduced this must move
# to Redis.
_tickets: dict[str, _Ticket] = {}
_tickets_lock = asyncio.Lock()


@router.post("/ticket")
async def issue_ticket(caller: CallerIdentity = Depends(require_caller)) -> dict[str, object]:
    """Validate the bearer token and return a short-lived WebSocket ticket."""
    token = secrets.token_urlsafe(24)
    async with _tickets_lock:
        # Reap expired tickets opportunistically.
        now = time.time()
        for k in [k for k, v in _tickets.items() if v.expires_at < now]:
            _tickets.pop(k, None)
        _tickets[token] = _Ticket(
            owner_oid=caller.object_id,
            expires_at=now + TICKET_TTL_SECONDS,
        )
    return {"ticket": token, "ttl_seconds": TICKET_TTL_SECONDS}


async def _consume_ticket(token: str | None) -> _Ticket:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ticket required")
    async with _tickets_lock:
        entry = _tickets.pop(token, None)
    if entry is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired ticket")
    if entry.expires_at < time.time():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ticket expired")
    return entry


@router.get("/health")
async def terminal_health() -> dict[str, object]:
    """Cheap reachability check for the terminal sidecar's loopback ttyd."""
    upstream = TERMINAL_UPSTREAM.replace("ws://", "http://").replace("wss://", "https://")
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(upstream + "/")
        return {"status": "ok" if r.status_code < 500 else "degraded", "upstream_status": r.status_code}
    except httpx.RequestError as exc:
        return {"status": "down", "error": str(exc)[:120]}


@router.websocket("/ws")
async def ws_terminal(
    websocket: WebSocket,
    ticket: str | None = Query(default=None),
) -> None:
    """Proxy the browser <-> ttyd WebSocket after ticket validation.

    Once accepted, this duplex-copies bytes in both directions until either
    side closes. The api process never inspects the bytes (raw terminal
    stream).
    """
    try:
        await _consume_ticket(ticket)
    except HTTPException as exc:
        # Cannot send a 401 over WebSocket; close with policy violation.
        await websocket.close(code=4401, reason=exc.detail)
        return

    await websocket.accept(subprotocol="tty")  # ttyd uses "tty" subprotocol.

    upstream_url = TERMINAL_UPSTREAM.rstrip("/").replace("http://", "ws://").replace("https://", "wss://") + "/ws"

    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=["tty"],
            ping_interval=20,
            ping_timeout=20,
            max_size=None,  # ttyd may send large frames for screen redraws.
        ) as upstream:
            async def b2u() -> None:
                """Browser -> ttyd."""
                try:
                    while True:
                        msg = await websocket.receive()
                        # FastAPI message dict: {"type": "websocket.receive", "bytes" | "text"}
                        if msg.get("type") == "websocket.disconnect":
                            return
                        if "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                        elif "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                except (WebSocketDisconnect, websockets.ConnectionClosed):
                    return

            async def u2b() -> None:
                """ttyd -> browser."""
                try:
                    async for frame in upstream:
                        if isinstance(frame, bytes):
                            await websocket.send_bytes(frame)
                        else:
                            await websocket.send_text(frame)
                except (WebSocketDisconnect, websockets.ConnectionClosed):
                    return

            await asyncio.gather(b2u(), u2b(), return_exceptions=True)
    except Exception as exc:
        LOGGER.warning("terminal proxy upstream error: %s", exc)
        try:
            await websocket.close(code=1011, reason="upstream error")
        except Exception:
            pass
        return

    try:
        await websocket.close()
    except Exception:
        pass

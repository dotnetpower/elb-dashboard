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
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from api.auth import CallerIdentity, require_caller

LOGGER = logging.getLogger(__name__)

TERMINAL_UPSTREAM = os.environ.get("TERMINAL_UPSTREAM", "http://127.0.0.1:7681")
TICKET_TTL_SECONDS = 30  # Browser must redeem within 30 s.

router = APIRouter(prefix="/api/terminal", tags=["terminal"])
REQUIRE_CALLER = Depends(require_caller)


@dataclass(frozen=True)
class _Ticket:
    owner_oid: str
    owner_upn: str | None
    session_id: str
    expires_at: float


# Process-local one-shot ticket store. Container Apps replica is pinned
# to 1, so this is safe; if scale-out is ever introduced this must move
# to Redis.
_tickets: dict[str, _Ticket] = {}
_tickets_lock = asyncio.Lock()


def _log_identity_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@router.post("/ticket")
async def issue_ticket(caller: CallerIdentity = REQUIRE_CALLER) -> dict[str, object]:
    """Validate the bearer token and return a short-lived WebSocket ticket."""
    token = secrets.token_urlsafe(24)
    session_id = secrets.token_hex(6)
    async with _tickets_lock:
        # Reap expired tickets opportunistically.
        now = time.time()
        for k in [k for k, v in _tickets.items() if v.expires_at <= now]:
            _tickets.pop(k, None)
        _tickets[token] = _Ticket(
            owner_oid=caller.object_id,
            owner_upn=caller.upn,
            session_id=session_id,
            expires_at=now + TICKET_TTL_SECONDS,
        )
    return {
        "ticket": token,
        "ttl_seconds": TICKET_TTL_SECONDS,
        "session_id": session_id,
        "caller": {
            "display_name": caller.upn or caller.object_id,
            "upn": caller.upn,
        },
        "shell_user": os.environ.get("TERMINAL_SHELL_USER", "azureuser"),
    }


async def _consume_ticket(token: str | None) -> _Ticket:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ticket required")
    async with _tickets_lock:
        entry = _tickets.pop(token, None)
    if entry is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired ticket")
    if entry.expires_at <= time.time():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ticket expired")
    return entry


@router.get("/health")
async def terminal_health() -> dict[str, object]:
    """Cheap reachability check for the terminal sidecar's loopback ttyd."""
    upstream = TERMINAL_UPSTREAM.replace("ws://", "http://").replace("wss://", "https://")
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(upstream + "/")
        return {
            "status": "ok" if r.status_code < 500 else "degraded",
            "upstream_status": r.status_code,
        }
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
        ticket_entry = await _consume_ticket(ticket)
    except HTTPException as exc:
        # Cannot send a 401 over WebSocket; close with policy violation.
        await websocket.close(code=4401, reason=exc.detail)
        return

    upstream_url = (
        TERMINAL_UPSTREAM.rstrip("/")
        .replace("http://", "ws://")
        .replace("https://", "wss://")
        + "/ws"
    )

    # Retry upstream connect briefly to absorb the DNS / port-not-yet-listening
    # race that always follows a `terminal` sidecar restart (compose's embedded
    # DNS takes ~1 s to publish the new container IP). Without this the very
    # first reconnect after `docker compose restart terminal` returns 403 to
    # the browser, which then has to wait through another full backoff cycle.
    upstream = None
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            upstream = await websockets.connect(
                upstream_url,
                subprotocols=["tty"],
                ping_interval=20,
                ping_timeout=20,
                open_timeout=4,
                max_size=None,  # ttyd may send large frames for screen redraws.
            )
            break
        except (OSError, TimeoutError, websockets.InvalidHandshake) as exc:
            last_exc = exc
            # 0.2s, 0.4s, 0.8s — total ~1.4 s before giving up.
            await asyncio.sleep(0.2 * (2**attempt))
        except Exception as exc:  # non-transient (e.g. invalid URL) — give up
            last_exc = exc
            break

    if upstream is None:
        LOGGER.warning(
            "terminal proxy upstream connect failed after retries: %s", last_exc
        )
        try:
            await websocket.close(code=1011, reason="upstream unavailable")
        except Exception as close_exc:
            LOGGER.debug("terminal proxy close after connect failure failed: %s", close_exc)
        return

    await websocket.accept(subprotocol="tty")  # ttyd uses "tty" subprotocol.
    LOGGER.info(
        "terminal session connected session_id=%s owner_hash=%s upn_hash=%s",
        ticket_entry.session_id,
        _log_identity_hash(ticket_entry.owner_oid),
        _log_identity_hash(ticket_entry.owner_upn),
    )

    # Track which side closed first so we don't try to close a half that
    # already shut itself down (which raises a benign-but-noisy ASGI error).
    browser_closed = False
    upstream_closed = False

    try:
        async def b2u() -> None:
            """Browser -> ttyd."""
            nonlocal browser_closed
            try:
                while True:
                    msg = await websocket.receive()
                    # FastAPI message dict: {"type": "websocket.receive", "bytes" | "text"}
                    if msg.get("type") == "websocket.disconnect":
                        browser_closed = True
                        return
                    if "bytes" in msg and msg["bytes"] is not None:
                        await upstream.send(msg["bytes"])
                    elif "text" in msg and msg["text"] is not None:
                        await upstream.send(msg["text"])
            except (WebSocketDisconnect, websockets.ConnectionClosed):
                browser_closed = True
                return

        async def u2b() -> None:
            """ttyd -> browser."""
            nonlocal upstream_closed
            try:
                async for frame in upstream:
                    if isinstance(frame, bytes):
                        await websocket.send_bytes(frame)
                    else:
                        await websocket.send_text(frame)
            except (WebSocketDisconnect, websockets.ConnectionClosed):
                upstream_closed = True
                return
            upstream_closed = True

        forward_tasks = {
            asyncio.create_task(b2u()),
            asyncio.create_task(u2b()),
        }
        done, pending = await asyncio.wait(
            forward_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled() and (task_exception := task.exception()) is not None:
                raise task_exception
    except Exception as exc:
        LOGGER.warning("terminal proxy upstream error: %s", exc)
        if not browser_closed:
            try:
                await websocket.close(code=1011, reason="upstream error")
            except Exception as close_exc:
                LOGGER.debug("terminal proxy close after upstream error failed: %s", close_exc)
        return
    finally:
        if not upstream_closed:
            try:
                await upstream.close()
            except Exception as close_exc:
                LOGGER.debug("terminal proxy upstream final close failed: %s", close_exc)

    if not browser_closed:
        try:
            await websocket.close()
        except Exception as close_exc:
            LOGGER.debug("terminal proxy final close failed: %s", close_exc)

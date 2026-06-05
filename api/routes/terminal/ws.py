"""Browser terminal ticketing and WebSocket proxy routes.

Responsibility: Browser terminal ticketing and WebSocket proxy routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_Ticket`, `_log_identity_hash`, `issue_ticket`, `terminal_health`,
`terminal_azure_cli`, `ws_terminal`
Risky contracts: Terminal access must stay bearer/ticket-gated and upstreams must remain
loopback-only.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
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
from api.services.sanitise import redact_oid

LOGGER = logging.getLogger(__name__)

TERMINAL_UPSTREAM = os.environ.get("TERMINAL_UPSTREAM", "http://127.0.0.1:7681")
TICKET_TTL_SECONDS = 30  # Browser must redeem within 30 s.

# CSWSH (Cross-Site WebSocket Hijacking) defence. The browser's `Origin` header
# is set automatically and cannot be forged from a malicious page. We refuse
# any WebSocket upgrade whose origin is not in the allowlist below. The
# allowlist defaults to "same-origin only" — i.e. an empty list means the
# Origin must equal the request's Host. Operators can opt into specific
# additional origins (e.g. preview Container App URLs) via
# ``TERMINAL_WS_ALLOWED_ORIGINS=https://a.example,https://b.example``.
_ALLOWED_ORIGINS_RAW = os.environ.get("TERMINAL_WS_ALLOWED_ORIGINS", "").strip()
_ALLOWED_ORIGINS: frozenset[str] = frozenset(
    o.strip().rstrip("/") for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()
)
# Permissive bypass — set to "true" only in tests / local dev where the
# browser may be on a different localhost port than the api.
#
# Audit P0 #4: when `CONTAINER_APP_NAME` is set (the platform always exports
# this in a deployed Container Apps revision), the bypass is force-disabled
# even if `TERMINAL_WS_ALLOW_ANY_ORIGIN=true` slipped in through a stale env
# import. This closes the CSWSH escape hatch in production without breaking
# local debugging.
_TERMINAL_WS_ALLOW_ANY_ORIGIN = (
    os.environ.get("TERMINAL_WS_ALLOW_ANY_ORIGIN", "").lower() == "true"
    and not os.environ.get("CONTAINER_APP_NAME")
)


def _origin_allowed(websocket: WebSocket) -> bool:
    """Return True if the WebSocket's Origin header passes the CSWSH check."""
    if _TERMINAL_WS_ALLOW_ANY_ORIGIN:
        return True
    origin = (websocket.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        # Native (non-browser) clients (e.g. curl, python websockets test
        # helpers) won't send an Origin header. We still require the ticket,
        # which is bearer-validated, so this is safe.
        return True
    if origin in _ALLOWED_ORIGINS:
        return True
    # Same-origin fallback — compare against the request's own scheme + host.
    host = (websocket.headers.get("host") or "").strip().lower()
    if host:
        # Browser only sends one Host; we accept both http and https for it
        # because the api sidecar runs behind the Container App's TLS
        # terminator. The Origin from the SPA will be ``https://<host>``.
        for scheme in ("https", "http"):
            if origin == f"{scheme}://{host}".rstrip("/"):
                return True
    return False

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
    """Backward-compatible thin wrapper around `api.services.sanitise.redact_oid`.

    Existing call sites in this module use this name; keep it so the diff
    stays small while making the helper itself a single source of truth.
    See [api/services/sanitise.py](../../services/sanitise.py) `redact_oid`.
    """
    return redact_oid(value)


def _session_arg(owner_oid: str | None) -> str:
    """Derive a stable, non-reversible per-operator tmux session token.

    ttyd is started with `--url-arg`, so the value returned here is appended
    to the upstream WebSocket URL as `?arg=<token>` and ttyd forwards it as
    the first argument to the `elb-tmux-attach` wrapper, which uses it as the
    tmux session-name suffix.

    Hashing the object id (instead of passing it raw) keeps the OID off the
    ttyd command line / process table (charter §11 audit rule) while staying
    deterministic: the same operator always re-attaches to their own session
    after a browser refresh, and two different operators can never collide
    into a shared shell. The token is ``u`` + 16 hex chars, i.e. only
    ``[a-z0-9]``, which the wrapper accepts verbatim.
    """
    digest = hashlib.sha256((owner_oid or "anonymous").encode("utf-8")).hexdigest()
    return "u" + digest[:16]


def _build_upstream_url(owner_oid: str | None) -> str:
    """Build the loopback ttyd WebSocket URL for an operator.

    The ONLY input is the server-side `owner_oid` from the validated ticket;
    nothing the browser sends (its `?ticket=` query, headers, sub-protocol)
    reaches this URL. That is the load-bearing security property — if a
    browser-controlled value ever flowed into the `arg` here, a caller could
    attach to an arbitrary (or a known victim's) tmux session. The
    `test_terminal_session_arg.py` guard tests lock this down.
    """
    base = (
        TERMINAL_UPSTREAM.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
    )
    return f"{base}/ws?arg={_session_arg(owner_oid)}"


# ---------------------------------------------------------------------------
# Audit P3 #29 — WebSocket close-code observability.
#
# The terminal proxy closes the WS with five distinct codes depending on
# what failed:
#   * 4401 — bearer/ticket auth failure (no valid ticket presented)
#   * 4403 — origin rejected (CSWSH guard tripped)
#   * 1011 — upstream ttyd connect failure or mid-stream upstream error
#   * 1000 — normal close (both sides finished)
#   * 1006 — unexpected disconnect (logged when no close frame was sent)
#
# Pre-2026-05-30 only the 1011 and 4403 paths had a log line, so the
# dashboard's terminal-session telemetry could not tell apart "user
# closed the tab" from "ticket expired" from "upstream sidecar died".
# `_audit_ws_close` emits one structured log line per close so the
# downstream `RequestIdMiddleware` log capture can roll them up into App
# Insights custom metrics keyed by `close_code`. Identity fields are
# always passed through `_log_identity_hash` (charter §11 audit rule).
#
# This is purely additive observability — no behavioural change — so it
# is *not* gated behind a `STRICT_*` env var per §12a Rule 4 (Rule 4
# explicitly scopes to "positive validation" changes).
# ---------------------------------------------------------------------------


def _ws_close_severity(code: int) -> int:
    """Pick a log level for a WS close code.

    1000 = normal close → INFO. Everything else → WARNING so it shows up
    in the default log filter without being a hard error.
    """
    if code == 1000:
        return logging.INFO
    return logging.WARNING


def _log_ws_close(
    *,
    code: int,
    reason: str,
    session_id: str | None = None,
    owner_oid: str | None = None,
    owner_upn: str | None = None,
    **extra: object,
) -> None:
    """Emit a single structured `terminal_ws_close` audit line.

    Never logs raw OID / UPN — both go through `_log_identity_hash`. Extra
    kwargs are written as `key=value` so a future log-shipper / App
    Insights query can pivot on them.
    """
    extras_str = " ".join(f"{k}={v!r}" for k, v in extra.items()) if extra else ""
    LOGGER.log(
        _ws_close_severity(code),
        "terminal_ws_close code=%d reason=%r session_id=%s owner_hash=%s upn_hash=%s%s",
        code,
        reason,
        session_id,
        _log_identity_hash(owner_oid),
        _log_identity_hash(owner_upn),
        f" {extras_str}" if extras_str else "",
    )


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


# T2 — Azure CLI sign-in probe.
#
# The Browser Terminal needs the user to run `az login --use-device-code` once
# per terminal-home Files share. There was previously no way for the UI to
# tell the user whether that has been done; the cockpit just showed a generic
# "Sidecar: ok" badge. This endpoint runs `az account show -o json` inside the
# terminal sidecar via the existing terminal_exec server (allowlist already
# permits `az`). Results are cached for 60 s to keep the cockpit refresh cheap.
_AZURE_CLI_CACHE_TTL = 60.0
_azure_cli_cache: dict[str, object] | None = None
_azure_cli_cache_at: float = 0.0
_azure_cli_lock = asyncio.Lock()


async def _probe_azure_cli() -> dict[str, object]:
    """Best-effort probe — never raises. Returns a stable shape for the SPA."""
    # Import lazily so terminal_exec import errors (e.g. EXEC_TOKEN unset in a
    # bare test environment) cannot break the health endpoint itself.
    try:
        from api.services import terminal_exec
    except Exception as exc:  # pragma: no cover - defensive
        return {"status": "unknown", "error": f"exec helper unavailable: {exc}"}

    def _run() -> dict[str, object]:
        try:
            return terminal_exec.run(
                ["az", "account", "show", "-o", "json"],
                timeout_seconds=8,
            )
        except Exception as exc:  # pragma: no cover - network-dependent
            return {"exit_code": -1, "stdout": "", "stderr": str(exc)[:200]}

    result = await asyncio.to_thread(_run)
    exit_code = int(result.get("exit_code", -1) or -1)  # type: ignore[call-overload]
    stdout = str(result.get("stdout") or "").strip()
    if exit_code == 0 and stdout:
        import json as _json

        try:
            parsed = _json.loads(stdout)
            return {
                "status": "signed_in",
                "user": parsed.get("user", {}).get("name"),
                "tenant_id": parsed.get("tenantId"),
                "subscription_id": parsed.get("id"),
                "checked_at": time.time(),
            }
        except Exception:
            return {"status": "signed_in", "checked_at": time.time()}
    stderr = str(result.get("stderr") or "")[:200]
    if "Please run 'az login'" in stderr or "AADSTS" in stderr or exit_code in (1, -1):
        return {
            "status": "signed_out",
            "hint": "Run `az login --use-device-code` in the terminal.",
            "checked_at": time.time(),
        }
    return {"status": "unknown", "error": stderr or "no output", "checked_at": time.time()}


@router.get("/azure-cli")
async def terminal_azure_cli(
    force: bool = Query(default=False, description="Bypass the 60-second result cache."),
    caller: CallerIdentity = REQUIRE_CALLER,
) -> dict[str, object]:
    """Report whether the terminal sidecar has a working `az` sign-in.

    Cached for 60 s. The probe is cheap (~200 ms when signed in) but it does
    fan-out to terminal_exec, so we do not want the cockpit polling on a
    sub-minute interval.
    """
    global _azure_cli_cache, _azure_cli_cache_at
    now = time.time()
    async with _azure_cli_lock:
        cached = _azure_cli_cache
        cache_age = now - _azure_cli_cache_at
        if not force and cached is not None and cache_age < _AZURE_CLI_CACHE_TTL:
            return {**cached, "cached": True, "cache_age_s": int(cache_age)}
        probed = await _probe_azure_cli()
        _azure_cli_cache = probed
        _azure_cli_cache_at = now
    # Identity hash for log correlation only; do not echo caller back.
    LOGGER.info(
        "azure-cli probe caller=%s status=%s",
        _log_identity_hash(caller.object_id),
        probed.get("status"),
    )
    return {**probed, "cached": False}



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
        _log_ws_close(
            code=4401,
            reason=str(exc.detail),
            phase="ticket",
        )
        await websocket.close(code=4401, reason=exc.detail)
        return

    if not _origin_allowed(websocket):
        # Defence against Cross-Site WebSocket Hijacking. Even with a valid
        # one-shot ticket, an attacker page that triggers the SPA to issue a
        # ticket and then opens a WS from a different origin would otherwise
        # get a working shell. Close BEFORE accept so no frames flow.
        LOGGER.warning(
            "terminal_ws origin rejected origin=%r host=%r",
            websocket.headers.get("origin"),
            websocket.headers.get("host"),
        )
        _log_ws_close(
            code=4403,
            reason="origin not allowed",
            session_id=ticket_entry.session_id,
            owner_oid=ticket_entry.owner_oid,
            owner_upn=ticket_entry.owner_upn,
            phase="origin",
        )
        await websocket.close(code=4403, reason="origin not allowed")
        return

    upstream_url = _build_upstream_url(ticket_entry.owner_oid)

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
                subprotocols=["tty"],  # type: ignore[list-item]
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
        LOGGER.warning("terminal proxy upstream connect failed after retries: %s", last_exc)
        _log_ws_close(
            code=1011,
            reason="upstream unavailable",
            session_id=ticket_entry.session_id,
            owner_oid=ticket_entry.owner_oid,
            owner_upn=ticket_entry.owner_upn,
            phase="upstream_connect",
            error_class=type(last_exc).__name__ if last_exc else None,
        )
        try:
            await websocket.close(code=1011, reason="upstream unavailable")
        except Exception as close_exc:
            LOGGER.debug("terminal proxy close after connect failure failed: %s", close_exc)
        return

    await websocket.accept(subprotocol="tty")  # ttyd uses "tty" subprotocol.
    LOGGER.info(
        "terminal session connected session_id=%s tmux_session=%s owner_hash=%s upn_hash=%s",
        ticket_entry.session_id,
        _session_arg(ticket_entry.owner_oid),
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
        _log_ws_close(
            code=1011,
            reason="upstream error",
            session_id=ticket_entry.session_id,
            owner_oid=ticket_entry.owner_oid,
            owner_upn=ticket_entry.owner_upn,
            phase="proxy",
            error_class=type(exc).__name__,
        )
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

    _log_ws_close(
        code=1000,
        reason="normal close",
        session_id=ticket_entry.session_id,
        owner_oid=ticket_entry.owner_oid,
        owner_upn=ticket_entry.owner_upn,
        phase="complete",
        browser_initiated=browser_closed,
        upstream_initiated=upstream_closed,
    )
    if not browser_closed:
        try:
            await websocket.close()
        except Exception as close_exc:
            LOGGER.debug("terminal proxy final close failed: %s", close_exc)

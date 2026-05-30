"""Sidecar snapshot, ticket, and SSE monitoring routes.

Responsibility: Sidecar snapshot, ticket, and SSE monitoring routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_SidecarTicket`, `sidecars_snapshot`, `sidecars_ticket`, `sidecars_events`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py
api/tests/test_monitor_cache.py`.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import secrets
import time as _time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse

from api.auth import CallerIdentity, require_caller
from api.routes.monitor.common import _graceful
from api.services import sse_ticket
from api.services.sidecar_metrics import collect_snapshot as collect_snapshot

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_SIDECAR_TICKET_TTL_SEC = 30
_SSE_PUSH_INTERVAL_SEC = 5
_SSE_HEARTBEAT_INTERVAL_SEC = 25  # < Container Apps' 240s idle ws timeout.


@dataclass(frozen=True)
class _SidecarTicket:
    owner_oid: str
    expires_at: float
    # Audit P0 #2: optional client-binding hashes captured at issue time so
    # the consume path can refuse a ticket replayed from a foreign IP or
    # browser when `STRICT_SSE_TICKET_BINDING=true`. Defaults to ``None`` so
    # legacy callers (and tests that build the dataclass directly) keep
    # working when the strict-binding flag is off.
    ip_hash: str | None = None
    ua_hash: str | None = None


_sidecar_tickets: dict[str, _SidecarTicket] = {}
_sidecar_tickets_lock = asyncio.Lock()


@router.get("/sidecars")
async def sidecars_snapshot(
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """One-shot snapshot of all sidecars' health + CPU/MEM. Used by the SPA
    on initial card mount and as the polling fallback when SSE fails.

    Note: ``drain_events=False`` — the SSE stream is the canonical drainer
    of the animation event hash. If this poll endpoint also drained, two
    independent readers (the live SSE consumer and any HTTP poller — same
    tab, second tab, monitoring probe) would race for events and the badge
    would silently flicker for whichever client lost the race.
    """
    try:
        from api.routes import monitor as monitor_package

        return monitor_package.collect_snapshot(drain_events=False)
    except Exception as exc:
        return cast(dict[str, Any], _graceful(
            "sidecars_snapshot",
            exc,
            empty={
                "ts": None,
                "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
                "sidecars": {},
            },
        ))


@router.post("/sidecars/ticket")
async def sidecars_ticket(
    request: Request,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, object]:
    """Validate the bearer and issue a short-lived single-use SSE ticket.

    When `STRICT_SSE_TICKET_BINDING=true` (audit P0 #2 #3) the issue path
    also rejects foreign Origins with 403 and captures hashed client IP +
    User-Agent on the ticket so the consume path can detect a replay from
    a different browser or network. Default OFF preserves legacy behaviour
    per charter §12a Rule 4.
    """
    sse_ticket.enforce_issue_origin(request)
    token = secrets.token_urlsafe(24)
    async with _sidecar_tickets_lock:
        now = _time.time()
        # Reap expired tickets opportunistically.
        for k in [k for k, v in _sidecar_tickets.items() if v.expires_at <= now]:
            _sidecar_tickets.pop(k, None)
        _sidecar_tickets[token] = _SidecarTicket(
            owner_oid=caller.object_id,
            expires_at=now + _SIDECAR_TICKET_TTL_SEC,
            ip_hash=sse_ticket.client_ip_hash(request),
            ua_hash=sse_ticket.user_agent_hash(request),
        )
    return {"ticket": token, "ttl_seconds": _SIDECAR_TICKET_TTL_SEC}


async def _consume_sidecar_ticket(
    token: str | None, request: Request | None = None
) -> _SidecarTicket | None:
    """Pop and validate a sidecar SSE ticket. Returns None if missing/invalid/expired.

    The SSE route maps ``None`` to **HTTP 204** so the browser's native
    EventSource stops auto-reconnecting after a stream drop (per spec,
    204 is the documented "no reconnect" signal). The frontend's own
    onerror handler still fires and retries with a fresh ticket. Using
    401 here previously generated phantom App Insights Dependency
    failures on every drop.

    When `STRICT_SSE_TICKET_BINDING=true` (audit P0 #2 #3) the function
    additionally checks that the consume request's IP and User-Agent
    hashes match the values captured at issue time; any mismatch is
    treated identically to an expired ticket so the browser receives 204
    and the frontend re-issues from a fresh token. `request` is optional
    only so legacy callers that did not pass it (and tests that pop the
    ticket manually) keep working; production routes always pass it.
    """
    if not token:
        return None
    async with _sidecar_tickets_lock:
        entry = _sidecar_tickets.pop(token, None)
    if entry is None:
        return None
    if entry.expires_at <= _time.time():
        return None
    if request is not None and not sse_ticket.binding_matches(
        request=request,
        ticket_ip_hash=entry.ip_hash,
        ticket_ua_hash=entry.ua_hash,
    ):
        return None
    return entry


@router.get("/sidecars/events")
async def sidecars_events(
    request: Request, ticket: str | None = Query(default=None)
) -> Response:
    """Server-Sent Events stream of sidecar metric snapshots.

    Protocol:
      * Every 5s: ``event: snapshot`` followed by the same JSON shape as
        the GET /sidecars endpoint.
      * Every 25s of idle: ``: heartbeat`` comment line so Container Apps'
        proxy keeps the connection alive (idle ws/SSE timeout is 240s).
      * Client should reconnect on any close (TanStack Query / EventSource
        does this automatically with ``last-event-id``).
      * Ticket validation failures return **HTTP 204**, not 401 — see
        ``_consume_sidecar_ticket``.

    Multi-subscriber model: a single in-process broadcaster owns the
    Redis hash drain. Each connected SSE stream is just a subscriber to
    its fan-out. This guarantees that two browser tabs (or any number of
    concurrent EventSource consumers) see *the same* event counts every
    tick — no consumer can steal another's events. See
    ``_SidecarBroadcaster``.
    """
    if (await _consume_sidecar_ticket(ticket, request)) is None:
        return Response(status_code=204)

    from api.routes import monitor as monitor_package

    queue, initial = await monitor_package._SIDECAR_BROADCASTER.subscribe()

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            # Initial snapshot was captured atomically with the subscribe
            # (drain_events=False) so we don't steal a tick from the
            # broadcaster's pending drain.
            yield f"event: snapshot\ndata: {_json.dumps(initial)}\n\n"

            while True:
                try:
                    payload = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_HEARTBEAT_INTERVAL_SEC
                    )
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if payload is None:
                    # Sentinel from broadcaster shutdown — let the client reconnect.
                    return
                yield payload
        finally:
            from api.routes import monitor as monitor_package

            await monitor_package._SIDECAR_BROADCASTER.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable proxy buffering
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Sidecar broadcaster — single drain loop, fan-out to N SSE subscribers.
#
# Two browser tabs both opening EventSource used to race for the Redis
# events hash and steal each other's counts. The broadcaster removes
# that race by being the *only* component that calls
# ``collect_snapshot(drain_events=True)``. Subscribers receive the same
# pre-serialised SSE frame from a per-connection bounded queue.
#
# Lifecycle:
#   * First subscribe() spawns the background drain task.
#   * unsubscribe() removes the queue; when the last subscriber leaves,
#     the drain task is cancelled (no Redis traffic when nobody's
#     watching).
#   * close() is called from FastAPI shutdown to drain cleanly.
# ---------------------------------------------------------------------------


class _SidecarBroadcaster:
    """In-process fan-out for sidecar SSE snapshots."""

    # Per-subscriber queue size. Each snapshot frame is small (~1 KB).
    # Bound the queue so a stuck subscriber can't grow memory unbounded;
    # if the queue is full we drop the oldest frame (the freshest one
    # always wins for a monitoring UI).
    _QUEUE_MAXSIZE = 8

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str | None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def subscribe(self) -> tuple[asyncio.Queue[str | None], dict[str, Any]]:
        """Register a new subscriber. Returns (queue, initial_snapshot).

        The initial snapshot is captured under the same lock that spawns
        the drain task, so the very first subscriber gets a non-draining
        snapshot for fast first paint and the broadcaster owns every
        subsequent drain.
        """

        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self._QUEUE_MAXSIZE)
        async with self._lock:
            self._subscribers.add(queue)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run(), name="sidecar-broadcaster")
        # Initial snapshot for the new subscriber. Using drain_events=False
        # keeps the broadcaster the sole drainer.
        try:
            from api.routes import monitor as monitor_package

            initial = await asyncio.to_thread(monitor_package.collect_snapshot, drain_events=False)
        except Exception as exc:
            LOGGER.warning("sidecar broadcaster: initial snapshot failed: %s", exc)
            initial = {
                "ts": None,
                "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
                "sidecars": {},
                "events": {"row1": 0, "row2": 0, "row3": 0, "row4": 0},
            }
        return queue, initial

    async def unsubscribe(self, queue: asyncio.Queue[str | None]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)
            if not self._subscribers and self._task is not None:
                task = self._task
                self._task = None
                task.cancel()

    async def close(self) -> None:
        """Stop the broadcaster and wake any waiting subscribers."""

        async with self._lock:
            task = self._task
            self._task = None
            for q in list(self._subscribers):
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    # Drop oldest, push sentinel so the consumer exits promptly.
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(None)
                    except asyncio.QueueFull:
                        pass
            self._subscribers.clear()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: S110
                pass

    async def _run(self) -> None:
        """The single drain loop. Fans the snapshot out to every queue."""
        try:
            while True:
                from api.routes import monitor as monitor_package

                await asyncio.sleep(monitor_package._SSE_PUSH_INTERVAL_SEC)
                try:
                    snap = await asyncio.to_thread(monitor_package.collect_snapshot)
                    payload = f"event: snapshot\ndata: {_json.dumps(snap)}\n\n"
                except Exception as exc:
                    LOGGER.warning("sidecar broadcaster: tick failed: %s", exc)
                    payload = 'event: error\ndata: {"code":"tick_failed"}\n\n'
                # Snapshot the subscriber set so unsubscribe() during fan-out
                # doesn't mutate while we iterate.
                for q in list(self._subscribers):
                    try:
                        q.put_nowait(payload)
                    except asyncio.QueueFull:
                        # Slow consumer: drop the oldest queued frame so
                        # the latest snapshot still gets through. This is
                        # the right policy for a monitoring UI — we want
                        # currentness over completeness.
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            q.put_nowait(payload)
                        except asyncio.QueueFull:
                            pass
        except asyncio.CancelledError:
            return


_SIDECAR_BROADCASTER = _SidecarBroadcaster()

"""Live BLAST job log SSE routes.

Responsibility: Live BLAST job log SSE routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `BlastLogTicketRequest`, `_LogTicket`, `blast_job_logs_ticket`,
`blast_job_logs_events`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.auth import CallerIdentity, require_caller
from api.services.job_artifacts import build_execution_steps_snapshot
from api.services.job_logs.event_bus import read_job_log_events
from api.services.job_logs.k8s import (
    K8sLogTarget,
    discover_k8s_log_targets,
    resolve_elastic_blast_job_id,
    stream_k8s_log_lines,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_LOG_TICKET_TTL_SEC = 30
_SSE_HEARTBEAT_INTERVAL_SEC = 15
_DISCOVERY_INTERVAL_SEC = 2.0
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "deleted"})


class BlastLogTicketRequest(BaseModel):
    subscription_id: str = ""
    resource_group: str = ""
    cluster_name: str = ""
    namespace: str = "default"
    tail_lines: int = Field(default=120, ge=1, le=2_000)


@dataclass(frozen=True)
class _LogTicket:
    owner_oid: str
    job_id: str
    subscription_id: str
    resource_group: str
    cluster_name: str
    namespace: str
    tail_lines: int
    expires_at: float


_tickets: dict[str, _LogTicket] = {}
_tickets_lock = asyncio.Lock()


@router.post("/logs/{job_id}/ticket")
async def blast_job_logs_ticket(
    job_id: str = Path(...),
    request: BlastLogTicketRequest | None = Body(default=None),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, object]:
    """Validate job access and issue a short-lived single-use SSE ticket."""

    from api.services.state_repo import get_state_repo

    repo = get_state_repo()
    state = repo.get_summary(job_id)
    if state is None:
        raise HTTPException(404, "job not found")
    if state.owner_oid and state.owner_oid != caller.object_id:
        raise HTTPException(403, "not owner")
    request = request or BlastLogTicketRequest()
    token = secrets.token_urlsafe(24)
    now = time.time()
    async with _tickets_lock:
        for key in [key for key, value in _tickets.items() if value.expires_at <= now]:
            _tickets.pop(key, None)
        _tickets[token] = _LogTicket(
            owner_oid=caller.object_id,
            job_id=job_id,
            subscription_id=request.subscription_id or state.subscription_id or "",
            resource_group=request.resource_group or state.resource_group or "",
            cluster_name=request.cluster_name or state.cluster_name or "",
            namespace=request.namespace or "default",
            tail_lines=request.tail_lines,
            expires_at=now + _LOG_TICKET_TTL_SEC,
        )
    return {"ticket": token, "ttl_seconds": _LOG_TICKET_TTL_SEC}


async def _consume_log_ticket(job_id: str, token: str | None) -> _LogTicket:
    if not token:
        raise HTTPException(401, "ticket required")
    async with _tickets_lock:
        entry = _tickets.pop(token, None)
    if entry is None:
        raise HTTPException(401, "invalid or expired ticket")
    if entry.expires_at <= time.time():
        raise HTTPException(401, "ticket expired")
    if entry.job_id != job_id:
        raise HTTPException(403, "ticket job mismatch")
    return entry


@router.get("/logs/{job_id}/events")
async def blast_job_logs_events(
    job_id: str = Path(...),
    ticket: str | None = Query(default=None),
) -> StreamingResponse:
    """Server-Sent Events stream of live BLAST job logs.

    Event types:
      * ``snapshot``: current execution step snapshot;
      * ``log``: one terminal or Kubernetes log line;
      * ``status``: source discovery / degraded notices;
      * comments: heartbeat frames while idle.
    """

    entry = await _consume_log_ticket(job_id, ticket)
    queue: asyncio.Queue[str | None] = asyncio.Queue(
        maxsize=max(1, int(os.environ.get("BLAST_LOG_SSE_QUEUE_MAXSIZE", "256")))
    )
    stop_async = asyncio.Event()
    follower_stops: list[threading.Event] = []

    async def enqueue(event_name: str, payload: dict[str, Any], *, event_id: str = "") -> None:
        frame = _sse_frame(event_name, payload, event_id=event_id)
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await queue.put(frame)

    def enqueue_from_thread(event_name: str, payload: dict[str, Any], event_id: str = "") -> None:
        frame = _sse_frame(event_name, payload, event_id=event_id)
        try:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass

    async def emit_snapshot() -> None:
        try:
            from api.services.state_repo import get_state_repo

            state = await asyncio.to_thread(get_state_repo().get, job_id)
            if state is not None:
                await enqueue("snapshot", build_execution_steps_snapshot(state))
        except Exception as exc:
            LOGGER.info("log stream snapshot skipped job_id=%s: %s", job_id, type(exc).__name__)

    async def redis_reader() -> None:
        last_id = "0-0"
        while not stop_async.is_set():
            events = await asyncio.to_thread(
                read_job_log_events,
                job_id,
                last_id=last_id,
                block_ms=5_000,
                count=100,
            )
            for event in events:
                event_id = str(event.get("id") or "")
                if event_id:
                    last_id = event_id
                await enqueue("log", event, event_id=event_id)

    async def k8s_follow_manager() -> None:
        followed: dict[str, asyncio.Task[None]] = {}
        while not stop_async.is_set():
            state = await _load_state(job_id)
            if state is None:
                await asyncio.sleep(_DISCOVERY_INTERVAL_SEC)
                continue
            payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
            elastic_job_id = resolve_elastic_blast_job_id(payload)
            targets = await _discover_targets(entry, job_id, elastic_job_id)
            for target in targets:
                if target.key in followed:
                    continue
                thread_stop = threading.Event()
                follower_stops.append(thread_stop)
                followed[target.key] = asyncio.create_task(
                    _follow_target(entry, target, thread_stop, enqueue_from_thread),
                    name=f"blast-log-follow:{target.key}",
                )
                await enqueue(
                    "status",
                    {
                        "job_id": job_id,
                        "source": "k8s",
                        "status": "following",
                        "pod": target.pod_name,
                        "container": target.container_name,
                        "phase": target.phase,
                    },
                )
            for key, task in list(followed.items()):
                if task.done():
                    followed.pop(key, None)
            if str(getattr(state, "status", "") or "").casefold() in _TERMINAL_STATUSES:
                await asyncio.sleep(10)
                stop_async.set()
                break
            await asyncio.sleep(_DISCOVERY_INTERVAL_SEC)

        for stop in follower_stops:
            stop.set()
        await asyncio.gather(*followed.values(), return_exceptions=True)

    async def event_stream() -> AsyncGenerator[str, None]:
        tasks: list[asyncio.Task[Any]] = []
        try:
            await emit_snapshot()
            tasks = [
                asyncio.create_task(redis_reader(), name="blast-log-redis-reader"),
                asyncio.create_task(k8s_follow_manager(), name="blast-log-k8s-manager"),
            ]
            while not stop_async.is_set() or not queue.empty():
                try:
                    frame = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_HEARTBEAT_INTERVAL_SEC
                    )
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if frame is None:
                    return
                yield frame
            yield _sse_frame("completed", {"job_id": job_id})
        finally:
            stop_async.set()
            for stop in follower_stops:
                stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse_frame(event_name: str, payload: dict[str, Any], *, event_id: str = "") -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}event: {event_name}\ndata: {data}\n\n"


async def _load_state(job_id: str) -> Any | None:
    try:
        from api.services.state_repo import get_state_repo

        return await asyncio.to_thread(get_state_repo().get, job_id)
    except Exception as exc:
        LOGGER.info("log stream state lookup skipped job_id=%s: %s", job_id, type(exc).__name__)
        return None


async def _discover_targets(
    entry: _LogTicket,
    job_id: str,
    elastic_job_id: str,
) -> list[K8sLogTarget]:
    if not (entry.subscription_id and entry.resource_group and entry.cluster_name):
        return []
    try:
        from api.services import get_credential

        return await asyncio.to_thread(
            discover_k8s_log_targets,
            get_credential(),
            entry.subscription_id,
            entry.resource_group,
            entry.cluster_name,
            namespace=entry.namespace,
            job_id=job_id,
            elastic_job_id=elastic_job_id,
        )
    except Exception as exc:
        LOGGER.info("k8s log target discovery skipped job_id=%s: %s", job_id, type(exc).__name__)
        return []


async def _follow_target(
    entry: _LogTicket,
    target: K8sLogTarget,
    stop_event: threading.Event,
    enqueue_from_thread: Any,
) -> None:
    loop = asyncio.get_running_loop()

    def run() -> None:
        try:
            from api.services import get_credential

            credential = get_credential()
            for index, line in enumerate(
                stream_k8s_log_lines(
                    credential,
                    entry.subscription_id,
                    entry.resource_group,
                    entry.cluster_name,
                    target,
                    tail_lines=entry.tail_lines,
                    stop_event=stop_event,
                )
            ):
                event_id = (
                    f"k8s:{target.namespace}:{target.pod_name}:"
                    f"{target.container_name}:{index}"
                )
                loop.call_soon_threadsafe(
                    enqueue_from_thread,
                    "log",
                    {
                        "id": event_id,
                        "schema_version": 1,
                        "job_id": entry.job_id,
                        "source": "k8s",
                        "phase": target.phase,
                        "stream": "stdout",
                        "pod": target.pod_name,
                        "container": target.container_name,
                        "line": line,
                    },
                    event_id,
                )
        except Exception as exc:
            LOGGER.info(
                "k8s log follow ended job_id=%s target=%s: %s",
                entry.job_id,
                target.key,
                type(exc).__name__,
            )

    await asyncio.to_thread(run)

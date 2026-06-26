"""In-process fan-out bus that pushes "jobs changed" events to SSE clients.

Responsibility: Own the api-sidecar registry of connected jobs-events SSE
    consumers and a thread-safe ``broadcast_jobs_changed`` that wakes every one
    of them. This is the real-time counterpart to the poll-based jobs/message-
    flow caches: whenever a job row changes (Service Bus drain, a direct
    dashboard submit, a reconcile task), the same invalidation funnel that drops
    the local caches also calls ``broadcast_jobs_changed`` so the browser
    refetches instantly instead of waiting out a poll interval. Deliberately
    Service-Bus-AGNOSTIC: the event is "a job row changed", which fires for
    direct submits with the Service Bus integration disabled exactly as it does
    for a queue drain.
Edit boundaries: Pure in-process pub/sub plumbing — no HTTP, no Azure/Service
    Bus SDK, no cache logic. The SSE route (``api/routes/monitor/jobs_events.py``)
    owns ``register`` / ``unregister`` around one streaming response; the cache
    invalidation funnels (``jobs_cache_signal.invalidate_jobs_visibility_caches_local``
    and ``blast.submit._invalidate_message_flow_caches``) own the broadcast call.
Key entry points: ``register``, ``unregister``, ``broadcast_jobs_changed``,
    ``subscriber_count``, ``reset_for_test``.
Risky contracts: ``broadcast_jobs_changed`` is called from ARBITRARY threads
    (the Redis cache-invalidate subscriber daemon thread, a sync route handler
    running in the AnyIO threadpool) but each subscriber's ``asyncio.Queue``
    belongs to the api event loop, so delivery MUST go through
    ``loop.call_soon_threadsafe``. It is best-effort and MUST NEVER raise into
    its caller — a closed loop, a full queue, or a missing loop is an accepted
    no-op (the poll fallback still bounds staleness). Per-subscriber queues are
    bounded; on overflow the oldest event is dropped because "jobs-changed" is
    idempotent (the browser coalesces to a single refetch), so a slow client can
    never grow memory without bound or block the broadcaster.
Validation: ``uv run pytest -q api/tests/test_jobs_events_bus.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field

LOGGER = logging.getLogger(__name__)

# Bounded per-client buffer. "jobs-changed" carries no per-event state the
# browser needs to accumulate (it just triggers a refetch), so a small buffer is
# plenty and overflow coalesces rather than grows.
_MAX_QUEUE = 32


@dataclass(eq=False)
class Subscriber:
    """One connected SSE consumer: its event loop + bounded delivery queue."""

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, str]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_MAX_QUEUE)
    )


_subscribers: set[Subscriber] = set()
_lock = threading.Lock()


def register() -> Subscriber:
    """Register the calling coroutine's stream and return its ``Subscriber``.

    Must be called from inside the running api event loop (the SSE GET handler),
    so the captured loop is the one the queue is consumed on.
    """
    sub = Subscriber(loop=asyncio.get_running_loop())
    with _lock:
        _subscribers.add(sub)
    return sub


def unregister(sub: Subscriber) -> None:
    """Drop a stream from the registry (idempotent). Call from the stream's finally."""
    with _lock:
        _subscribers.discard(sub)


def _offer(queue: asyncio.Queue[dict[str, str]], event: dict[str, str]) -> None:
    """Enqueue an event, dropping the oldest on overflow (runs on the loop thread)."""
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # Coalesce: discard the stalest event and keep the newest. "jobs-changed"
        # is idempotent so the client loses nothing by collapsing a burst. Log at
        # INFO so a chronically backed-up subscriber is observable without
        # spamming default WARNING-filtered prod logs.
        LOGGER.info("jobs_events_bus drop-oldest on overflow")
        try:
            queue.get_nowait()
        except Exception:
            return
        try:
            queue.put_nowait(event)
        except Exception:
            return


def broadcast_jobs_changed(reason: str = "") -> None:
    """Wake every connected SSE client. Thread-safe, best-effort, never raises.

    Called from the cache-invalidation funnels, which run on arbitrary threads.
    Each subscriber's queue lives on the api event loop, so delivery is marshalled
    via ``loop.call_soon_threadsafe``. A closed/stopped loop for any subscriber is
    swallowed (and that subscriber pruned) so one dead stream never blocks the rest.
    """
    event = {"type": "jobs-changed", "reason": reason or ""}
    with _lock:
        subs = list(_subscribers)
    dead: list[Subscriber] = []
    for sub in subs:
        try:
            sub.loop.call_soon_threadsafe(_offer, sub.queue, event)
        except RuntimeError:
            # Event loop is closed/closing — prune this subscriber.
            dead.append(sub)
        except Exception as exc:  # pragma: no cover - defensive, must not raise
            LOGGER.debug("jobs-events broadcast skipped: %s", type(exc).__name__)
    if dead:
        with _lock:
            for sub in dead:
                _subscribers.discard(sub)


def subscriber_count() -> int:
    """Number of currently-registered SSE streams (observability / tests)."""
    with _lock:
        return len(_subscribers)


def reset_for_test() -> None:
    """Clear the registry so a test starts from a clean slate."""
    with _lock:
        _subscribers.clear()

"""Cross-sidecar invalidation signal for the BLAST jobs / message-flow caches.

A BLAST job becomes visible on Recent searches, the Dashboard jobs card, and the
Message Flow card through three in-process caches that live in the *api* sidecar:
the jobs-list SWR cache (~10 s), the monitor ``message-flow`` snapshot (~30 s),
and the external ``/v1/jobs`` discovery cache (~70 s). A producer that runs in
the api process (the BLAST submit route, the Service Bus Playground send route)
can drop those caches directly so a freshly created row surfaces on the very next
poll. But a job materialised by the *worker* sidecar — the Service Bus
request-queue drain creating the durable jobstate row, or superseding the
send-time placeholder with the real OpenAPI-keyed row — cannot reach the api
process's in-process caches, so the new/updated row waits out the full cache TTL
before it surfaces (the "appears too late" latency). This module closes that gap
with the same best-effort Redis pub/sub pattern ``db_metadata`` uses: the worker
publishes an invalidation signal and the api sidecar's subscriber drops the three
caches locally.

Responsibility: Own the jobs/message-flow cache invalidation signal — the local
    cache-drop trio, the best-effort cross-sidecar publish, and the api-side
    subscriber lifecycle. No HTTP shaping, no Service Bus / Azure SDK, no Celery
    task bodies.
Edit boundaries: Reusable domain logic only. Routes (api) call
    ``notify_jobs_cache_changed`` after an in-process write; worker tasks call
    ``publish_jobs_cache_invalidate`` after a drain-time write. The api lifespan
    owns ``start_jobs_cache_subscriber`` / ``stop_jobs_cache_subscriber``.
Key entry points: ``invalidate_jobs_visibility_caches_local``,
    ``publish_jobs_cache_invalidate``, ``notify_jobs_cache_changed``,
    ``start_jobs_cache_subscriber``, ``stop_jobs_cache_subscriber``.
Risky contracts: Every function is best-effort and MUST NEVER raise into its
    caller — a Redis outage or a missing cache module is an accepted degraded
    state (the cache TTL still bounds the staleness). The subscriber thread MUST
    exit promptly on stop and back off (capped) on Redis errors instead of
    hot-looping. Only the api sidecar starts the subscriber; worker/beat only
    publish. Honours ``JOBS_CACHE_INVALIDATE_DISABLED=true`` (set by tests so
    pytest never spawns the daemon thread or attempts a real Redis connection).
Validation: ``uv run pytest -q api/tests/test_jobs_cache_signal.py``.
"""

from __future__ import annotations

import json
import logging
import os
import threading

LOGGER = logging.getLogger(__name__)

# Single channel shared by every jobs/message-flow producer. The payload only
# carries a ``reason`` for observability — the subscriber drops all three caches
# regardless (they are cheap to rebuild on the next read), so there is no
# per-key targeting to get wrong.
_CHANNEL = os.environ.get(
    "JOBS_CACHE_INVALIDATE_CHANNEL",
    "elb:cache:blast-jobs",
)

_DISABLED_ENV = "JOBS_CACHE_INVALIDATE_DISABLED"


def _disabled() -> bool:
    return os.environ.get(_DISABLED_ENV, "").strip().lower() == "true"


def invalidate_jobs_visibility_caches_local() -> None:
    """Drop the three api-process caches that gate job visibility. Best-effort.

    Mirrors the trio the BLAST submit route invalidates so a just-written row
    surfaces on the next poll. Each drop is isolated: one missing/broken cache
    module never blocks the others, and this function never raises.
    """
    try:
        from api.services.blast.jobs_list_cache import reset_jobs_list_cache

        reset_jobs_list_cache()
    except Exception as exc:
        LOGGER.debug("jobs list cache reset skipped: %s", type(exc).__name__)
    try:
        from api.services.monitor_cache import invalidate_monitor_snapshot_prefix

        invalidate_monitor_snapshot_prefix("monitor:message-flow")
    except Exception as exc:
        LOGGER.debug("message-flow cache invalidate skipped: %s", type(exc).__name__)
    try:
        from api.services.blast.external_jobs import _reset_external_jobs_cache

        _reset_external_jobs_cache()
    except Exception as exc:
        LOGGER.debug("external jobs cache reset skipped: %s", type(exc).__name__)


def publish_jobs_cache_invalidate(reason: str = "") -> bool:
    """Best-effort Redis publish so peer sidecars drop their jobs caches.

    Returns ``True`` when the publish succeeded, ``False`` on any failure
    (including when ``JOBS_CACHE_INVALIDATE_DISABLED=true``). Never raises —
    Redis being unreachable is an accepted degraded state (the cache TTL bounds
    the staleness). Used by the worker drain path, which cannot reach the api
    process's in-process caches directly.
    """
    if _disabled():
        return False
    try:
        from api.services.redis_clients import get_ops_redis_client

        client = get_ops_redis_client(socket_timeout=1.5)
        payload = json.dumps({"reason": reason or ""}, separators=(",", ":"))
        client.publish(_CHANNEL, payload)
        return True
    except Exception as exc:
        LOGGER.debug("jobs cache invalidate publish skipped: %s", type(exc).__name__)
        return False


def notify_jobs_cache_changed(reason: str = "") -> None:
    """Local invalidate + cross-sidecar publish in one call.

    An api-process producer (e.g. the Service Bus Playground send route) calls
    this after writing a row so its own cache is dropped immediately (the
    user-visible win) and peer sidecars are notified too (forward-safe for a
    multi-replica topology, harmless self-broadcast on the single-replica one).
    """
    invalidate_jobs_visibility_caches_local()
    publish_jobs_cache_invalidate(reason)


_SUBSCRIBER_THREAD: threading.Thread | None = None
_SUBSCRIBER_STOP: threading.Event | None = None
_SUBSCRIBER_LOCK = threading.Lock()

_BACKOFF_MAX_SECONDS = 30.0


def start_jobs_cache_subscriber() -> threading.Thread | None:
    """Start the background pub/sub listener (idempotent).

    Spawned from the api sidecar's FastAPI lifespan. The worker / beat sidecars
    don't hold the cache so they don't subscribe — they only publish. Reconnects
    on Redis errors with exponential backoff capped at 30 s. Honours
    ``JOBS_CACHE_INVALIDATE_DISABLED=true`` (set by tests so pytest never spawns
    the daemon thread).
    """
    if _disabled():
        return None
    global _SUBSCRIBER_THREAD, _SUBSCRIBER_STOP
    with _SUBSCRIBER_LOCK:
        if _SUBSCRIBER_THREAD is not None and _SUBSCRIBER_THREAD.is_alive():
            return _SUBSCRIBER_THREAD
        stop_event = threading.Event()

        def _run() -> None:
            from api.services.redis_clients import get_ops_redis_client

            backoff = 1.0
            while not stop_event.is_set():
                pubsub = None
                try:
                    client = get_ops_redis_client(socket_timeout=5)
                    pubsub = client.pubsub(ignore_subscribe_messages=True)
                    pubsub.subscribe(_CHANNEL)
                    backoff = 1.0
                    # get_message(timeout=1.0) (not listen()) so the stop_event
                    # is checked at least once per second — listen() would block
                    # forever on an idle connection and leak the thread past
                    # shutdown.
                    while not stop_event.is_set():
                        message = pubsub.get_message(timeout=1.0)
                        if not message:
                            continue
                        # The payload is advisory only; any message means "a job
                        # row changed in a peer sidecar" so drop all three caches.
                        invalidate_jobs_visibility_caches_local()
                except Exception as exc:
                    LOGGER.info(
                        "jobs cache invalidate subscriber retry: %s",
                        type(exc).__name__,
                    )
                    if stop_event.wait(timeout=backoff):
                        break
                    backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
                finally:
                    if pubsub is not None:
                        try:
                            pubsub.close()
                        except Exception as exc:
                            LOGGER.debug(
                                "jobs cache invalidate pubsub close skipped: %s",
                                type(exc).__name__,
                            )

        thread = threading.Thread(
            target=_run,
            name="jobs-cache-invalidate-subscriber",
            daemon=True,
        )
        _SUBSCRIBER_STOP = stop_event
        _SUBSCRIBER_THREAD = thread
        thread.start()
        return thread


def stop_jobs_cache_subscriber(timeout: float = 5.0) -> None:
    """Signal the subscriber to stop and join its thread. Idempotent."""
    global _SUBSCRIBER_THREAD, _SUBSCRIBER_STOP
    with _SUBSCRIBER_LOCK:
        event, thread = _SUBSCRIBER_STOP, _SUBSCRIBER_THREAD
        _SUBSCRIBER_STOP, _SUBSCRIBER_THREAD = None, None
    if event is not None:
        event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


def reset_jobs_cache_subscriber_state_for_test() -> None:
    """Test hook: clear module singletons so a test starts from a clean slate."""
    global _SUBSCRIBER_THREAD, _SUBSCRIBER_STOP
    with _SUBSCRIBER_LOCK:
        _SUBSCRIBER_THREAD, _SUBSCRIBER_STOP = None, None

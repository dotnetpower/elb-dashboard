"""Resident Service Bus consumer — optional low-latency drain loop (issue #36 Tier 3).

A long-running loop that continuously long-polls the request queue and drains it
via the SAME ``_drain_handler`` the beat task uses, so a Service-Bus-submitted
BLAST job reaches the execution plane within ~1 s instead of waiting up to the
30 s beat interval. Default-OFF: when the gate env is unset the loop never
starts and the 30 s beat remains the sole drainer (unchanged behaviour). When
ON, the beat task stays registered as a *fallback reconcile* (it no-ops when the
queue is already drained), so the resident loop is an accelerator, never a
single point of failure.

Responsibility: Own the resident drain loop lifecycle (start/stop, bounded
    backoff, graceful stop) and nothing else. The per-message logic stays in
    ``api.tasks.servicebus.tasks._drain_handler`` — this module must NOT
    duplicate it.
Edit boundaries: No per-message business logic here. Service Bus access goes
    through the shared admission-gated ``api.tasks.servicebus.tasks._drain_once``.
    Keep the loop bounded and interruptible (a stuck loop must be stoppable and
    must back off on repeated failure rather than hot-spin).
Key entry points: ``resident_consumer_enabled``, ``run_resident_consumer``,
    ``start_resident_consumer``, ``stop_resident_consumer``,
    ``RESIDENT_CONSUMER_ENV``.
Risky contracts: ``run_resident_consumer`` MUST exit promptly when the stop
    event is set, MUST back off (capped) after a drain error instead of
    hot-looping, and MUST never raise out of the loop body (an unhandled error
    would kill the consumer thread silently). The beat fallback MUST remain
    registered so a crashed loop still drains on the next beat tick. A blocked
    admission decision waits interruptibly and never opens a Service Bus receiver.
Validation: ``uv run pytest -q api/tests/test_resident_consumer.py``.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

LOGGER = logging.getLogger(__name__)

# Default-OFF gate. When unset/false the resident loop never starts.
RESIDENT_CONSUMER_ENV = "SERVICEBUS_RESIDENT_CONSUMER"

# Per-iteration long-poll window. Short enough that a stop is honoured quickly,
# long enough that an idle queue does not spin. Tunable for tests.
_POLL_WAIT_SECONDS = max(1, int(os.environ.get("SERVICEBUS_RESIDENT_POLL_SECONDS", "5")))
# Messages drained per iteration (bounded so one iteration cannot run forever).
_DRAIN_BATCH = max(1, int(os.environ.get("SERVICEBUS_RESIDENT_BATCH", "32")))
# Backoff after a drain error: start small, cap so a persistent outage does not
# hot-loop but still retries periodically.
_BACKOFF_START_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0

_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_thread_lock = threading.Lock()


def resident_consumer_enabled() -> bool:
    """True when the resident drain loop should run (gate env AND SB enabled)."""
    if os.environ.get(RESIDENT_CONSUMER_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return False
    try:
        from api.services.service_bus_pref import service_bus_enabled

        return bool(service_bus_enabled())
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("resident consumer gate check failed: %s", type(exc).__name__)
        return False


def run_resident_consumer(
    stop_event: threading.Event,
    *,
    poll_wait_seconds: int = _POLL_WAIT_SECONDS,
    drain_batch: int = _DRAIN_BATCH,
    max_iterations: int | None = None,
) -> dict[str, int]:
    """Continuously drain the request queue until ``stop_event`` is set.

    Returns aggregate counters (test/observability). ``max_iterations`` bounds
    the loop for tests; production passes ``None`` (run until stopped). Never
    raises out of the loop body — a drain error backs off (capped) and retries.
    """
    from api.services.service_bus_pref import get_service_bus_config
    from api.tasks.servicebus.tasks import _drain_once

    totals = {"iterations": 0, "received": 0, "completed": 0, "abandoned": 0, "dead_lettered": 0}
    backoff = _BACKOFF_START_SECONDS
    iterations = 0
    while not stop_event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        totals["iterations"] = iterations
        try:
            cfg = get_service_bus_config()
            outcome = _drain_once(
                cfg,
                max_messages=drain_batch,
                max_wait_seconds=poll_wait_seconds,
                max_concurrency=1,
            )
            if outcome.get("skipped"):
                stop_event.wait(
                    timeout=max(1, int(outcome.get("retry_after_seconds") or 1))
                )
                continue
            totals["received"] += int(outcome.get("received") or 0)
            totals["completed"] += int(outcome.get("completed") or 0)
            totals["abandoned"] += int(outcome.get("abandoned") or 0)
            totals["dead_lettered"] += int(outcome.get("dead_lettered") or 0)
            backoff = _BACKOFF_START_SECONDS  # success resets the backoff
        except Exception as exc:
            LOGGER.warning(
                "resident consumer drain error (%s); backing off %.1fs",
                type(exc).__name__,
                backoff,
            )
            # Interruptible backoff: wake immediately if asked to stop.
            stop_event.wait(timeout=backoff)
            backoff = min(_BACKOFF_MAX_SECONDS, backoff * 2)
    return totals


def start_resident_consumer() -> bool:
    """Start the resident consumer daemon thread if gated on and not running.

    Returns True if a thread was started, False otherwise (disabled or already
    running). Idempotent — safe to call from worker bootstrap.
    """
    global _thread, _stop_event
    if not resident_consumer_enabled():
        return False
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event = threading.Event()
        event = _stop_event
        _thread = threading.Thread(
            target=_run_forever,
            args=(event,),
            name="sb-resident-consumer",
            daemon=True,
        )
        _thread.start()
        LOGGER.info("resident service bus consumer started")
        return True


def _run_forever(stop_event: threading.Event) -> None:  # pragma: no cover - thin wrapper
    try:
        run_resident_consumer(stop_event)
    except Exception:
        LOGGER.exception("resident service bus consumer exited unexpectedly")


def stop_resident_consumer(timeout: float = 10.0) -> None:
    """Signal the resident consumer to stop and join its thread."""
    global _thread, _stop_event
    with _thread_lock:
        event, thread = _stop_event, _thread
        _stop_event, _thread = None, None
    if event is not None:
        event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


def reset_resident_consumer_state_for_test() -> Any:
    """Test hook: clear module singletons so a test starts from a clean slate."""
    global _thread, _stop_event
    with _thread_lock:
        _thread, _stop_event = None, None

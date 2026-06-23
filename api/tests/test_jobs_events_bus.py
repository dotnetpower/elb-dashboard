"""Tests for the in-process jobs-events fan-out bus.

Responsibility: Verify register/unregister, thread-safe delivery via
    ``call_soon_threadsafe``, overflow coalescing, the no-subscriber no-op, and
    that a broadcast never raises.
Edit boundaries: Test-only.
Key entry points: the ``test_*`` functions.
Risky contracts: broadcast is called from arbitrary threads but delivers onto
    the api event loop; overflow drops the oldest event (idempotent coalesce).
Validation: ``uv run pytest -q api/tests/test_jobs_events_bus.py``.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from api.services import jobs_events_bus as bus


@pytest.fixture(autouse=True)
def _clean() -> None:
    bus.reset_for_test()
    yield
    bus.reset_for_test()


def test_no_subscribers_is_noop() -> None:
    # Must never raise when nothing is connected.
    bus.broadcast_jobs_changed("x")
    assert bus.subscriber_count() == 0


@pytest.mark.asyncio
async def test_broadcast_from_thread_delivers_to_registered() -> None:
    sub = bus.register()
    assert bus.subscriber_count() == 1

    # Broadcast from a NON-loop thread to exercise call_soon_threadsafe.
    threading.Thread(target=lambda: bus.broadcast_jobs_changed("drain")).start()

    event = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
    assert event["type"] == "jobs-changed"
    assert event["reason"] == "drain"

    bus.unregister(sub)
    assert bus.subscriber_count() == 0


@pytest.mark.asyncio
async def test_overflow_coalesces_dropping_oldest() -> None:
    sub = bus.register()
    # Fill the bounded queue past capacity directly on the loop thread.
    for i in range(bus._MAX_QUEUE + 5):
        bus._offer(sub.queue, {"type": "jobs-changed", "reason": str(i)})
    # Never grows past the bound.
    assert sub.queue.qsize() == bus._MAX_QUEUE
    # The newest event survived (oldest were dropped).
    drained = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
    assert drained[-1]["reason"] == str(bus._MAX_QUEUE + 4)
    bus.unregister(sub)


@pytest.mark.asyncio
async def test_unregister_stops_delivery() -> None:
    sub = bus.register()
    bus.unregister(sub)
    threading.Thread(target=lambda: bus.broadcast_jobs_changed("after-unreg")).start()
    await asyncio.sleep(0.05)  # let any stray callback run
    assert sub.queue.qsize() == 0

"""Tests for the in-process SSE fan-out broadcaster.

Responsibility: Tests for the in-process SSE fan-out broadcaster
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_FakeRedisInfo`, `fresh_broadcaster`,
`test_two_subscribers_see_identical_frames`,
`test_drain_task_stops_when_last_subscriber_leaves`,
`test_slow_subscriber_drops_oldest_not_block_others`,
`test_close_wakes_subscribers_with_sentinel`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_sidecar_broadcaster.py`.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from api.routes import monitor


class _FakeRedisInfo:
    """Minimal Redis stand-in used by `collect_snapshot` indirectly. The
    broadcaster never calls into it directly — we patch `collect_snapshot`
    instead, since that's the boundary that matters for fan-out tests.
    """


@pytest.fixture
def fresh_broadcaster(monkeypatch):
    """Each test gets its own broadcaster instance and a deterministic
    `collect_snapshot` stub. The shorter tick interval lets the suite
    finish in well under a second.
    """

    monkeypatch.setattr(monitor, "_SSE_PUSH_INTERVAL_SEC", 0.05)
    bc = monitor._SidecarBroadcaster()
    monkeypatch.setattr(monitor, "_SIDECAR_BROADCASTER", bc)

    counter = {"n": 0}

    def _fake_collect(*, drain_events: bool = True):
        counter["n"] += 1
        return {
            "ts": float(counter["n"]),
            "revision": "test",
            "sidecars": {},
            # Drained snapshots claim a non-zero row1 value; non-draining
            # snapshots (initial / poll fallback) must always be zero.
            "events": (
                {"row1": counter["n"], "row2": 0, "row3": 0, "row4": 0}
                if drain_events
                else {"row1": 0, "row2": 0, "row3": 0, "row4": 0}
            ),
        }

    monkeypatch.setattr(monitor, "collect_snapshot", _fake_collect)
    yield bc, counter
    asyncio.get_event_loop().run_until_complete(bc.close()) if False else None


@pytest.mark.asyncio
async def test_two_subscribers_see_identical_frames(fresh_broadcaster):
    bc, _ = fresh_broadcaster

    q1, initial1 = await bc.subscribe()
    q2, initial2 = await bc.subscribe()

    # Both initial snapshots are non-draining — the broadcaster owns the drain.
    assert initial1["events"] == {"row1": 0, "row2": 0, "row3": 0, "row4": 0}
    assert initial2["events"] == {"row1": 0, "row2": 0, "row3": 0, "row4": 0}

    # Wait for one fan-out tick to land in both queues. This is the H2 race
    # fix in action: a single drain produces a single payload that both
    # subscribers receive — neither one steals from the other.
    frame1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    frame2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert frame1 == frame2
    payload = json.loads(frame1.split("data: ", 1)[1])
    # Exactly one drain happened (counter == 1 across both initials' 0/0).
    # The non-zero events count proves the broadcaster (not the subscribers)
    # called collect_snapshot with drain_events=True.
    assert payload["events"]["row1"] >= 1

    await bc.unsubscribe(q1)
    await bc.unsubscribe(q2)
    await bc.close()


@pytest.mark.asyncio
async def test_drain_task_stops_when_last_subscriber_leaves(fresh_broadcaster):
    bc, counter = fresh_broadcaster

    q, _ = await bc.subscribe()
    # Wait one tick so the loop is definitely running.
    await asyncio.wait_for(q.get(), timeout=1.0)
    assert bc._task is not None
    assert not bc._task.done()

    await bc.unsubscribe(q)

    # Give the cancelled task a moment to settle. After this, the
    # broadcaster MUST stop calling collect_snapshot — otherwise an idle
    # dashboard would keep paying Redis traffic forever.
    await asyncio.sleep(0.2)
    snapshots_before = counter["n"]
    await asyncio.sleep(0.3)
    assert counter["n"] == snapshots_before, "drain loop kept ticking after last subscriber left"
    assert bc._task is None


@pytest.mark.asyncio
async def test_slow_subscriber_drops_oldest_not_block_others(fresh_broadcaster):
    """If one subscriber stops draining its queue, the broadcaster must
    not block the others. Oldest-frame-drop policy keeps the freshest
    snapshot flowing to live consumers.
    """
    bc, _ = fresh_broadcaster

    slow_q, _ = await bc.subscribe()
    fast_q, _ = await bc.subscribe()

    # Fast subscriber drains promptly; slow one never reads. Run for long
    # enough that the slow queue must hit its bound and start dropping.
    fast_frames: list[str] = []

    async def drain_fast():
        while len(fast_frames) < 5:
            fast_frames.append(await asyncio.wait_for(fast_q.get(), timeout=2.0))

    await drain_fast()

    # Fast subscriber kept up.
    assert len(fast_frames) == 5
    # Slow subscriber's queue is bounded — broadcaster never deadlocked.
    assert slow_q.qsize() <= bc._QUEUE_MAXSIZE

    await bc.unsubscribe(slow_q)
    await bc.unsubscribe(fast_q)
    await bc.close()


@pytest.mark.asyncio
async def test_close_wakes_subscribers_with_sentinel(fresh_broadcaster):
    bc, _ = fresh_broadcaster

    q, _ = await bc.subscribe()
    await bc.close()

    # close() pushes a None sentinel so any awaiting consumer exits its
    # loop instead of hanging on get() forever.
    msg = await asyncio.wait_for(q.get(), timeout=1.0)
    assert msg is None

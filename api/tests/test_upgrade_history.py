"""Tests for the upgrade history append-blob writer/reader.

Module summary: Drives `record_event` / `tail_events` against the
in-memory backend; asserts ordering, capping, and the never-raise
contract.

Responsibility: Verify history audit semantics.
Edit boundaries: Update when the event schema changes.
Key entry points: Tests for append, tail order, tail cap, malformed
  line tolerance, never-raise on backend failure.
Risky contracts: `record_event` must never raise even when the backend
  is broken — audit logging cannot block the upgrade.
Validation: `uv run pytest -q api/tests/test_upgrade_history.py`.
"""

from __future__ import annotations

import json

import pytest
from api.services.upgrade import history


@pytest.fixture(autouse=True)
def _in_memory() -> None:
    history.set_backend(history.InMemoryHistoryBackend())
    yield
    history.set_backend(None)


def test_record_and_tail_returns_newest_first() -> None:
    history.record_event("start", job_id="j1", target_version="0.3.0")
    history.record_event("state", job_id="j1", phase="fetching")
    history.record_event("succeeded", job_id="j1", running_version="0.3.0")
    events = history.tail_events(limit=10)
    assert [e.event for e in events] == ["succeeded", "state", "start"]
    assert events[0].detail == {"running_version": "0.3.0"}


def test_tail_caps_to_max() -> None:
    for i in range(50):
        history.record_event("state", job_id="j1", step=i)
    events = history.tail_events(limit=10)
    assert len(events) == 10
    # newest first: last step (49) is first
    assert events[0].detail["step"] == 49


def test_tail_skips_corrupt_lines() -> None:
    # Inject a malformed line through the backend.
    history._backend().append(b"not json\n")
    history.record_event("start", job_id="j1")
    events = history.tail_events(limit=5)
    assert len(events) == 1
    assert events[0].event == "start"


def test_record_event_never_raises_on_backend_failure() -> None:
    class _Boom:
        def append(self, payload: bytes) -> None:
            raise RuntimeError("simulated backend failure")

        def read_all(self) -> bytes:
            return b""

    history.set_backend(_Boom())
    # Must not raise.
    history.record_event("start", job_id="j1")
    history.record_event("succeeded", job_id="j1")


def test_history_event_round_trip() -> None:
    history.record_event("rollback_done", job_id="j2", target={"api": "x:1"})
    raw = history._backend().read_all()
    line = raw.strip().split(b"\n")[-1]
    parsed = json.loads(line)
    assert parsed["event"] == "rollback_done"
    assert parsed["target"] == {"api": "x:1"}

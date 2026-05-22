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
    history.reset_chain_for_tests()
    yield
    history.set_backend(None)
    history.reset_chain_for_tests()


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
    # event_id is stamped at write time so downstream readers can dedupe.
    assert parsed["event_id"]
    assert len(parsed["event_id"]) >= 16


def test_tail_dedupes_by_event_id() -> None:
    """A double-written event (e.g. backend network retry replayed the
    same payload twice) must appear only once in the tail. Without this
    guarantee a transient outage made the SPA history page show the
    same `succeeded` event twice.
    """
    history.record_event("start", job_id="jdup", target_version="0.3.0")
    # Replay the most recent JSON line into the backend verbatim —
    # simulating an at-least-once backend that double-wrote.
    raw = history._backend().read_all()
    line = raw.strip().split(b"\n")[-1]
    history._backend().append(line + b"\n")
    history._backend().append(line + b"\n")
    events = history.tail_events(limit=10)
    # Three writes → one logical event after dedup.
    assert len(events) == 1
    assert events[0].event == "start"


def test_tail_drops_events_older_than_max_age() -> None:
    """Stale audit rows must not clutter the SPA history view. Events
    older than `MAX_TAIL_AGE_DAYS` are filtered out.
    """
    from datetime import UTC, datetime, timedelta

    too_old = (
        datetime.now(UTC) - timedelta(days=history.MAX_TAIL_AGE_DAYS + 5)
    ).isoformat(timespec="seconds")
    fresh = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    history._backend().append(
        f'{{"ts":"{too_old}","job_id":"old","event":"succeeded","event_id":"a"}}\n'.encode()
    )
    history._backend().append(
        f'{{"ts":"{fresh}","job_id":"new","event":"succeeded","event_id":"b"}}\n'.encode()
    )
    events = history.tail_events(limit=10)
    assert len(events) == 1
    assert events[0].job_id == "new"


def test_legacy_events_without_event_id_dedupe_by_payload() -> None:
    """Events written before the `event_id` field landed still need to
    dedupe — fall back to a payload hash so the tail stays stable.
    """
    legacy_line = (
        b'{"ts":"2026-05-22T00:00:00+00:00","job_id":"jold",'
        b'"event":"succeeded","running_version":"0.2.9"}\n'
    )
    history._backend().append(legacy_line)
    history._backend().append(legacy_line)
    events = history.tail_events(limit=10)
    assert len(events) == 1
    assert events[0].event == "succeeded"


def test_audit_hash_chain_is_valid_on_normal_write() -> None:
    """Sequential record_event calls form a valid hash chain."""
    history.record_event("start", job_id="chain1", target_version="0.3.0")
    history.record_event("state", job_id="chain1", phase="building")
    history.record_event("succeeded", job_id="chain1", running_version="0.3.0")
    ok, reason = history.verify_chain()
    assert ok, reason
    assert "verified across 3 events" in reason


def test_audit_hash_chain_detects_tampering() -> None:
    """If a row is mutated after the fact, verify_chain reports the break."""
    history.record_event("start", job_id="tampered", target_version="0.3.0")
    history.record_event("succeeded", job_id="tampered", running_version="0.3.0")
    raw = history._backend().read_all()
    lines = raw.split(b"\n")
    tampered_first = lines[0].replace(
        b'"target_version": "0.3.0"', b'"target_version": "9.9.9"'
    )
    rebuilt = tampered_first + b"\n" + b"\n".join(lines[1:])
    history.set_backend(history.InMemoryHistoryBackend())
    history._backend().append(rebuilt)
    ok, reason = history.verify_chain()
    assert not ok
    assert "chain broken" in reason

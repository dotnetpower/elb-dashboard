"""Tests for the BLAST message lifecycle trace recorder/deriver.

Responsibility: Verify ``record_stage`` appends a best-effort ``mf.<stage>``
    history event with the real stage timestamp, and ``derive_trace`` rebuilds
    an ordered, deduplicated stage timeline + dwell/latency metrics from raw
    history rows.
Edit boundaries: Test-only. Pure — no Azure, no Service Bus.
Key entry points: ``test_record_stage_*``, ``test_derive_trace_*``.
Risky contracts: record is best-effort (must not raise); derive keeps the first
    occurrence of each stage and tolerates missing/out-of-order rows.
Validation: ``uv run pytest -q api/tests/test_message_trace.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from api.services.blast.message_trace import (
    MESSAGE_TRACE_STAGES,
    derive_trace,
    record_stage,
)


class _FakeRepo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def append_history(self, job_id, event, payload=None):
        self.calls.append((job_id, event, payload or {}))


def _hist(event: str, ts: str, stage_ts: str | None = None) -> dict:
    payload = {}
    if stage_ts is not None:
        payload["stage_ts"] = stage_ts
    return {"event": event, "ts": ts, "payload_json": json.dumps(payload) if payload else ""}


def test_record_stage_appends_prefixed_event_with_stage_ts() -> None:
    repo = _FakeRepo()
    record_stage(repo, "job-1", "enqueued", stage_ts="2026-06-14T00:00:00+00:00", foo="bar")
    assert len(repo.calls) == 1
    job_id, event, payload = repo.calls[0]
    assert job_id == "job-1"
    assert event == "mf.enqueued"
    assert payload["stage"] == "enqueued"
    assert payload["stage_ts"] == "2026-06-14T00:00:00+00:00"
    assert payload["foo"] == "bar"


def test_record_stage_accepts_datetime_stage_ts() -> None:
    repo = _FakeRepo()
    dt = datetime(2026, 6, 14, 1, 2, 3, tzinfo=UTC)
    record_stage(repo, "job-1", "received", stage_ts=dt)
    _, _, payload = repo.calls[0]
    assert payload["stage_ts"].startswith("2026-06-14T01:02:03")


def test_record_stage_ignores_unknown_stage() -> None:
    repo = _FakeRepo()
    record_stage(repo, "job-1", "not_a_stage")
    assert repo.calls == []


def test_record_stage_is_best_effort_on_repo_failure() -> None:
    class _BoomRepo:
        def append_history(self, *_a, **_k):
            raise RuntimeError("boom")

    # Must not raise.
    record_stage(_BoomRepo(), "job-1", "submitted")


def test_derive_trace_orders_and_dedups_stages() -> None:
    rows = [
        _hist("mf.received", "2026-06-14T00:00:05+00:00"),
        _hist("mf.enqueued", "2026-06-14T00:00:10+00:00", stage_ts="2026-06-14T00:00:00+00:00"),
        # Duplicate received (at-least-once redelivery) must NOT move the ts.
        _hist("mf.received", "2026-06-14T00:00:30+00:00", stage_ts="2026-06-14T00:00:05+00:00"),
        _hist("mf.submitted", "2026-06-14T00:00:08+00:00"),
        _hist("update", "2026-06-14T00:00:09+00:00"),  # non-mf event ignored
    ]
    trace = derive_trace(rows)
    stages = [s["stage"] for s in trace["stages"]]
    # Canonical order: enqueued, received, submitted.
    assert stages == ["enqueued", "received", "submitted"]
    # First occurrence kept for received (the stage_ts, not the later row).
    received = next(s for s in trace["stages"] if s["stage"] == "received")
    assert received["ts"] == "2026-06-14T00:00:05+00:00"


def test_derive_trace_computes_metrics() -> None:
    rows = [
        _hist("mf.enqueued", "x", stage_ts="2026-06-14T00:00:00+00:00"),
        _hist("mf.received", "x", stage_ts="2026-06-14T00:00:02+00:00"),
        _hist("mf.submitted", "x", stage_ts="2026-06-14T00:00:05+00:00"),
        _hist("mf.completion_published", "x", stage_ts="2026-06-14T00:00:30+00:00"),
    ]
    trace = derive_trace(rows)
    assert trace["metrics"]["queue_dwell_ms"] == 2000
    # submit_latency = submitted(00:05) - received(00:02) = 3 s.
    assert trace["metrics"]["submit_latency_ms"] == 3000
    assert trace["metrics"]["e2e_ms"] == 30000


def test_derive_trace_terminal_and_last_stage() -> None:
    rows = [
        _hist("mf.received", "x", stage_ts="2026-06-14T00:00:02+00:00"),
        _hist("mf.failed", "x", stage_ts="2026-06-14T00:00:09+00:00"),
        _hist("mf.completion_published", "x", stage_ts="2026-06-14T00:00:10+00:00"),
    ]
    trace = derive_trace(rows)
    assert trace["terminal_stage"] == "failed"
    assert trace["last_stage"] == "completion_published"


def test_derive_trace_empty_is_stable() -> None:
    trace = derive_trace([])
    assert trace["stages"] == []
    assert trace["terminal_stage"] is None
    assert trace["last_stage"] is None
    assert trace["metrics"]["queue_dwell_ms"] is None


def test_stage_order_constant_unique() -> None:
    assert len(MESSAGE_TRACE_STAGES) == len(set(MESSAGE_TRACE_STAGES))

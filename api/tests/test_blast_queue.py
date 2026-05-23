"""Tests for BLAST Queue behavior.

Responsibility: Tests for BLAST Queue behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_queue_snapshot_reports_position_and_depth`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_queue.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.services.blast.queue import queue_snapshot


def test_queue_snapshot_reports_position_and_depth() -> None:
    rows = [
        SimpleNamespace(job_id="job-2", status="queued", created_at="2026-05-20T00:00:02"),
        SimpleNamespace(job_id="job-1", status="queued", created_at="2026-05-20T00:00:01"),
        SimpleNamespace(job_id="job-3", status="running", created_at="2026-05-20T00:00:03"),
        SimpleNamespace(job_id="job-4", status="completed", created_at="2026-05-20T00:00:04"),
    ]

    snapshot = queue_snapshot(rows, job_id="job-2")

    assert snapshot["active_count"] == 3
    assert snapshot["queued_count"] == 2
    assert snapshot["running_count"] == 1
    assert snapshot["queue_position"] == 2

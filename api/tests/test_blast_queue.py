from __future__ import annotations

from types import SimpleNamespace

from api.services.blast_queue import queue_snapshot


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

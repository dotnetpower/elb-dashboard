"""Job-detail ``message_trace`` exposure test.

Responsibility: Verify ``GET /api/blast/jobs/{job_id}?history=1`` attaches the
    derived ``message_trace`` (ordered lifecycle stages + dwell/latency metrics)
    built from the same history rows it returns.
Edit boundaries: Test-only. Mocks the state repo; does not touch Azure.
Key entry points: ``test_blast_job_detail_includes_message_trace``.
Risky contracts: ``message_trace`` is present only when history is requested and
    is derived from the returned history rows (single source of truth).
Validation: ``uv run pytest -q api/tests/test_blast_job_message_trace_route.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        job_id="openapi-1",
        task_id=None,
        type="blast",
        status="completed",
        phase="completed",
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:01:00Z",
        error_code=None,
        parent_job_id=None,
        owner_oid=None,
        owner_upn="api",
        job_title="blastn - core_nt",
        program="blastn",
        db="core_nt",
        query_label="query.fa",
        subscription_id="",
        resource_group="",
        cluster_name="",
        storage_account="",
        payload={"external": {"db": "core_nt", "program": "blastn"}},
    )


def _mf(stage: str, stage_ts: str) -> dict:
    import json

    return {
        "PartitionKey": "openapi-1",
        "RowKey": stage,
        "event": f"mf.{stage}",
        "ts": stage_ts,
        "payload_json": json.dumps({"stage": stage, "stage_ts": stage_ts}),
    }


def test_blast_job_detail_includes_message_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    class _FakeRepo:
        def get(self, job_id: str):
            return _state()

        def get_history(self, job_id: str, limit: int = 200):
            return [
                _mf("enqueued", "2026-06-14T00:00:00+00:00"),
                _mf("received", "2026-06-14T00:00:02+00:00"),
                _mf("submitted", "2026-06-14T00:00:05+00:00"),
                _mf("completion_published", "2026-06-14T00:00:30+00:00"),
            ]

    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _FakeRepo())

    from api.main import app

    client = TestClient(app)
    r = client.get(
        "/api/blast/jobs/openapi-1",
        params={"history": "1", "include_database_metadata": "false"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "message_trace" in body
    trace = body["message_trace"]
    stages = [s["stage"] for s in trace["stages"]]
    assert stages == ["enqueued", "received", "submitted", "completion_published"]
    assert trace["metrics"]["queue_dwell_ms"] == 2000
    assert trace["metrics"]["e2e_ms"] == 30000
    assert trace["last_stage"] == "completion_published"


def test_blast_job_detail_omits_trace_without_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    class _FakeRepo:
        def get(self, job_id: str):
            return _state()

        def get_history(self, job_id: str, limit: int = 200):  # pragma: no cover
            raise AssertionError("history must not be fetched when not requested")

    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _FakeRepo())

    from api.main import app

    client = TestClient(app)
    r = client.get(
        "/api/blast/jobs/openapi-1",
        params={"include_database_metadata": "false"},
    )
    assert r.status_code == 200
    assert "message_trace" not in r.json()

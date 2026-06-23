"""Tests for the age-based result retention purge.

Responsibility: Prove ``retention.purge_aged_results`` is disabled by default
(flag off OR window 0), plans without touching anything in dry-run, purges +
tombstones completed jobs older than the window when live, skips recent and
already-deleted rows, and never raises per job.
Edit boundaries: Retention orchestration behaviour only. No Azure network — the
state repo + per-job purge are mocked.
Key entry points: ``test_retention_*``.
Risky contracts: deletion is gated on BOTH ``STORAGE_DFS_ENABLED`` and a window
> 0; an aged job is purged AND tombstoned (leaves listings).
Validation: ``uv run pytest -q api/tests/test_retention.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api.services.state.job_state import JobState
from api.services.storage import retention


class _FakeRepo:
    def __init__(self, rows: list[JobState]) -> None:
        self.rows = rows
        self.updated: list[dict[str, Any]] = []

    def list_completed(self, *, limit: int = 100) -> list[JobState]:
        return self.rows[:limit]

    def update(self, job_id: str, **kw: Any) -> None:
        self.updated.append({"job_id": job_id, **kw})


def _job(job_id: str, *, age_days: int, status: str = "completed") -> JobState:
    ts = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    return JobState(
        job_id=job_id,
        type="blast",
        status=status,
        storage_account="elbstg01",
        created_at=ts,
        updated_at=ts,
    )


def _enable(monkeypatch: pytest.MonkeyPatch, *, days: int = 30) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", "true")
    monkeypatch.setenv("BLAST_RESULT_RETENTION_DAYS", str(days))


def test_retention_days_default_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLAST_RESULT_RETENTION_DAYS", raising=False)
    assert retention.retention_days() == 0


def test_disabled_when_window_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", "true")
    monkeypatch.delenv("BLAST_RESULT_RETENTION_DAYS", raising=False)
    out = retention.purge_aged_results(dry_run=False)
    assert out["enabled"] is False


def test_disabled_when_dfs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_DFS_ENABLED", raising=False)
    monkeypatch.setenv("BLAST_RESULT_RETENTION_DAYS", "30")
    out = retention.purge_aged_results(dry_run=False)
    assert out["enabled"] is False


def test_dry_run_plans_without_purging(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    repo = _FakeRepo([_job("old-1", age_days=40)])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    called = {"purge": 0}
    monkeypatch.setattr(
        "api.services.storage.job_purge.purge_job_result_storage",
        lambda _s: called.__setitem__("purge", called["purge"] + 1),
    )
    out = retention.purge_aged_results(dry_run=True)
    assert out["enabled"] is True
    assert out["planned"] == 1
    assert out["purged"] == 0
    assert called["purge"] == 0
    assert repo.updated == []


def test_live_purges_and_tombstones_aged(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, days=30)
    repo = _FakeRepo([_job("old-1", age_days=40), _job("recent-1", age_days=5)])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    purged: list[str] = []
    monkeypatch.setattr(
        "api.services.storage.job_purge.purge_job_result_storage",
        lambda s: purged.append(s.job_id),
    )
    out = retention.purge_aged_results(dry_run=False)
    assert out["purged"] == 1
    assert out["skipped"] == 1  # the recent job
    assert purged == ["old-1"]
    assert repo.updated == [{"job_id": "old-1", "status": "deleted", "phase": "deleted"}]


def test_skips_already_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    repo = _FakeRepo([_job("gone-1", age_days=99, status="deleted")])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr(
        "api.services.storage.job_purge.purge_job_result_storage",
        lambda _s: (_ for _ in ()).throw(AssertionError("must not purge a deleted row")),
    )
    out = retention.purge_aged_results(dry_run=False)
    assert out["skipped"] == 1
    assert out["purged"] == 0


def test_records_error_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    repo = _FakeRepo([_job("old-1", age_days=40)])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr(
        "api.services.storage.job_purge.purge_job_result_storage",
        lambda _s: (_ for _ in ()).throw(RuntimeError("purge boom")),
    )
    out = retention.purge_aged_results(dry_run=False)
    assert out["errors"] == 1
    assert out["purged"] == 0
    assert repo.updated == []

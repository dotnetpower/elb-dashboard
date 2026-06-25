"""Tests for the BLAST auto-retry beat sweep (side effects + bounds).

Responsibility: Verify the sweep is a no-op when disabled, resubmits due transient
failures (enqueue-first then flip to queued), quarantines exhausted/unrestorable
jobs, respects the per-sweep cap, and never strips a job out of ``failed`` when the
broker enqueue raises.
Edit boundaries: Test-only; fakes the repo and the enqueue helper. No real Celery.
Key entry points: pytest test functions.
Risky contracts: enqueue-before-flip (broker outage leaves the row failed) and the
double-submit guard (only ``status='failed'`` rows are acted on).
Validation: ``uv run pytest -q api/tests/test_blast_auto_retry_task.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.tasks.blast import auto_retry_task
from api.tests.test_blast_auto_retry import FakeState


class FakeRepo:
    def __init__(self, rows: list[FakeState]) -> None:
        self._rows = rows
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.history: list[tuple[str, str, dict[str, Any]]] = []

    def list_recent_failed(self, *, job_type: str = "blast", limit: int = 200) -> list[FakeState]:
        del job_type, limit
        return list(self._rows)

    def update(self, job_id: str, **kwargs: Any) -> None:
        self.updates.append((job_id, kwargs))

    def append_history(self, job_id: str, event: str, payload: dict[str, Any]) -> None:
        self.history.append((job_id, event, payload))


class FakeResult:
    id = "task-new"


def _wire(
    monkeypatch: pytest.MonkeyPatch, repo: FakeRepo, *, enqueue_raises: bool = False
) -> list[Any]:
    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository", lambda: repo, raising=True
    )
    calls: list[Any] = []

    def fake_delay(task: Any, **kwargs: Any) -> FakeResult:
        if enqueue_raises:
            raise RuntimeError("broker down")
        calls.append(kwargs)
        return FakeResult()

    monkeypatch.setattr("api.routes.blast._safe_delay", fake_delay, raising=True)
    return calls


def test_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLAST_AUTO_RETRY_ENABLED", raising=False)
    repo = FakeRepo([FakeState()])
    _wire(monkeypatch, repo)
    summary = auto_retry_task.auto_retry_failed_jobs.run()
    assert summary["enabled"] is False
    assert summary["scanned"] == 0
    assert repo.updates == []


def test_retry_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_AUTO_RETRY_ENABLED", "true")
    repo = FakeRepo([FakeState()])
    calls = _wire(monkeypatch, repo)
    summary = auto_retry_task.auto_retry_failed_jobs.run()
    assert summary["retried"] == 1
    assert calls and calls[0]["job_id"] == "job-1"
    # The row was flipped to queued with the new task id + advanced counter.
    flip = [u for u in repo.updates if u[1].get("status") == "queued"]
    assert flip and flip[0][1]["task_id"] == "task-new"
    assert flip[0][1]["payload"]["auto_retry"]["count"] == 1
    assert any(h[1] == "auto_retry_scheduled" for h in repo.history)


def test_retry_resets_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_AUTO_RETRY_ENABLED", "true")
    state = FakeState(
        payload={
            "query_file": "q.fa",
            "options": {},
            "_progress": {"steps": {"submitting": {"status": "failed"}}},
        }
    )
    repo = FakeRepo([state])
    _wire(monkeypatch, repo)
    auto_retry_task.auto_retry_failed_jobs.run()
    flip = [u for u in repo.updates if u[1].get("status") == "queued"]
    assert flip
    # The stale failed-step timeline must be dropped so the resubmit rebuilds it.
    assert "_progress" not in flip[0][1]["payload"]
    assert flip[0][1]["payload"]["auto_retry"]["count"] == 1


def test_quarantine_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_AUTO_RETRY_ENABLED", "true")
    monkeypatch.setenv("BLAST_AUTO_RETRY_MAX", "1")
    exhausted = FakeState(payload={"query_file": "q.fa", "auto_retry": {"count": 1}})
    repo = FakeRepo([exhausted])
    _wire(monkeypatch, repo)
    summary = auto_retry_task.auto_retry_failed_jobs.run()
    assert summary["quarantined"] == 1
    assert summary["retried"] == 0
    assert any(h[1] == "auto_retry_quarantined" for h in repo.history)
    # status was NOT changed (stays failed) — only payload updated.
    assert all("status" not in u[1] for u in repo.updates)


def test_sweep_cap_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_AUTO_RETRY_ENABLED", "true")
    monkeypatch.setenv("BLAST_AUTO_RETRY_SWEEP_LIMIT", "1")
    rows = [FakeState(job_id=f"job-{i}") for i in range(3)]
    repo = FakeRepo(rows)
    calls = _wire(monkeypatch, repo)
    summary = auto_retry_task.auto_retry_failed_jobs.run()
    assert summary["retried"] == 1
    assert len(calls) == 1


def test_enqueue_failure_leaves_row_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_AUTO_RETRY_ENABLED", "true")
    repo = FakeRepo([FakeState()])
    _wire(monkeypatch, repo, enqueue_raises=True)
    summary = auto_retry_task.auto_retry_failed_jobs.run()
    assert summary["retried"] == 0
    # No flip to queued — the broker failure must not strip the terminal state.
    assert all(u[1].get("status") != "queued" for u in repo.updates)

"""Tests for stale AKS provisioning reconciliation.

Responsibility: Verify queued/running AKS provision rows that stop making
progress are converted into user-visible failed JobState rows.
Edit boundaries: Patch only the state repository facade; do not start Celery,
Redis, or Azure SDK clients.
Key entry points: `test_reconciler_fails_stale_queued_aks_provision`,
`test_reconciler_skips_fresh_running_aks_provision`.
Risky contracts: Legitimate ARM creates refresh `updated_at` frequently and
must not be failed while they are still making progress.
Validation: `uv run pytest -q api/tests/test_aks_stale_provision_reconciler.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest


class _FakeRepo:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.updated: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []

    def list_active(self, *, job_type: str, limit: int) -> list[Any]:
        assert job_type == "aks_provision"
        return self.rows[:limit]

    def update(self, job_id: str, **kwargs: Any) -> Any:
        self.updated.append({"job_id": job_id, **kwargs})
        return SimpleNamespace(job_id=job_id)

    def append_history(self, job_id: str, event: str, payload: dict[str, Any]) -> None:
        self.history.append({"job_id": job_id, "event": event, "payload": payload})


def _row(*, status: str, age_seconds: int) -> Any:
    updated_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return SimpleNamespace(
        job_id=f"job-{status}",
        task_id=f"task-{status}",
        status=status,
        phase="queued" if status == "queued" else "arm_create_or_update",
        updated_at=updated_at.isoformat(timespec="seconds"),
        created_at=updated_at.isoformat(timespec="seconds"),
        payload={"cluster_name": "elb-cluster-01"},
    )


def test_reconciler_fails_stale_queued_aks_provision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeRepo([_row(status="queued", age_seconds=3600)])
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: repo)

    from api.tasks.azure.provision import reconcile_stale_aks_provisions

    result = reconcile_stale_aks_provisions.run(limit=10)

    assert result == {"scanned": 1, "failed": 1, "skipped": 0, "errors": 0}
    assert repo.updated[0]["status"] == "failed"
    assert repo.updated[0]["phase"] == "aks_provision_queue_stalled"
    assert repo.updated[0]["payload"]["terminal_task_event"]["task_id"] == "task-queued"
    assert repo.history[0]["event"] == "aks_provision_queue_stalled"


def test_reconciler_skips_fresh_running_aks_provision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeRepo([_row(status="running", age_seconds=60)])
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: repo)

    from api.tasks.azure.provision import reconcile_stale_aks_provisions

    result = reconcile_stale_aks_provisions.run(limit=10)

    assert result == {"scanned": 1, "failed": 0, "skipped": 1, "errors": 0}
    assert repo.updated == []
    assert repo.history == []

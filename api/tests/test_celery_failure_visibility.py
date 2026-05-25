"""Tests for Celery task terminal failure visibility.

Responsibility: Verify global Celery signal handlers leave task crashes and
revokes in JobState rows so users can find failures after background work dies.
Edit boundaries: Patch only state repository fakes; do not start a real worker
or broker.
Key entry points: `test_task_failure_records_failed_job_state`,
`test_task_revoked_records_cancelled_job_state`.
Risky contracts: Signal handlers must be best-effort and must not require
Azure Table Storage in unit tests.
Validation: `uv run pytest -q api/tests/test_celery_failure_visibility.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class _FakeState:
    def __init__(self) -> None:
        self.job_id = "job-1"
        self.payload = {"cluster_name": "elb-cluster"}


class _FakeRepo:
    def __init__(self) -> None:
        self.updated: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []

    def get(self, job_id: str) -> _FakeState | None:
        return _FakeState() if job_id == "job-1" else None

    def find_by_task_id(self, task_id: str) -> _FakeState | None:
        return _FakeState() if task_id == "task-1" else None

    def update(self, job_id: str, **kwargs: Any) -> _FakeState:
        self.updated.append({"job_id": job_id, **kwargs})
        return _FakeState()

    def append_history(self, job_id: str, event: str, payload: dict[str, Any]) -> None:
        self.history.append({"job_id": job_id, "event": event, "payload": payload})


@pytest.fixture()
def fake_repo(monkeypatch: pytest.MonkeyPatch) -> _FakeRepo:
    repo = _FakeRepo()
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: repo)
    return repo


def test_task_failure_records_failed_job_state(fake_repo: _FakeRepo) -> None:
    from api import celery_app

    celery_app._on_task_failure(
        sender=SimpleNamespace(name="api.tasks.azure.provision_aks"),
        task_id="task-1",
        exception=RuntimeError("cluster create crashed"),
        kwargs={"job_id": "job-1"},
    )

    assert fake_repo.updated
    update = fake_repo.updated[0]
    assert update["job_id"] == "job-1"
    assert update["status"] == "failed"
    assert update["phase"] == "celery_task_failed"
    assert update["error_code"] == "RuntimeError"
    assert update["payload"]["terminal_task_event"]["task_id"] == "task-1"
    assert update["payload"]["terminal_task_event"]["task_name"] == (
        "api.tasks.azure.provision_aks"
    )
    assert fake_repo.history[0]["event"] == "celery_task_failed"
    assert "cluster create crashed" in fake_repo.history[0]["payload"]["message"]


def test_task_revoked_records_cancelled_job_state(fake_repo: _FakeRepo) -> None:
    from api import celery_app

    celery_app._on_task_revoked(
        sender=SimpleNamespace(name="api.tasks.azure.provision_aks"),
        request=SimpleNamespace(id="task-1", kwargs={"job_id": "job-1"}),
        terminated=True,
        expired=False,
        signum=15,
    )

    assert fake_repo.updated[0]["status"] == "cancelled"
    assert fake_repo.updated[0]["phase"] == "celery_task_revoked"
    assert fake_repo.history[0]["event"] == "celery_task_revoked"

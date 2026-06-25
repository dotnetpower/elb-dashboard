"""Tests for the manual one-click BLAST retry route.

Responsibility: Cover ``POST /api/blast/jobs/{id}/retry`` — accepts a transient
failed job, rejects non-failed / non-retryable / external / unrestorable, and
re-enqueues submit with the restored kwargs.
Edit boundaries: Test-only; monkeypatches the state repo, owner assertion, and the
enqueue helper so no Azure / Celery is touched.
Key entry points: pytest test functions.
Risky contracts: only ``auto_retryable`` (transient_infra) failures are accepted;
enqueue-before-flip.
Validation: ``uv run pytest -q api/tests/test_blast_manual_retry.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient


@dataclass
class FakeState:
    job_id: str = "job-1"
    status: str = "failed"
    phase: str = "submitting"
    error_code: str = "terminal_az_login_failed"
    owner_oid: str = "oid-1"
    owner_upn: str = "user@example.com"
    subscription_id: str = "sub-1"
    resource_group: str = "rg-1"
    cluster_name: str = "clu-1"
    storage_account: str = "st1"
    program: str = "blastn"
    db: str = "nt"
    tenant_id: str = "tid-1"
    payload: dict[str, Any] = field(
        default_factory=lambda: {"query_file": "q.fa", "options": {}}
    )


class FakeRepo:
    def __init__(self, state: FakeState | None) -> None:
        self._state = state
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.history: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, _job_id: str) -> FakeState | None:
        return self._state

    def update(self, job_id: str, **kwargs: Any) -> None:
        self.updates.append((job_id, kwargs))

    def append_history(self, job_id: str, event: str, payload: dict[str, Any]) -> None:
        self.history.append((job_id, event, payload))


class FakeResult:
    id = "task-new"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    from api.routes.blast import jobs_lifecycle

    # Owner check is exercised separately; bypass it here so tests focus on retry logic.
    monkeypatch.setattr(jobs_lifecycle, "_assert_job_owner", lambda *_a, **_k: None)
    from api.main import app

    return TestClient(app)


def _wire(monkeypatch: pytest.MonkeyPatch, state: FakeState | None) -> tuple[FakeRepo, list]:
    repo = FakeRepo(state)
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: repo, raising=True
    )
    calls: list[dict[str, Any]] = []

    def fake_delay(_task: Any, **kwargs: Any) -> FakeResult:
        calls.append(kwargs)
        return FakeResult()

    monkeypatch.setattr("api.routes.blast._safe_delay", fake_delay, raising=True)
    return repo, calls


def test_retry_transient_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, calls = _wire(monkeypatch, FakeState())
    r = client.post("/api/blast/jobs/job-1/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert calls and calls[0]["job_id"] == "job-1"
    flip = [u for u in repo.updates if u[1].get("status") == "queued"]
    assert flip and flip[0][1]["task_id"] == "task-new"
    assert flip[0][1]["payload"]["auto_retry"]["quarantined"] is False
    assert "_progress" not in flip[0][1]["payload"]
    assert any(h[1] == "manual_retry" for h in repo.history)


def test_retry_not_found(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, None)
    r = client.post("/api/blast/jobs/missing/retry")
    assert r.status_code == 404


def test_retry_not_failed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, FakeState(status="running"))
    r = client.post("/api/blast/jobs/job-1/retry")
    assert r.status_code == 409


def test_retry_runtime_not_retryable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, FakeState(error_code="blast_search_failed"))
    r = client.post("/api/blast/jobs/job-1/retry")
    assert r.status_code == 400
    assert r.json()["code"] == "not_retryable"


def test_retry_unrestorable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, FakeState(payload={"options": {}}))  # no query_file
    r = client.post("/api/blast/jobs/job-1/retry")
    assert r.status_code == 400
    assert r.json()["code"] == "unrestorable"


def test_retry_external_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    external = FakeState(payload={"query_file": "q.fa", "external": {"job_id": "x"}})
    _wire(monkeypatch, external)
    r = client.post("/api/blast/jobs/job-1/retry")
    assert r.status_code == 400
    assert r.json()["code"] == "external_not_retryable"

"""Tests for `api.routes.blast.external_webhook` (POST /api/blast/register-external-job).

Responsibility: Lock in the receiver contract that the sibling elb-openapi pod's
``_webhook_notify`` depends on — auth gating, always-202 success envelope (even on
unknown job / failure), forward-only state writes, idempotency, and the
"webhook_not_configured" 503 when the dashboard env is missing the shared secret.
Edit boundaries: Assertions only. Mocks the JobStateRepository singleton; never makes
real network calls.
Key entry points: ``test_register_external_job_*``.
Risky contracts: A regression that returns 4xx/5xx on unknown-job / write-failure paths
would cause the sibling to retry-storm. A regression that accepts a backward transition
(``running`` → ``submitted``) would corrupt the dashboard's job view.
Validation: ``uv run pytest -q api/tests/test_external_webhook.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

_WEBHOOK_PATH = "/api/blast/register-external-job"
_TOKEN = "test-shared-secret"


@dataclass
class _FakeRow:
    job_id: str
    status: str = "running"
    phase: str = "running"
    error_code: str = ""


@dataclass
class _FakeRepo:
    rows: dict[str, _FakeRow] = field(default_factory=dict)
    updates: list[dict[str, Any]] = field(default_factory=list)
    raise_on_get: Exception | None = None
    raise_on_update: Exception | None = None
    raise_key_error_on_update: bool = False

    def get(self, job_id: str) -> _FakeRow | None:
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self.rows.get(job_id)

    def update(self, job_id: str, **kwargs: Any) -> _FakeRow:
        if self.raise_key_error_on_update:
            raise KeyError(job_id)
        if self.raise_on_update is not None:
            raise self.raise_on_update
        row = self.rows.get(job_id)
        if row is None:
            raise KeyError(job_id)
        for k, v in kwargs.items():
            setattr(row, k, v)
        self.updates.append({"job_id": job_id, **kwargs})
        return row


@pytest.fixture()
def fake_repo() -> _FakeRepo:
    return _FakeRepo()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, fake_repo: _FakeRepo) -> TestClient:
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("ELB_OPENAPI_INTERNAL_TOKEN", _TOKEN)
    # Avoid the route picking up ``ELB_OPENAPI_API_TOKEN`` as a fallback and
    # masking a test that explicitly wants only the INTERNAL_TOKEN configured.
    monkeypatch.delenv("ELB_OPENAPI_API_TOKEN", raising=False)

    from api.services import state_repo as state_repo_module

    monkeypatch.setattr(state_repo_module, "get_state_repo", lambda: fake_repo)

    from api.main import app

    return TestClient(app)


def _headers(token: str | None = _TOKEN) -> dict[str, str]:
    if token is None:
        return {"Content-Type": "application/json"}
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------
def test_register_external_job_rejects_missing_bearer(client: TestClient) -> None:
    r = client.post(_WEBHOOK_PATH, json={"job_id": "x", "event": "completed"}, headers={})
    assert r.status_code == 401
    assert "missing_bearer" in r.text


def test_register_external_job_rejects_wrong_bearer(client: TestClient) -> None:
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "x", "event": "completed"},
        headers=_headers("definitely-not-the-token"),
    )
    assert r.status_code == 401
    assert "bad_bearer" in r.text


def test_register_external_job_503_when_dashboard_unconfigured(
    monkeypatch: pytest.MonkeyPatch, fake_repo: _FakeRepo
) -> None:
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("ELB_OPENAPI_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("ELB_OPENAPI_API_TOKEN", raising=False)
    # Pin the runtime-cache fallback to empty so the test does not depend on
    # whether the test host can reach the ops Redis (it usually cannot, but
    # explicit is safer).
    from api.services.openapi import runtime as openapi_runtime

    monkeypatch.setattr(openapi_runtime, "get_openapi_api_token", lambda **_: "")
    from api.main import app

    c = TestClient(app)
    r = c.post(
        _WEBHOOK_PATH, json={"job_id": "x", "event": "completed"}, headers=_headers("anything")
    )
    assert r.status_code == 503
    assert "webhook_not_configured" in r.text


def test_register_external_job_accepts_runtime_cache_token(
    monkeypatch: pytest.MonkeyPatch, fake_repo: _FakeRepo
) -> None:
    """Production path: api sidecar has no token env, but the worker stashed it in Redis."""

    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("ELB_OPENAPI_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("ELB_OPENAPI_API_TOKEN", raising=False)
    from api.services import state_repo as state_repo_module
    from api.services.openapi import runtime as openapi_runtime

    monkeypatch.setattr(state_repo_module, "get_state_repo", lambda: fake_repo)
    monkeypatch.setattr(openapi_runtime, "get_openapi_api_token", lambda **_: _TOKEN)
    from api.main import app

    c = TestClient(app)
    fake_repo.rows["job-1"] = _FakeRow(job_id="job-1", status="running", phase="running")
    r = c.post(_WEBHOOK_PATH, json={"job_id": "job-1", "event": "completed"}, headers=_headers())
    assert r.status_code == 202
    assert r.json()["synced"] is True


def test_register_external_job_accepts_api_token_fallback(
    monkeypatch: pytest.MonkeyPatch, fake_repo: _FakeRepo
) -> None:
    """When only ``ELB_OPENAPI_API_TOKEN`` is set the route accepts it (single-secret cluster)."""

    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("ELB_OPENAPI_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", _TOKEN)
    from api.services import state_repo as state_repo_module

    monkeypatch.setattr(state_repo_module, "get_state_repo", lambda: fake_repo)
    from api.main import app

    c = TestClient(app)
    fake_repo.rows["job-1"] = _FakeRow(job_id="job-1", status="running", phase="running")
    r = c.post(_WEBHOOK_PATH, json={"job_id": "job-1", "event": "completed"}, headers=_headers())
    assert r.status_code == 202
    assert r.json()["synced"] is True


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_register_external_job_writes_terminal_status(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.rows["job-1"] = _FakeRow(job_id="job-1", status="running", phase="running")
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "job-1", "event": "completed", "status": "completed"},
        headers=_headers(),
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "accepted"
    assert body["synced"] is True
    assert body["to"] == "completed"
    assert fake_repo.updates == [{"job_id": "job-1", "status": "completed", "phase": "completed"}]
    assert fake_repo.rows["job-1"].status == "completed"


def test_register_external_job_writes_failed_with_error(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.rows["job-1"] = _FakeRow(job_id="job-1", status="running", phase="running")
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "job-1", "event": "failed", "status": "failed", "error": "boom"},
        headers=_headers(),
    )
    assert r.status_code == 202
    assert r.json()["synced"] is True
    assert fake_repo.updates[0]["error_code"] == "boom"


def test_register_external_job_clears_stale_error_on_success(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.rows["job-1"] = _FakeRow(
        job_id="job-1", status="running", phase="running", error_code="worker_lost"
    )
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "job-1", "event": "completed", "status": "completed"},
        headers=_headers(),
    )
    assert r.status_code == 202
    assert fake_repo.updates[0]["error_code"] == ""


# ---------------------------------------------------------------------------
# Unknown / failure paths must all 202
# ---------------------------------------------------------------------------
def test_register_external_job_unknown_job_returns_202(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "nope", "event": "completed", "status": "completed"},
        headers=_headers(),
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "accepted"
    assert body["synced"] is False
    assert body["reason"] == "unknown_job"
    assert fake_repo.updates == []


def test_register_external_job_repo_unavailable_returns_202(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.raise_on_get = RuntimeError("table client down")
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "job-1", "event": "completed", "status": "completed"},
        headers=_headers(),
    )
    assert r.status_code == 202
    assert r.json()["reason"] == "state_repo_unavailable"


def test_register_external_job_update_keyerror_returns_202(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.rows["job-1"] = _FakeRow(job_id="job-1", status="running")
    fake_repo.raise_key_error_on_update = True
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "job-1", "event": "completed", "status": "completed"},
        headers=_headers(),
    )
    assert r.status_code == 202
    assert r.json()["reason"] == "row_gone"


def test_register_external_job_update_exception_returns_202(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.rows["job-1"] = _FakeRow(job_id="job-1", status="running")
    fake_repo.raise_on_update = RuntimeError("transient table failure")
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "job-1", "event": "completed", "status": "completed"},
        headers=_headers(),
    )
    assert r.status_code == 202
    assert r.json()["reason"] == "update_failed"


# ---------------------------------------------------------------------------
# Idempotency + forward-only
# ---------------------------------------------------------------------------
def test_register_external_job_idempotent_same_status(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.rows["job-1"] = _FakeRow(job_id="job-1", status="completed", phase="completed")
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "job-1", "event": "completed", "status": "completed"},
        headers=_headers(),
    )
    assert r.status_code == 202
    body = r.json()
    assert body["synced"] is True
    assert body.get("noop") is True
    # No update call when nothing changes.
    assert fake_repo.updates == []


def test_register_external_job_rejects_backward_transition(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.rows["job-1"] = _FakeRow(job_id="job-1", status="running", phase="running")
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "job-1", "event": "submitted", "status": "submitted"},
        headers=_headers(),
    )
    assert r.status_code == 202
    body = r.json()
    assert body["synced"] is False
    assert body["reason"] == "backward_transition_ignored"
    assert fake_repo.updates == []
    assert fake_repo.rows["job-1"].status == "running"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_register_external_job_rejects_missing_job_id(client: TestClient) -> None:
    r = client.post(_WEBHOOK_PATH, json={"event": "completed"}, headers=_headers())
    assert r.status_code == 422


def test_register_external_job_unknown_status_returns_202(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.rows["job-1"] = _FakeRow(job_id="job-1", status="running")
    r = client.post(
        _WEBHOOK_PATH,
        json={"job_id": "job-1", "event": "weird_event"},
        headers=_headers(),
    )
    assert r.status_code == 202
    assert r.json()["reason"] == "unknown_status"
    assert fake_repo.updates == []

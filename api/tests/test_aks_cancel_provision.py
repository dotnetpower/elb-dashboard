"""Tests for `/api/aks/cancel-provision/{task_id}` route.

Responsibility: Verify ownership enforcement, idempotent terminal-state
    handling, and the side-effect contract (Celery revoke + state-repo
    cancelled marker) of the new cancel route.
Edit boundaries: Pure unit tests with fake Celery and a stubbed
    JobStateRepository. No live broker or Azure SDK.
Key entry points: see per-test docstrings.
Risky contracts: When Celery's revoke signature changes the test
    monkeypatch is the canary.
Validation: `uv run pytest -q api/tests/test_aks_cancel_provision.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def _patch_celery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str,
    revoke_calls: list[dict[str, Any]] | None = None,
) -> None:
    import api.routes.aks.cancel as cancel_mod

    class _FakeAsyncResult:
        def __init__(self, tid: str, app: Any | None = None) -> None:
            self.task_id = tid
            self.status = status

    class _FakeControl:
        def revoke(self, tid: str, **kwargs: Any) -> None:
            if revoke_calls is not None:
                revoke_calls.append({"task_id": tid, **kwargs})

    class _FakeCeleryApp:
        control = _FakeControl()

    # `AsyncResult` is imported lazily inside the route; we patch the
    # module-level name `celery.result.AsyncResult` so the route picks
    # up the fake. Belt-and-suspenders: also patch the alias the route
    # holds on its own module.
    import celery.result

    monkeypatch.setattr(celery.result, "AsyncResult", _FakeAsyncResult)
    monkeypatch.setattr(cancel_mod, "celery_app", _FakeCeleryApp())


def _patch_state_repo(
    monkeypatch: pytest.MonkeyPatch,
    *,
    owner_oid: str | None,
    job_id: str = "job-1",
    update_calls: list[dict[str, Any]] | None = None,
) -> None:
    import api.routes.aks.cancel as cancel_mod

    class _FakeState:
        def __init__(self) -> None:
            self.job_id = job_id
            self.owner_oid = owner_oid

    class _FakeRepo:
        def find_by_task_id(self, _task_id: str) -> _FakeState | None:
            return _FakeState()

    monkeypatch.setattr(cancel_mod, "JobStateRepository", lambda: _FakeRepo())

    def _capture(jid: str, phase: str, status: str = "running", **extra: Any) -> None:
        if update_calls is not None:
            update_calls.append(
                {"job_id": jid, "phase": phase, "status": status, **extra}
            )

    # The route imported `update_state` directly into its module
    # namespace, so the local binding is what actually runs. Patch that
    # binding rather than `api.tasks.azure.helpers.update_state` (the
    # latter would only affect new imports).
    monkeypatch.setattr(cancel_mod, "update_state", _capture)


def test_cancel_revokes_celery_and_marks_state(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A STARTED task is revoked with terminate=True and the JobState row
    is flipped to status='cancelled' / phase='cancelled_by_user'."""
    revoke_calls: list[dict[str, Any]] = []
    update_calls: list[dict[str, Any]] = []
    _patch_celery(monkeypatch, status="STARTED", revoke_calls=revoke_calls)
    _patch_state_repo(monkeypatch, owner_oid=None, update_calls=update_calls)

    resp = client.post("/api/aks/cancel-provision/task-abc")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == "task-abc"
    assert body["was_running"] is True
    assert body["cancelled"] is True
    assert body["previous_status"] == "STARTED"
    assert body["settle_after_seconds"] == 20
    # Celery revoke must have been called with terminate=True so the
    # worker actually drops out of the ARM poll loop.
    assert revoke_calls == [
        {"task_id": "task-abc", "terminate": True, "signal": "SIGTERM"}
    ]
    # State update went through with the canonical cancelled marker.
    assert update_calls == [
        {
            "job_id": "job-1",
            "phase": "cancelled_by_user",
            "status": "cancelled",
            "error_code": "cancelled_by_user",
        }
    ]


def test_cancel_is_idempotent_on_terminal_states(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SUCCESS/FAILURE/REVOKED tasks return 200 with `was_running=False`
    and never invoke `revoke()` again."""
    revoke_calls: list[dict[str, Any]] = []
    _patch_celery(monkeypatch, status="SUCCESS", revoke_calls=revoke_calls)
    _patch_state_repo(monkeypatch, owner_oid=None)

    resp = client.post("/api/aks/cancel-provision/task-done")

    assert resp.status_code == 200
    body = resp.json()
    assert body["was_running"] is False
    assert body["previous_status"] == "SUCCESS"
    assert revoke_calls == []


def test_cancel_rejects_non_owner(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the JobState carries a different `owner_oid`, the request is
    rejected with 403 even though dev-bypass auth synthesises a caller."""
    # Dev-bypass synthesises "anonymous" as object_id; make the state row
    # claim a different owner so the ownership gate trips.
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    _patch_celery(monkeypatch, status="STARTED")
    _patch_state_repo(monkeypatch, owner_oid="other-user-oid")

    resp = client.post("/api/aks/cancel-provision/task-abc")
    assert resp.status_code == 403, resp.text


def test_cancel_passes_through_when_no_state_row(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown task id (no JobState row) still revokes Celery — the
    state row may simply have rolled out of the table while the task
    kept running."""
    import api.routes.aks.cancel as cancel_mod

    class _FakeRepo:
        def find_by_task_id(self, _task_id: str) -> None:
            return None

    monkeypatch.setattr(cancel_mod, "JobStateRepository", lambda: _FakeRepo())
    revoke_calls: list[dict[str, Any]] = []
    _patch_celery(monkeypatch, status="STARTED", revoke_calls=revoke_calls)

    resp = client.post("/api/aks/cancel-provision/task-orphan")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["was_running"] is True
    assert body["job_id"] is None
    assert revoke_calls and revoke_calls[0]["terminate"] is True

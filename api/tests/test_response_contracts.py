"""Tests for API response contract helpers and route projections.

Responsibility: Tests for API response contract helpers and route projections.
Edit boundaries: Keep tests focused on public response shape, not Azure or Celery internals.
Key entry points: `test_submit_response_includes_operation_target_and_admission`,
`test_preflight_returns_admission_decision`, `test_operation_status_projects_celery_task`.
Risky contracts: Do not require network access, real Redis, or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_response_contracts.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_submit_response_includes_operation_target_and_admission() -> None:
    from api.routes.blast.submit import _submit_response

    response = _submit_response("job-1", "task-1", request_id="req-1")

    assert response["job_id"] == "job-1"
    assert response["statusQueryGetUri"] == "/api/tasks/task-1"
    assert response["operation_status_url"] == "/api/operations/task-1"
    assert response["operation"]["operation_id"] == "task-1"
    assert response["operation"]["operation_type"] == "blast.submit"
    assert response["target"]["job_id_kind"] == "dashboard"
    assert response["target"]["dashboard_job_id"] == "job-1"
    assert response["admission"]["decision"] == "accepted"
    assert response["admission"]["basis"] == "current_control_plane_snapshot"
    assert response["meta"]["request_id"] == "req-1"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def test_preflight_returns_admission_decision(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.services as services
    from api.celery_app import celery_app
    from api.services import monitoring

    class FakeConnection:
        def ensure_connection(self, *args: Any, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(services, "get_credential", lambda: object())
    monkeypatch.setattr(
        monitoring,
        "list_aks_clusters",
        lambda *_args, **_kwargs: [
            {"name": "elb-cluster", "power_state": "Running", "node_count": 3}
        ],
    )
    monkeypatch.setattr(
        "api.services.blast.task_config.validate_blast_database_available",
        lambda *, storage_account, database: {
            "container": "blast-db",
            "blob_prefix": f"{database}/{database}",
            "marker_blob": f"{database}/{database}.nsq",
        },
    )
    # Preflight now goes through `validate_blast_database_ready`, which wraps
    # `validate_blast_database_available` with a metadata-blob readiness
    # check. Stub the wrapper at the facade so the route picks it up via its
    # lazy import.
    monkeypatch.setattr(
        "api.services.blast.task_config.validate_blast_database_ready",
        lambda *, storage_account, database: {
            "container": "blast-db",
            "blob_prefix": f"{database}/{database}",
            "marker_blob": f"{database}/{database}.nsq",
        },
    )
    monkeypatch.setattr(celery_app, "connection", lambda: FakeConnection())

    response = client.post(
        "/api/blast/pre-flight",
        json={
            "resource_group": "rg-elb",
            "aks_cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "db": "core_nt",
            "query_data": ">q1\nATGCATGCATGC\n",
            "outfmt": 5,
            "word_size": 28,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ready"] is True
    assert body["decision"] == "would_accept"
    assert body["admission"]["decision"] == "would_accept"
    assert body["admission"]["capacity"]["classification"] == "not_evaluated"
    assert body["meta"]["request_id"]


def test_operation_status_projects_celery_task(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.routes import operations

    class FakeAsyncResult:
        status = "STARTED"
        result = None

        def __init__(self, task_id: str, **_kwargs: Any) -> None:
            self.task_id = task_id
            self.info = {"phase": "running"}

        def ready(self) -> bool:
            return False

        def successful(self) -> bool:
            return False

        def failed(self) -> bool:
            return False

    monkeypatch.setattr(operations, "AsyncResult", FakeAsyncResult)

    response = client.get("/api/operations/task-1")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["operation"]["operation_id"] == "task-1"
    assert body["operation"]["state"] == "running"
    assert body["operation"]["links"]["legacy_task"] == "/api/tasks/task-1"
    assert body["celery"]["status"] == "STARTED"
    assert body["progress"] == {"phase": "running"}

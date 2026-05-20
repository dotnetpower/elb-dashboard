"""Tests for BLAST Log Routes behavior.

Responsibility: Tests for BLAST Log Routes behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_log_ticket_route_binds_ticket_to_job_and_owner`,
`test_log_ticket_route_rejects_other_owner`, `test_log_sse_path_is_excluded_from_http_inspector`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_log_routes.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient


def test_log_ticket_route_binds_ticket_to_job_and_owner(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    class Repo:
        def get_summary(self, job_id):
            assert job_id == "job-1"
            return SimpleNamespace(
                owner_oid="00000000-0000-0000-0000-000000000000",
                subscription_id="sub-from-state",
                resource_group="rg-from-state",
                cluster_name="cluster-from-state",
            )

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: Repo())

    from api.main import app
    from api.routes.blast import logs

    logs._tickets.clear()
    response = TestClient(app).post(
        "/api/blast/logs/job-1/ticket",
        json={"resource_group": "rg-request", "tail_lines": 25},
    )

    assert response.status_code == 200
    token = response.json()["ticket"]
    ticket = logs._tickets[token]
    assert ticket.job_id == "job-1"
    assert ticket.owner_oid == "00000000-0000-0000-0000-000000000000"
    assert ticket.subscription_id == "sub-from-state"
    assert ticket.resource_group == "rg-request"
    assert ticket.cluster_name == "cluster-from-state"
    assert ticket.tail_lines == 25


def test_log_ticket_route_rejects_other_owner(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    class Repo:
        def get_summary(self, _job_id):
            return SimpleNamespace(owner_oid="other-owner")

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: Repo())

    from api.main import app

    response = TestClient(app).post("/api/blast/logs/job-1/ticket", json={})

    assert response.status_code == 403


def test_log_sse_path_is_excluded_from_http_inspector() -> None:
    from api.main import _inspector_should_capture

    assert _inspector_should_capture("/api/blast/logs/job-1/events") is False

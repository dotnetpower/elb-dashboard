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


def test_log_sse_returns_204_when_ticket_missing(monkeypatch) -> None:
    """SSE endpoint must return 204 (not 401) when no ticket is provided.

    Per the HTML spec, 204 is the only documented response that tells a
    browser's EventSource to stop auto-reconnecting. Returning 401 here
    used to generate App Insights phantom failures on every drop.
    """

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    from api.main import app

    response = TestClient(app).get("/api/blast/logs/job-1/events")

    assert response.status_code == 204
    assert response.content == b""


def test_log_sse_returns_204_when_ticket_invalid(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    from api.main import app
    from api.routes.blast import logs

    logs._tickets.clear()
    response = TestClient(app).get("/api/blast/logs/job-1/events?ticket=not-a-real-ticket")

    assert response.status_code == 204
    assert response.content == b""


def test_log_sse_returns_204_when_ticket_bound_to_other_job(monkeypatch) -> None:
    """A ticket for job A used against the URL for job B must 204, not 403.

    The auto-retry storm that motivated the 204 conversion applies to
    every error-status path the SSE endpoint can emit — keep the mismatch
    case silent for the same reason.
    """

    import time as _time

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    from api.main import app
    from api.routes.blast import logs

    logs._tickets.clear()
    logs._tickets["mismatch-ticket"] = logs._LogTicket(
        owner_oid="caller",
        job_id="job-OTHER",
        subscription_id="",
        resource_group="",
        cluster_name="",
        namespace="default",
        tail_lines=120,
        expires_at=_time.time() + 30,
    )

    response = TestClient(app).get("/api/blast/logs/job-1/events?ticket=mismatch-ticket")

    assert response.status_code == 204
    assert response.content == b""
    # Mismatched ticket is still consumed to prevent replay against another job.
    assert "mismatch-ticket" not in logs._tickets

"""Tests for Live Wall sidecar log routes and local log tailing.

Responsibility: Verify sanitized recent log tails, ticket issuance, and route
  registration for the Live Wall log stream.
Edit boundaries: Keep tests local and deterministic; do not require Docker,
  Redis, Azure credentials, or running sidecar processes.
Key entry points: `test_read_recent_lines_sanitizes_sensitive_values`,
  `test_logs_recent_route_returns_local_tail`, `test_log_routes_precede_frontend_catchall`
Risky contracts: Raw logs may contain bearer tokens or SAS query strings; tests
  must guard that the browser-facing payload never does.
Validation: `uv run pytest -q api/tests/test_sidecar_logs.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from api.main import app
from api.services.sidecar_logs import read_recent_lines
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


def _write_log(base: Path, filename: str, text: str) -> None:
    latest = base / "latest"
    latest.mkdir(parents=True)
    (latest / filename).write_text(text, encoding="utf-8")


def test_read_recent_lines_sanitizes_sensitive_values(tmp_path: Path) -> None:
    _write_log(
        tmp_path,
        "api.log",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\n"
        "GET https://acct.blob.core.windows.net/c/blob?sig=secret-signature&sp=r\n",
    )

    lines = read_recent_lines("api", tail=5, log_base=tmp_path)

    assert len(lines) == 2
    rendered = "\n".join(line["text"] for line in lines)
    assert "abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "secret-signature" not in rendered
    assert "***REDACTED***" in rendered


def test_read_recent_lines_returns_empty_for_missing_log(tmp_path: Path) -> None:
    assert read_recent_lines("worker", log_base=tmp_path) == []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("LOCAL_LOG_BASE", str(tmp_path))
    _write_log(tmp_path, "api.log", "api started\nGET /api/health 200 OK\n")
    return TestClient(app)


def test_logs_ticket_route_issues_ticket(client: TestClient) -> None:
    response = client.post("/api/monitor/logs/ticket")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["ticket"], str)
    assert body["expires_at"] > 0


def test_logs_recent_route_returns_local_tail(client: TestClient) -> None:
    response = client.get("/api/monitor/logs/api/recent?tail=1")

    assert response.status_code == 200
    body = response.json()
    assert body["container"] == "api"
    assert [line["text"] for line in body["lines"]] == ["GET /api/health 200 OK"]


def test_logs_recent_rejects_unknown_container(client: TestClient) -> None:
    response = client.get("/api/monitor/logs/not-a-sidecar/recent")

    assert response.status_code == 404


def test_logs_events_returns_204_when_ticket_missing(client: TestClient) -> None:
    """SSE endpoint must return 204 (not 401) when no ticket is provided.

    204 is the documented EventSource "do not reconnect" signal, which
    breaks the phantom App Insights 401 storm caused by browsers
    auto-retrying the same URL after a stream drop popped the ticket.
    """

    response = client.get("/api/monitor/logs/api/events")

    assert response.status_code == 204
    assert response.content == b""


def test_logs_events_returns_204_when_ticket_invalid(client: TestClient) -> None:
    response = client.get("/api/monitor/logs/api/events?ticket=not-a-real-ticket")

    assert response.status_code == 204
    assert response.content == b""


def test_logs_events_returns_204_for_unknown_container_with_invalid_ticket(
    client: TestClient,
) -> None:
    """Container validation runs before ticket consumption.

    Unknown containers must still 404 so frontends can detect a typo
    in the URL, but a valid container with a missing ticket falls
    through to 204.
    """

    response = client.get("/api/monitor/logs/not-a-sidecar/events")

    assert response.status_code == 404


def test_logs_events_returns_204_on_reused_ticket(client: TestClient) -> None:
    """Simulate the browser's native EventSource auto-retry after a drop.

    First valid use pops the ticket; a second request with the same
    ticket must return 204 so the browser stops auto-reconnecting and
    the frontend's bounded retry path takes over with a fresh ticket.
    We pop the ticket manually here because a first valid GET would
    block on the live SSE stream.
    """

    from api.routes.monitor import logs as logs_module

    issued = client.post("/api/monitor/logs/ticket")
    assert issued.status_code == 200
    ticket = issued.json()["ticket"]

    logs_module._log_tickets.pop(ticket, None)

    response = client.get(f"/api/monitor/logs/api/events?ticket={ticket}")
    assert response.status_code == 204
    assert response.content == b""


def test_log_routes_precede_frontend_catchall() -> None:
    positions: dict[tuple[str, str], int] = {}
    for position, route in enumerate(app.routes):
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            positions[(method, route.path)] = position

    frontend = positions[("GET", "/{full_path:path}")]
    assert positions[("POST", "/api/monitor/logs/ticket")] < frontend
    assert positions[("GET", "/api/monitor/logs/{container}/recent")] < frontend
    assert positions[("GET", "/api/monitor/logs/{container}/events")] < frontend


def test_next_poll_interval_resets_on_lines_and_backs_off_when_idle() -> None:
    from api.routes.monitor import logs as logs_module

    base = logs_module._LOG_POLL_INTERVAL_SEC
    factor = logs_module._LOG_POLL_BACKOFF_FACTOR

    # Idle ticks grow geometrically toward the cap and never exceed it.
    i1 = logs_module._next_poll_interval(base, had_lines=False, max_interval=5.0)
    assert i1 == pytest.approx(min(base * factor, 5.0))
    i2 = logs_module._next_poll_interval(i1, had_lines=False, max_interval=5.0)
    assert i2 == pytest.approx(min(i1 * factor, 5.0))
    capped = logs_module._next_poll_interval(100.0, had_lines=False, max_interval=5.0)
    assert capped == 5.0

    # A line arriving snaps the interval straight back to the minimum.
    assert logs_module._next_poll_interval(5.0, had_lines=True, max_interval=5.0) == base


def test_log_poll_max_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.routes.monitor import logs as logs_module

    monkeypatch.delenv("LIVE_WALL_LOG_POLL_MAX_INTERVAL_SEC", raising=False)
    assert logs_module._log_poll_max_interval_sec() == 5.0

    # Explicit override is honoured.
    monkeypatch.setenv("LIVE_WALL_LOG_POLL_MAX_INTERVAL_SEC", "12")
    assert logs_module._log_poll_max_interval_sec() == 12.0

    # A value below the floor collapses to the minimum (restores fixed cadence).
    monkeypatch.setenv("LIVE_WALL_LOG_POLL_MAX_INTERVAL_SEC", "0.2")
    assert logs_module._log_poll_max_interval_sec() == logs_module._LOG_POLL_INTERVAL_SEC

    # Garbage falls back to the safe default.
    monkeypatch.setenv("LIVE_WALL_LOG_POLL_MAX_INTERVAL_SEC", "not-a-number")
    assert logs_module._log_poll_max_interval_sec() == 5.0


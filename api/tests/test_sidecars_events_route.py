"""Tests for the `/api/monitor/sidecars/events` ticket-SSE endpoint.

Responsibility: Verify that the sidecar metrics SSE endpoint returns 204
    (not 401) for missing/invalid/expired tickets so browsers' native
    EventSource stops auto-reconnecting against a consumed URL.
Edit boundaries: Keep the test focused on the ticket gate. Stream content
    is covered by the broadcaster tests (`test_sidecar_broadcaster.py`).
Key entry points: `test_sidecars_events_returns_204_when_ticket_missing`,
    `test_sidecars_events_returns_204_when_ticket_invalid`,
    `test_sidecars_events_returns_204_on_reused_ticket`.
Risky contracts: A 401 here re-triggers the App Insights phantom-failure
    storm fixed in 2026-05-27 (see `docs/features_change/2026-05/`).
Validation: `uv run pytest -q api/tests/test_sidecars_events_route.py`.
"""

from __future__ import annotations

import time

import pytest
from api.main import app
from api.routes.monitor import sidecars as sidecars_module
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    return TestClient(app)


def test_sidecars_events_returns_204_when_ticket_missing(client: TestClient) -> None:
    """SSE endpoint must return 204 (not 401) when no ticket is provided.

    Per the HTML spec, 204 is the only documented response that tells a
    browser's EventSource to stop auto-reconnecting. Returning 401 here
    used to generate App Insights Dependency-failure noise on every
    stream drop because the browser would auto-retry the same URL whose
    ticket had just been consumed.
    """

    response = client.get("/api/monitor/sidecars/events")

    assert response.status_code == 204
    assert response.content == b""


def test_sidecars_events_returns_204_when_ticket_invalid(client: TestClient) -> None:
    response = client.get("/api/monitor/sidecars/events?ticket=not-a-real-ticket")

    assert response.status_code == 204
    assert response.content == b""


def test_sidecars_events_returns_204_on_reused_ticket(client: TestClient) -> None:
    """Simulate the browser's native EventSource auto-retry after a drop.

    First valid use pops the ticket from `_sidecar_tickets`; a second
    request with the same ticket must return 204 so the browser stops
    auto-reconnecting and the frontend's bounded retry path takes over
    with a fresh ticket. We pop the ticket manually here because the
    first valid GET would block on the live SSE stream.
    """

    issued = client.post("/api/monitor/sidecars/ticket")
    assert issued.status_code == 200
    ticket = issued.json()["ticket"]

    # Simulate "ticket already consumed by a now-dropped SSE connection".
    sidecars_module._sidecar_tickets.pop(ticket, None)

    response = client.get(f"/api/monitor/sidecars/events?ticket={ticket}")
    assert response.status_code == 204
    assert response.content == b""


def test_sidecars_events_returns_204_on_expired_ticket(client: TestClient) -> None:
    """Expired tickets must not 401 either."""

    issued = client.post("/api/monitor/sidecars/ticket")
    assert issued.status_code == 200
    ticket = issued.json()["ticket"]

    entry = sidecars_module._sidecar_tickets[ticket]
    sidecars_module._sidecar_tickets[ticket] = sidecars_module._SidecarTicket(
        owner_oid=entry.owner_oid,
        expires_at=time.time() - 60,
    )

    response = client.get(f"/api/monitor/sidecars/events?ticket={ticket}")
    assert response.status_code == 204
    assert response.content == b""

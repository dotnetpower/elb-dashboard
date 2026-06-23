"""Tests for the jobs-events SSE route + the invalidate-funnel broadcast hooks.

Responsibility: Verify the default-OFF gate (ticket returns ``enabled:false`` and
    the stream returns 204 when unset), ticket issuance + 204 on a bad/absent
    ticket when enabled, and that BOTH invalidate funnels fan out to the bus —
    including the Service-Bus-DISABLED direct-submit path.
Edit boundaries: Test-only.
Key entry points: the ``test_*`` functions.
Risky contracts: the SSE stream must stay ticket-gated (no require_caller) and
    return 204 (not 401) so EventSource stops auto-reconnecting; the broadcast
    hook must be Service-Bus-agnostic.
Validation: ``uv run pytest -q api/tests/test_jobs_events_route.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    from api.main import app

    return TestClient(app)


def test_ticket_disabled_by_default(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBS_EVENTS_SSE_ENABLED", raising=False)
    r = client.post("/api/monitor/jobs-events/ticket")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


def test_stream_204_when_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBS_EVENTS_SSE_ENABLED", raising=False)
    r = client.get("/api/monitor/jobs-events?ticket=whatever")
    assert r.status_code == 204


def test_ticket_issued_when_enabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_EVENTS_SSE_ENABLED", "true")
    r = client.post("/api/monitor/jobs-events/ticket")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert isinstance(body["ticket"], str) and body["ticket"]
    assert body["expires_at"] > 0


def test_stream_204_on_absent_or_bad_ticket(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBS_EVENTS_SSE_ENABLED", "true")
    assert client.get("/api/monitor/jobs-events").status_code == 204
    assert client.get("/api/monitor/jobs-events?ticket=nope").status_code == 204


def test_ticket_is_single_use(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBS_EVENTS_SSE_ENABLED", "true")
    ticket = client.post("/api/monitor/jobs-events/ticket").json()["ticket"]
    # A redeemed ticket cannot be reused; the second consume returns None. (The
    # live stream blocks once a valid ticket is accepted, so exercise the consume
    # helper directly instead of GETting the stream.)
    import asyncio

    from api.routes.monitor import jobs_events as je

    async def _consume_twice() -> tuple[object, object]:
        first = await je._consume_ticket(ticket)
        second = await je._consume_ticket(ticket)
        return first, second

    first, second = asyncio.run(_consume_twice())
    assert first is not None
    assert second is None


def test_local_invalidate_funnel_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared cache funnel (drain + playground path) fans out to the bus."""
    from api.services.blast import jobs_cache_signal

    calls: list[str] = []
    monkeypatch.setattr(
        "api.services.jobs_events_bus.broadcast_jobs_changed",
        lambda reason="": calls.append(reason),
    )
    jobs_cache_signal.invalidate_jobs_visibility_caches_local()
    assert calls == [""]


def test_direct_submit_funnel_broadcasts_without_service_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct-submit funnel broadcasts even with Service Bus disabled."""
    from api.routes.blast import submit

    calls: list[str] = []
    monkeypatch.setattr(
        "api.services.jobs_events_bus.broadcast_jobs_changed",
        lambda reason="": calls.append(reason),
    )
    submit._invalidate_message_flow_caches()
    assert calls == [""]

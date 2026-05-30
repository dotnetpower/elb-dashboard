"""Strict-mode SSE ticket binding tests (audit P0 #2 #3).

Module summary: When `STRICT_SSE_TICKET_BINDING=true` the sidecars +
logs SSE issue endpoints must reject foreign Origins with 403 and the
consume endpoints must refuse tickets replayed from a different IP /
User-Agent. When the flag is unset the behaviour is unchanged.

Responsibility: Cover both the ON and OFF paths per charter §12a Rule 4
  so flipping the default later is a single PR with green tests.
Edit boundaries: New positive / negative cases land here; the underlying
  helpers live in `api/services/sse_ticket.py`.
Key entry points: per-fixture / per-test functions.
Risky contracts: Must NOT add `Depends(require_caller)` to any SSE consume
  endpoint (charter §12a Rule 5).
Validation: `uv run pytest -q api/tests/test_sse_ticket_binding.py`.
"""

from __future__ import annotations

import pytest
from api.main import app
from api.routes.monitor import logs as logs_module
from api.routes.monitor import sidecars as sidecars_module
from api.services import sse_ticket
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helper: snapshot of the strict-mode flag for assertion clarity in tests.
# ---------------------------------------------------------------------------


def test_strict_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """`STRICT_SSE_TICKET_BINDING` is OFF by default per §12a Rule 4."""
    monkeypatch.delenv("STRICT_SSE_TICKET_BINDING", raising=False)
    assert sse_ticket.is_strict() is False


def test_strict_flag_honours_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    assert sse_ticket.is_strict() is True


# ---------------------------------------------------------------------------
# /sidecars/ticket — issue-time origin gate (strict ON)
# ---------------------------------------------------------------------------


def test_sidecars_ticket_rejects_foreign_origin_when_strict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict mode + foreign Origin → 403 from /sidecars/ticket."""
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    response = client.post(
        "/api/monitor/sidecars/ticket",
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_sidecars_ticket_accepts_same_origin_when_strict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict mode + same-origin (matches Host) → 200."""
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    # TestClient injects host=testserver; matching origin must pass.
    response = client.post(
        "/api/monitor/sidecars/ticket",
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 200
    assert "ticket" in response.json()


def test_sidecars_ticket_accepts_foreign_origin_when_strict_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (strict OFF) keeps the existing permissive behaviour."""
    monkeypatch.delenv("STRICT_SSE_TICKET_BINDING", raising=False)
    response = client.post(
        "/api/monitor/sidecars/ticket",
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# /sidecars/events — consume-time IP/UA binding (strict ON)
# ---------------------------------------------------------------------------


def test_sidecars_events_returns_204_on_ua_mismatch_when_strict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issuing with one UA and consuming with another → 204 (not 200)."""
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    issued = client.post(
        "/api/monitor/sidecars/ticket",
        headers={"user-agent": "browser-a", "origin": "http://testserver"},
    )
    assert issued.status_code == 200
    ticket = issued.json()["ticket"]

    response = client.get(
        f"/api/monitor/sidecars/events?ticket={ticket}",
        headers={"user-agent": "browser-b"},
    )
    assert response.status_code == 204


def test_sidecars_events_returns_204_on_ip_mismatch_when_strict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issuing under one X-Forwarded-For and consuming under another → 204."""
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    issued = client.post(
        "/api/monitor/sidecars/ticket",
        headers={
            "user-agent": "browser-a",
            "origin": "http://testserver",
            "x-forwarded-for": "10.0.0.1",
        },
    )
    assert issued.status_code == 200
    ticket = issued.json()["ticket"]

    response = client.get(
        f"/api/monitor/sidecars/events?ticket={ticket}",
        headers={"user-agent": "browser-a", "x-forwarded-for": "10.0.0.99"},
    )
    assert response.status_code == 204


def test_sidecars_events_works_when_strict_off_with_different_clients(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OFF path: legacy callers without matching IP/UA still work.

    We pop the ticket manually before the consume request because a live
    SSE consume would block on the broadcaster. The point of this test
    is that the *consume code path* returns the entry (= 200 / stream)
    when strict mode is off, regardless of header mismatch — verified by
    `_consume_sidecar_ticket` returning a non-None entry.
    """
    monkeypatch.delenv("STRICT_SSE_TICKET_BINDING", raising=False)
    issued = client.post(
        "/api/monitor/sidecars/ticket",
        headers={"user-agent": "browser-a", "x-forwarded-for": "10.0.0.1"},
    )
    assert issued.status_code == 200
    ticket = issued.json()["ticket"]

    # The ticket is in the store with ip_hash/ua_hash captured at issue.
    entry = sidecars_module._sidecar_tickets.get(ticket)
    assert entry is not None
    assert entry.ip_hash is not None
    assert entry.ua_hash is not None

    # With strict off, `binding_matches` returns True regardless of input.
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "headers": [(b"user-agent", b"browser-b"), (b"x-forwarded-for", b"10.0.0.99")],
        "client": ("10.0.0.99", 12345),
    }
    fake_request = StarletteRequest(scope)
    assert sse_ticket.binding_matches(
        request=fake_request,
        ticket_ip_hash=entry.ip_hash,
        ticket_ua_hash=entry.ua_hash,
    ) is True


# ---------------------------------------------------------------------------
# /logs/ticket + /logs/{container}/events — same hardening, parallel cases
# ---------------------------------------------------------------------------


def test_logs_ticket_rejects_foreign_origin_when_strict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    response = client.post(
        "/api/monitor/logs/ticket",
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_logs_ticket_accepts_same_origin_when_strict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    response = client.post(
        "/api/monitor/logs/ticket",
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 200
    assert "ticket" in response.json()


def test_logs_events_returns_204_on_ua_mismatch_when_strict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    issued = client.post(
        "/api/monitor/logs/ticket",
        headers={"user-agent": "browser-a", "origin": "http://testserver"},
    )
    assert issued.status_code == 200
    ticket = issued.json()["ticket"]

    response = client.get(
        f"/api/monitor/logs/api/events?ticket={ticket}",
        headers={"user-agent": "browser-b"},
    )
    assert response.status_code == 204


def test_logs_events_returns_204_on_ip_mismatch_when_strict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    issued = client.post(
        "/api/monitor/logs/ticket",
        headers={
            "user-agent": "browser-a",
            "origin": "http://testserver",
            "x-forwarded-for": "10.0.0.1",
        },
    )
    assert issued.status_code == 200
    ticket = issued.json()["ticket"]

    response = client.get(
        f"/api/monitor/logs/api/events?ticket={ticket}",
        headers={"user-agent": "browser-a", "x-forwarded-for": "10.0.0.99"},
    )
    assert response.status_code == 204


def test_logs_events_legacy_ticket_without_binding_fails_when_strict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict mode rejects tickets that pre-date the binding rollout (ip/ua = None).

    Otherwise an attacker could forge a ticket payload with the binding
    fields stripped and bypass the check by claiming a stale store.
    """
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    # Build a legacy-style ticket directly in the store (no ip/ua).
    import secrets
    import time

    token = secrets.token_urlsafe(24)
    logs_module._log_tickets[token] = logs_module._LogTicket(
        owner_oid="00000000-0000-0000-0000-000000000000",
        expires_at=time.time() + 30,
    )
    response = client.get(
        f"/api/monitor/logs/api/events?ticket={token}",
        headers={"user-agent": "browser-a", "origin": "http://testserver"},
    )
    assert response.status_code == 204


# ---------------------------------------------------------------------------
# Unit tests for the helper itself.
# ---------------------------------------------------------------------------


def test_client_ip_hash_prefers_xff_first_hop() -> None:
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "headers": [
            (b"x-forwarded-for", b"203.0.113.5, 10.0.0.1, 10.0.0.2"),
        ],
        "client": ("10.0.0.99", 12345),
    }
    request = StarletteRequest(scope)
    expected = sse_ticket._short_sha256("203.0.113.5")
    assert sse_ticket.client_ip_hash(request) == expected


def test_client_ip_hash_falls_back_to_peer() -> None:
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "headers": [],
        "client": ("198.51.100.7", 9999),
    }
    request = StarletteRequest(scope)
    assert sse_ticket.client_ip_hash(request) == sse_ticket._short_sha256(
        "198.51.100.7"
    )


def test_user_agent_hash_uses_unknown_when_missing() -> None:
    from starlette.requests import Request as StarletteRequest

    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 1)}
    request = StarletteRequest(scope)
    assert sse_ticket.user_agent_hash(request) == sse_ticket._short_sha256("unknown")


def test_binding_matches_returns_true_when_strict_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STRICT_SSE_TICKET_BINDING", raising=False)
    from starlette.requests import Request as StarletteRequest

    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 1)}
    request = StarletteRequest(scope)
    assert (
        sse_ticket.binding_matches(
            request=request, ticket_ip_hash="aaa", ticket_ua_hash="bbb"
        )
        is True
    )


def test_binding_matches_rejects_legacy_ticket_when_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRICT_SSE_TICKET_BINDING", "true")
    from starlette.requests import Request as StarletteRequest

    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 1)}
    request = StarletteRequest(scope)
    assert (
        sse_ticket.binding_matches(
            request=request, ticket_ip_hash=None, ticket_ua_hash=None
        )
        is False
    )

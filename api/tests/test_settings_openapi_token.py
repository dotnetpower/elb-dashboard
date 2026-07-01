"""Tests for /api/settings/openapi-token — shared M2M token disclosure.

Module summary: The SPA's "Copy curl" surface fetches this endpoint to
inline the real shared token into the copied command. The endpoint must
(a) require authentication (no anonymous token exposure), (b) return the
token that ``api.auth._resolve_expected_openapi_token`` resolves so the
disclosed value matches what the auth gate actually accepts, and (c)
surface the ``ALLOW_OPENAPI_TOKEN_AUTH`` gate state so the SPA can render
a helpful hint when the gate is off.

Responsibility: Route-level auth + response-shape assertions.
Edit boundaries: Do not test the auth-layer token-resolution here — that
    lives in ``test_smoke.py`` / ``test_aks_openapi_databases.py``.
Key entry points: ``test_*``.
Risky contracts: The endpoint deliberately does NOT gate on Contributor /
    Owner. Any authenticated caller receives the token. If that changes,
    update the anonymous-vs-authed persona coverage here.
Validation: ``uv run pytest -q api/tests/test_settings_openapi_token.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def test_returns_token_and_gate_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "real-token-value")
    monkeypatch.setenv("ALLOW_OPENAPI_TOKEN_AUTH", "true")

    resp = client.get("/api/settings/openapi-token")

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == "real-token-value"
    assert body["gate_enabled"] is True


def test_empty_token_when_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ELB_OPENAPI_API_TOKEN", raising=False)
    monkeypatch.setenv("ALLOW_OPENAPI_TOKEN_AUTH", "true")
    # Simulate no Redis cache entry either.
    monkeypatch.setattr(
        "api.services.openapi.runtime.get_openapi_api_token",
        lambda *a, **k: "",
    )

    resp = client.get("/api/settings/openapi-token")

    assert resp.status_code == 200
    assert resp.json() == {"token": "", "gate_enabled": True}


def test_reports_gate_off(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "some-token")
    monkeypatch.delenv("ALLOW_OPENAPI_TOKEN_AUTH", raising=False)

    resp = client.get("/api/settings/openapi-token")

    assert resp.status_code == 200
    body = resp.json()
    assert body["gate_enabled"] is False
    # Token still returned so the SPA can decide what to do; the gate flag
    # is what drives the "won't authenticate" hint.
    assert body["token"] == "some-token"


def test_anonymous_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real-auth mode: no dev bypass, no bearer, no shared token header.
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    c = TestClient(app)
    resp = c.get("/api/settings/openapi-token")
    assert resp.status_code == 401

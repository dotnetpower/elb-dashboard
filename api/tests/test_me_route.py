"""Tests for `/api/me`.

Responsibility: Lock in the response shape of `/api/me` so the SPA contract stays stable
when the route is extended (subscriptions, errors, …).
Edit boundaries: Pure FastAPI / monkeypatch tests; do not require real Azure access.
Key entry points: `test_me_returns_identity_with_subscriptions`,
`test_me_surfaces_subscriptions_error_field`.
Risky contracts: SPA branches on `subscriptions` and `subscriptions_error`. Renaming either
field requires a coordinated SPA change.
Validation: `uv run pytest -q api/tests/test_me_route.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    from api.main import app

    return TestClient(app)


def test_me_returns_identity_fields(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """Even when the subscription listing fails, identity claims must come through."""
    import api.routes.me as me_module

    monkeypatch.setattr(
        me_module, "_list_visible_subscriptions", lambda: ([], "boom: ARM offline")
    )
    res = client.get("/api/me")
    assert res.status_code == 200
    body = res.json()
    assert {"object_id", "tenant_id", "upn", "subscriptions"} <= body.keys()
    assert body["subscriptions"] == []
    assert "subscriptions_error" in body
    assert "boom" in body["subscriptions_error"]


def test_me_returns_subscriptions(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import api.routes.me as me_module

    fake = [
        {
            "subscriptionId": "11111111-1111-1111-1111-111111111111",
            "displayName": "Demo One",
            "tenantId": "tenant-a",
            "state": "Enabled",
        },
        {
            "subscriptionId": "22222222-2222-2222-2222-222222222222",
            "displayName": "Other",
            "tenantId": "tenant-b",
            "state": "Enabled",
        },
    ]
    monkeypatch.setattr(me_module, "_list_visible_subscriptions", lambda: (fake, None))

    res = client.get("/api/me")
    assert res.status_code == 200
    body = res.json()
    assert body["subscriptions"] == fake
    assert "subscriptions_error" not in body


def test_me_requires_caller(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without AUTH_DEV_BYPASS, anonymous requests must be rejected.

    `require_caller` reads `AUTH_DEV_BYPASS` lazily on every call, so simply
    flipping the env var is enough — no module reload required. The previous
    importlib.reload approach was brittle (any other test that had already
    imported `api.main` could see torn-down state).
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "false")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    res = client.get("/api/me")
    assert res.status_code in (401, 403)

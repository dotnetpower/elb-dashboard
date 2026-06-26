"""Tests for /api/settings/webhooks routes.

Responsibility: Route contracts — GET unset, PUT valid (masked) / invalid (400),
and the test-send endpoint.
Edit boundaries: Test-only; monkeypatches the config service + post.
Key entry points: pytest test functions.
Risky contracts: PUT rejects SSRF-failing URLs (400); responses mask the URL.
Validation: ``uv run pytest -q api/tests/test_settings_webhooks.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services import webhooks_pref as wp
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    from api.main import app

    return TestClient(app)


def test_get_unset(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.services.webhooks_pref.get_config", lambda: None, raising=True)
    r = client.get("/api/settings/webhooks")
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_put_valid_returns_masked(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_save(*, url: str, enabled: bool, events: str, owner_oid: str = "") -> Any:
        captured.update({"url": url, "enabled": enabled})
        return wp.WebhookConfig(url=url, enabled=enabled, events=events, updated_at="t")

    monkeypatch.setattr("api.services.webhooks_pref.save_config", fake_save, raising=True)
    r = client.put(
        "/api/settings/webhooks",
        json={
            "url": "https://hooks.slack.com/services/T/B/secret",
            "enabled": True,
            "events": "terminal",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert "secret" not in body["url_masked"]


def test_put_invalid_url_400(client: TestClient) -> None:
    r = client.put(
        "/api/settings/webhooks",
        json={"url": "https://evil.com/x", "enabled": True, "events": "terminal"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_webhook_url"


def test_test_send_not_configured_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("api.services.webhooks_pref.get_config", lambda: None, raising=True)
    r = client.post("/api/settings/webhooks/test")
    assert r.status_code == 400
    assert r.json()["code"] == "not_configured"


def test_test_send_delivers(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.webhooks_pref.get_config",
        lambda: wp.WebhookConfig(
            url="https://hooks.slack.com/services/a/b/c", enabled=True, events="terminal"
        ),
        raising=True,
    )
    monkeypatch.setattr("api.tasks.webhooks.post_webhook", lambda url, msg: True, raising=True)
    r = client.post("/api/settings/webhooks/test")
    assert r.status_code == 200
    assert r.json()["delivered"] is True

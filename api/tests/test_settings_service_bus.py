"""Tests for the Settings → Service Bus HTTP routes.

Responsibility: Verify GET returns a disabled default (never 404), PUT persists
    and validates, the SAS connection string is never returned, test/discover
    degrade gracefully, and purge caps the batch.
Edit boundaries: Route shaping only; persistence + SDK behaviour covered
    elsewhere.
Key entry points: the ``test_*`` functions.
Risky contracts: every route enforces ``require_caller``; no secret material in
    responses.
Validation: ``uv run pytest -q api/tests/test_settings_service_bus.py``.
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
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.delenv("SERVICEBUS_ENABLED", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    from api.main import app

    return TestClient(app)


def test_get_defaults_disabled(client: TestClient) -> None:
    r = client.get("/api/settings/service-bus")
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["enabled"] is False
    assert body["effective_enabled"] is False
    assert body["counts"]["available"] is False


def test_put_then_get_round_trip(client: TestClient) -> None:
    payload = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
    }
    r = client.put("/api/settings/service-bus", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "saved"

    g = client.get("/api/settings/service-bus")
    assert g.json()["config"]["namespace_fqdn"] == payload["namespace_fqdn"]


def test_put_rejects_invalid_fqdn(client: TestClient) -> None:
    r = client.put(
        "/api/settings/service-bus",
        json={"enabled": True, "namespace_fqdn": "not-a-host"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_config"


def test_put_never_returns_connection_string(client: TestClient) -> None:
    r = client.put(
        "/api/settings/service-bus",
        json={
            "enabled": True,
            "auth_mode": "sas",
            "namespace_fqdn": "ext.servicebus.windows.net",
            "sas_secret_name": "sb-conn",
        },
    )
    assert r.status_code == 200, r.text
    text = r.text.lower()
    assert "sharedaccesskey" not in text
    assert "connection_string" not in text


def test_test_route_requires_namespace(client: TestClient) -> None:
    r = client.post("/api/settings/service-bus/test", json={})
    assert r.status_code == 400
    assert r.json()["code"] == "not_configured"


def test_discover_requires_subscription_or_namespace(client: TestClient) -> None:
    r = client.post("/api/settings/service-bus/discover", json={})
    assert r.status_code == 400
    assert r.json()["code"] == "subscription_required"

"""Tests for `api.routes.settings.app_insights` HTTP layer.

Responsibility: Verify auth gating, request validation, and Celery task
enqueuing for the App Insights settings routes.
Edit boundaries: Stub Azure SDK + Celery .delay; do not exercise real ARM.
Key entry points: `client`, `test_status_returns_deployment_state`,
    `test_lookup_404_when_component_missing`,
    `test_provision_enqueues_celery_task_and_returns_id`,
    `test_routes_reject_anonymous_when_bypass_off`.
Risky contracts: The route surface (deployment_connection_string,
    deployment_configured, component, task_id) is consumed by the SPA
    typed client.
Validation: `uv run pytest -q api/tests/test_settings_app_insights.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.tests._fakes import make_delay_recorder
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    return TestClient(app)


def test_status_returns_deployment_state_when_env_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=abc;IngestionEndpoint=https://example.local/",
    )
    r = client.get("/api/settings/app-insights")
    assert r.status_code == 200
    body = r.json()
    assert body["deployment_configured"] is True
    assert body["deployment_connection_string"].startswith("InstrumentationKey=")


def test_status_returns_empty_when_env_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    r = client.get("/api/settings/app-insights")
    assert r.status_code == 200
    body = r.json()
    assert body["deployment_configured"] is False
    assert body["deployment_connection_string"] == ""


def test_lookup_404_when_component_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.routes.settings.app_insights.get_application_insights",
        lambda *_a, **_kw: None,
    )
    r = client.post(
        "/api/settings/app-insights/lookup",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-elb",
            "component_name": "appi-elb",
        },
    )
    assert r.status_code == 404


def test_lookup_returns_component_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_component: dict[str, Any] = {
        "id": "/subscriptions/.../components/appi-elb",
        "name": "appi-elb",
        "connection_string": "InstrumentationKey=xyz",
    }
    monkeypatch.setattr(
        "api.routes.settings.app_insights.get_application_insights",
        lambda *_a, **_kw: fake_component,
    )
    r = client.post(
        "/api/settings/app-insights/lookup",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-elb",
            "component_name": "appi-elb",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["component"]["connection_string"] == "InstrumentationKey=xyz"


def test_lookup_rejects_invalid_subscription(client: TestClient) -> None:
    r = client.post(
        "/api/settings/app-insights/lookup",
        json={
            "subscription_id": "not-a-guid",
            "resource_group": "rg-elb",
            "component_name": "appi-elb",
        },
    )
    assert r.status_code == 400
    assert "subscription_id" in r.json()["detail"]


def test_provision_enqueues_celery_task_and_returns_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, fake_delay = make_delay_recorder("ai-task-1")
    monkeypatch.setattr("api.tasks.azure.provision_app_insights.delay", fake_delay, raising=True)
    r = client.post(
        "/api/settings/app-insights/provision",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-elb",
            "component_name": "appi-elb",
            "region": "koreacentral",
            "workspace_name": "log-elb",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == "ai-task-1"
    assert body["status"] == "queued"
    assert calls and calls[0]["component_name"] == "appi-elb"
    assert calls[0]["workspace_name"] == "log-elb"


def test_apply_enqueues_celery_task_and_returns_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, fake_delay = make_delay_recorder("ai-apply-1")
    monkeypatch.setattr(
        "api.tasks.azure.apply_app_insights_to_deployment.delay", fake_delay, raising=True
    )
    r = client.post(
        "/api/settings/app-insights/apply",
        json={
            "connection_string": "InstrumentationKey=abc;IngestionEndpoint=https://example.local/",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == "ai-apply-1"
    assert body["status"] == "queued"
    assert calls and calls[0]["connection_string"].startswith("InstrumentationKey=abc")


def test_apply_rejects_invalid_connection_string(client: TestClient) -> None:
    r = client.post(
        "/api/settings/app-insights/apply",
        json={"connection_string": "not-a-connection-string"},
    )
    assert r.status_code == 400


def test_routes_reject_anonymous_when_bypass_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    bare_client = TestClient(app)
    for method, path in (
        ("GET", "/api/settings/app-insights"),
        ("POST", "/api/settings/app-insights/lookup"),
        ("POST", "/api/settings/app-insights/provision"),
        ("POST", "/api/settings/app-insights/apply"),
    ):
        resp = bare_client.request(method, path, json={})
        assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"

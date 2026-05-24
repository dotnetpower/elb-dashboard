"""Tests for `api.routes.settings.aks_observability` HTTP layer.

Responsibility: Verify auth gating, request validation, status read, and
Celery task enqueuing for the AKS Container Insights settings routes.
Edit boundaries: Stub the service layer + Celery .delay; do not exercise real
ARM.
Key entry points: `client`, `test_status_returns_disabled_state`,
    `test_enable_enqueues_celery_task_and_returns_id`,
    `test_status_rejects_invalid_cluster_name`,
    `test_routes_reject_anonymous_when_bypass_off`.
Risky contracts: The response shape (`enabled`, `workspace_resource_id`,
    `task_id`) is consumed by the SPA typed client.
Validation: `uv run pytest -q api/tests/test_settings_aks_observability.py`.
"""

from __future__ import annotations

import pytest
from api.tests._fakes import make_delay_recorder
from azure.core.exceptions import ResourceNotFoundError
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    return TestClient(app)


def test_status_returns_disabled_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.routes.settings.aks_observability.get_container_insights_status",
        lambda *_a, **_kw: {
            "enabled": False,
            "workspace_resource_id": None,
            "cluster_provisioning_state": "Succeeded",
        },
    )
    r = client.get(
        "/api/settings/aks-observability",
        params={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False
    assert body["workspace_resource_id"] is None


def test_status_returns_enabled_state_with_workspace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = (
        "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-elb"
        "/providers/Microsoft.OperationalInsights/workspaces/log-elb"
    )
    monkeypatch.setattr(
        "api.routes.settings.aks_observability.get_container_insights_status",
        lambda *_a, **_kw: {
            "enabled": True,
            "workspace_resource_id": ws,
            "cluster_provisioning_state": "Succeeded",
        },
    )
    r = client.get(
        "/api/settings/aks-observability",
        params={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
        },
    )
    body = r.json()
    assert body["enabled"] is True
    assert body["workspace_resource_id"] == ws


def test_status_returns_404_when_cluster_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*_a: object, **_kw: object) -> dict[str, object]:
        raise ResourceNotFoundError("cluster gone")

    monkeypatch.setattr(
        "api.routes.settings.aks_observability.get_container_insights_status",
        _raise,
    )
    r = client.get(
        "/api/settings/aks-observability",
        params={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
        },
    )
    assert r.status_code == 404


def test_enable_enqueues_celery_task_and_returns_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, fake_delay = make_delay_recorder("aks-obs-task-1")
    monkeypatch.setattr(
        "api.tasks.azure.enable_aks_container_insights.delay", fake_delay, raising=True
    )
    ws = (
        "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-elb"
        "/providers/Microsoft.OperationalInsights/workspaces/log-elb"
    )
    r = client.post(
        "/api/settings/aks-observability/enable",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "workspace_resource_id": ws,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == "aks-obs-task-1"
    assert body["status"] == "queued"
    assert calls[0]["workspace_resource_id"] == ws


def test_disable_enqueues_celery_task_and_returns_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, fake_delay = make_delay_recorder("aks-obs-disable-1")
    monkeypatch.setattr(
        "api.tasks.azure.disable_aks_container_insights.delay", fake_delay, raising=True
    )
    r = client.post(
        "/api/settings/aks-observability/disable",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == "aks-obs-disable-1"
    assert body["status"] == "queued"
    assert calls[0]["cluster_name"] == "aks-elb"


def test_enable_rejects_invalid_workspace_resource_id(client: TestClient) -> None:
    r = client.post(
        "/api/settings/aks-observability/enable",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "workspace_resource_id": "not-an-arm-id",
        },
    )
    assert r.status_code == 400
    assert "workspace_resource_id" in r.json()["detail"]


def test_routes_reject_anonymous_when_bypass_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    bare_client = TestClient(app)
    resp = bare_client.get("/api/settings/aks-observability")
    assert resp.status_code in (401, 422)
    resp = bare_client.post("/api/settings/aks-observability/enable", json={})
    assert resp.status_code == 401
    resp = bare_client.post("/api/settings/aks-observability/disable", json={})
    assert resp.status_code == 401

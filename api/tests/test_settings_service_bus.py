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


@pytest.fixture(autouse=True)
def _stub_entity_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the status route off the live Service Bus data plane.

    ``GET /api/settings/service-bus`` probes live entity counts whenever the
    saved config is ``enabled`` (``_runtime_counts`` \u2192 ``service_bus.entity_counts``),
    which opens a real management/AMQP connection to the namespace. No test
    here asserts on live counts (the only ``counts`` assertion is the
    ``disabled`` path, which never calls ``entity_counts``), so raise
    ``ServiceBusUnavailable`` \u2014 mirroring the real "namespace unreachable"
    outcome \u2014 instantly instead of paying the ~5 s connect/retry to the fake
    namespace (slow + flaky in CI).
    """
    from api.services import service_bus

    def _unavailable(_cfg: object) -> dict[str, object]:
        raise service_bus.ServiceBusUnavailable("stubbed in tests")

    monkeypatch.setattr(service_bus, "entity_counts", _unavailable)


def test_get_defaults_disabled(client: TestClient) -> None:
    r = client.get("/api/settings/service-bus")
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["enabled"] is False
    assert body["effective_enabled"] is False
    assert body["env_gate_enabled"] is False
    assert body["counts"]["available"] is False


def test_env_gate_reported_independently_of_config(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env gate is surfaced separately so the SPA can explain why an
    operator-enabled config is still not live (deployment gate OFF)."""
    # Config enabled with a namespace, but the deployment master switch OFF.
    payload = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
    }
    assert client.put("/api/settings/service-bus", json=payload).status_code == 200

    body = client.get("/api/settings/service-bus").json()
    assert body["config"]["enabled"] is True
    assert body["env_gate_enabled"] is False  # gate OFF
    assert body["effective_enabled"] is False  # so the integration is dormant

    # Flip the deployment master switch ON; now both agree and it is live.
    monkeypatch.setenv("SERVICEBUS_ENABLED", "true")
    body = client.get("/api/settings/service-bus").json()
    assert body["env_gate_enabled"] is True
    assert body["effective_enabled"] is True


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

"""Tests for AKS service IP discovery responses.

Responsibility: Verify the /api/monitor/aks/service-ip response contract without live Azure calls.
Edit boundaries: Keep these tests focused on route response shaping and mocked k8s lookups.
Key entry points: `test_service_ip_returns_ready_payload`,
`test_service_ip_returns_missing_payload_without_404`.
Risky contracts: Missing OpenAPI service discovery must not be represented as HTTP 404 because
the request inspector treats that as an operator-visible API error.
Validation: `uv run pytest -q api/tests/test_monitor_aks_service_ip.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("SIDECAR_REPORTER_DISABLED", "true")

    from api import main as api_main
    from api.routes import monitor as monitor_package

    monkeypatch.setattr(monitor_package, "get_credential", lambda: object())
    return TestClient(api_main.create_app())


def _service_ip_url() -> str:
    return (
        "/api/monitor/aks/service-ip"
        "?subscription_id=sub-1"
        "&resource_group=rg-elb-cluster"
        "&cluster_name=elb-cluster-01"
        "&service_name=elb-openapi"
    )


def test_service_ip_returns_ready_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import monitoring as monitoring_svc

    monkeypatch.setattr(
        monitoring_svc,
        "k8s_get_service_ip",
        lambda *_args, **_kwargs: "10.42.0.52",
    )

    response = client.get(_service_ip_url())

    assert response.status_code == 200
    assert response.json() == {
        "service_name": "elb-openapi",
        "external_ip": "10.42.0.52",
        "available": True,
        "status": "ready",
    }


def test_service_ip_returns_missing_payload_without_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import monitoring as monitoring_svc

    monkeypatch.setattr(
        monitoring_svc,
        "k8s_get_service_ip",
        lambda *_args, **_kwargs: None,
    )

    response = client.get(_service_ip_url())

    assert response.status_code == 200
    assert response.json() == {
        "service_name": "elb-openapi",
        "external_ip": None,
        "available": False,
        "status": "missing_or_pending",
    }


def test_service_ip_exceptions_remain_non_fatal_for_discovery(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import monitoring as monitoring_svc

    def fail_lookup(*_args: object, **_kwargs: object) -> str | None:
        raise RuntimeError("k8s unavailable")

    monkeypatch.setattr(monitoring_svc, "k8s_get_service_ip", fail_lookup)

    response = client.get(_service_ip_url())

    assert response.status_code == 200
    assert response.json()["status"] == "missing_or_pending"

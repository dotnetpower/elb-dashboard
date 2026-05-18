from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def _patch_service_ip(monkeypatch: pytest.MonkeyPatch, ip: str | None) -> None:
    import api.services as services
    from api.services import k8s_monitoring

    monkeypatch.setattr(services, "get_credential", lambda: object())
    monkeypatch.setattr(k8s_monitoring, "k8s_get_service_ip", lambda *_args, **_kwargs: ip)


def test_openapi_proxy_forwards_try_it_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_service_ip(monkeypatch, "20.30.40.50")
    calls: list[dict[str, Any]] = []

    class StubAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> StubAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            content: bytes | None,
        ) -> httpx.Response:
            calls.append({"method": method, "url": url, "headers": headers, "content": content})
            return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)

    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/healthz",
        },
        headers={"Authorization": "Bearer should-not-forward", "Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert calls == [
        {
            "method": "GET",
            "url": "http://20.30.40.50/healthz",
            "headers": {"accept": "application/json"},
            "content": None,
        }
    ]


def test_openapi_proxy_returns_503_when_service_ip_missing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_service_ip(monkeypatch, None)

    response = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb", "path": "/healthz"},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "openapi_service_not_reachable"


def test_openapi_proxy_rejects_non_service_path(client: TestClient) -> None:
    response = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb", "path": "//example.test"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_openapi_path"

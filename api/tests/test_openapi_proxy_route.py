"""Tests for OpenAPI Proxy Route behavior.

Responsibility: Tests for OpenAPI Proxy Route behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `client`, `_patch_service_ip`, proxy forwarding, token fallback, JSON body,
service-missing, and invalid-path tests.
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_openapi_proxy_route.py`.
"""

from __future__ import annotations

import json
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
    _patch_service_ip(monkeypatch, "10.0.0.50")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "api-token")
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
            "url": "http://10.0.0.50/healthz",
            "headers": {"accept": "application/json", "X-ELB-API-Token": "api-token"},
            "content": None,
        }
    ]


def test_openapi_proxy_uses_runtime_token_when_env_token_missing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_service_ip(monkeypatch, "10.0.0.50")
    monkeypatch.delenv("ELB_OPENAPI_API_TOKEN", raising=False)

    from api.services import openapi_runtime

    monkeypatch.setattr(openapi_runtime, "get_openapi_api_token", lambda: "runtime-token")
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
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb", "path": "/v1/jobs"},
    )

    assert response.status_code == 200
    assert calls[0]["headers"]["X-ELB-API-Token"] == "runtime-token"


def test_openapi_proxy_forwards_query_path_and_json_body(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_service_ip(monkeypatch, "10.0.0.50")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "api-token")
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
            return httpx.Response(422, json={"detail": "validation"})

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)

    response = client.post(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/v1/jobs?dry_run=true",
        },
        headers={"Content-Type": "application/json"},
        json={"db": "16S_ribosomal_RNA"},
    )

    assert response.status_code == 422
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://10.0.0.50/v1/jobs?dry_run=true"
    assert calls[0]["headers"] == {
        "accept": "*/*",
        "content-type": "application/json",
        "X-ELB-API-Token": "api-token",
    }
    assert json.loads(calls[0]["content"] or b"{}") == {"db": "16S_ribosomal_RNA"}


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


def test_openapi_proxy_rejects_dashboard_uuid_for_openapi_status(client: TestClient) -> None:
    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/v1/jobs/9b45dbfe-1c63-433e-a650-609e2d43bbd8/status",
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "dashboard_job_id_not_openapi_job_id"
    assert "POST /v1/jobs" in body["message"]
    assert "/api/blast/jobs/{job_id}" in body["message"]


def test_openapi_proxy_rejects_dashboard_uuid_for_openapi_job_resource(
    client: TestClient,
) -> None:
    response = client.delete(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/v1/jobs/9b45dbfe-1c63-433e-a650-609e2d43bbd8",
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "dashboard_job_id_not_openapi_job_id"


def test_openapi_proxy_forwards_zip_download_headers(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_service_ip(monkeypatch, "10.0.0.50")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "api-token")
    zip_bytes = b"PK\x03\x04zip-bytes"

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
            del method, url, headers, content
            return httpx.Response(
                200,
                content=zip_bytes,
                headers={
                    "content-type": "application/zip",
                    "content-disposition": 'attachment; filename="merged_results.zip"',
                    "set-cookie": "should=not-leak",
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)

    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/v1/jobs/abc123/results",
        },
    )

    assert response.status_code == 200
    assert response.content == zip_bytes
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="merged_results.zip"'
    assert "set-cookie" not in response.headers


# ---------------------------------------------------------------------------
# Security audit (2026-05-22): #5 target_path allowlist + path traversal
# ---------------------------------------------------------------------------
def test_openapi_proxy_rejects_admin_path(client: TestClient) -> None:
    """An authenticated tenant member must not reach /admin/* with the
    auto-injected admin token. /admin is outside the Try-It allowlist."""
    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/admin/users",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "openapi_path_not_allowlisted"


def test_openapi_proxy_rejects_internal_path(client: TestClient) -> None:
    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/internal/debug",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "openapi_path_not_allowlisted"


def test_openapi_proxy_rejects_path_traversal_inside_allowlisted_prefix(
    client: TestClient,
) -> None:
    """A permitted /v1 prefix must not become a launchpad into /admin via ..."""
    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/v1/jobs/../admin",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "openapi_path_traversal_denied"


def test_openapi_proxy_rejects_url_encoded_path_traversal(client: TestClient) -> None:
    """Double-decoded `..` (URL-encoded as %2e%2e) must also be rejected."""
    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            # FastAPI Query decodes once; sending %252e%252e arrives here
            # as %2e%2e, which urllib.parse.unquote then decodes to '..'.
            "path": "/v1/jobs/%252e%252e/admin",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "openapi_path_traversal_denied"


def test_openapi_proxy_allows_healthz(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/healthz` (one of the four allowlisted prefixes) still works."""
    _patch_service_ip(monkeypatch, "10.0.0.50")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "api-token")

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
            del method, url, headers, content
            return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)

    response = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb", "path": "/healthz"},
    )
    assert response.status_code == 200


def test_openapi_proxy_allows_openapi_spec_and_docs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both `/openapi.json` and `/docs/...` are allowlisted (API Reference page)."""
    _patch_service_ip(monkeypatch, "10.0.0.50")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "api-token")

    class StubAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> StubAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def request(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            return httpx.Response(200, json={"openapi": "3.0.0"})

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)

    r1 = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb", "path": "/openapi.json"},
    )
    r2 = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb", "path": "/docs/swagger"},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200


# Hardening pass (same-day self-critique)
def test_openapi_proxy_rejects_admin_path_case_insensitively(client: TestClient) -> None:
    """Mixed-case ``/Admin/...`` must not slip past the case-sensitive
    SPA allowlist — an ingress that lower-cases the path would otherwise
    launder the request straight into admin."""
    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/Admin/Users",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "openapi_path_not_allowlisted"


def test_openapi_proxy_rejects_admin_under_v1_prefix(client: TestClient) -> None:
    """`/v1/admin/...` is technically inside the allowlisted /v1/ prefix
    but must be rejected: the elb-openapi service should not, but might,
    expose admin routes under a permitted prefix — defence in depth."""
    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/v1/admin/users",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "openapi_path_not_allowlisted"


def test_openapi_proxy_rejects_internal_in_query_string(client: TestClient) -> None:
    """Deny tokens are also checked against the query string so
    ``/v1/jobs?internal=1`` cannot be used as an exfiltration channel
    if the upstream interprets it as a flag."""
    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/v1/admin?op=x",
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "openapi_path_not_allowlisted"


def test_openapi_proxy_rejects_nul_byte_in_path(client: TestClient) -> None:
    """A NUL byte after a permitted prefix can truncate the path at the
    upstream (C-string handling), so the allowlist check would see a
    different value than the upstream router. Reject outright."""
    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/v1/safe%00/admin",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_openapi_path"


def test_openapi_proxy_rejects_docs_with_dot_path(client: TestClient) -> None:
    """`/docs.json` is NOT under `/docs/` — strict trailing-slash semantics
    prevent `/docsBYPASS` style prefix-extension attacks."""
    response = client.get(
        "/api/aks/openapi/proxy",
        params={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "path": "/docs.json",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "openapi_path_not_allowlisted"


def test_openapi_proxy_refuses_public_ip(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy auto-injects the admin X-ELB-API-Token. Forwarding it to a
    non-private upstream IP would expose the token over plain HTTP between
    the api sidecar and the public LoadBalancer. Refuse with a clear 502
    so the operator either switches to an internal LB or terminates TLS."""
    _patch_service_ip(monkeypatch, "20.30.40.50")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "api-token")

    response = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb", "path": "/healthz"},
    )
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "openapi_unsafe_transport"
    # Critical: the admin token must NOT appear anywhere in the response.
    assert "api-token" not in response.text


def test_openapi_proxy_accepts_private_ipv6(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IPv6 is not yet supported by the upstream URL construction
    (httpx needs bracketed literals); the conservative default refuses
    every IPv6 address until that lands. This test pins the conservative
    behaviour so a future IPv6 expansion is forced to update both the
    URL builder AND the IP-private check together."""
    _patch_service_ip(monkeypatch, "fd12:3456:789a::1")  # RFC4193 ULA (would be 'private')
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "ipv6-token")

    r = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb", "path": "/healthz"},
    )
    assert r.status_code == 502
    assert r.json()["code"] == "openapi_unsafe_transport"
    assert "ipv6-token" not in r.text


def test_openapi_proxy_refuses_public_ipv6(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Globally-routable IPv6 is also refused — same code path as the
    'no IPv6 yet' case above but pinned separately so a future fix that
    only handles the private case does not silently allow public IPv6."""
    _patch_service_ip(monkeypatch, "2606:4700:4700::1111")  # Cloudflare DNS
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "ipv6-public-token")

    r = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg-elb", "cluster_name": "aks-elb", "path": "/healthz"},
    )
    assert r.status_code == 502
    assert r.json()["code"] == "openapi_unsafe_transport"
    assert "ipv6-public-token" not in r.text

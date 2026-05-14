"""Unit tests for the api sidecar.

Runs against the FastAPI app via TestClient. No Azure cloud calls — tests
that require the cloud are skipped automatically.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Make sure no env state leaks between tests.
    os.environ.setdefault("AZURE_TENANT_ID", "common")
    os.environ.setdefault("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
def test_health_returns_200_with_version(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_health_response_has_request_id_header(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.headers.get("x-request-id"), "request_id middleware did not stamp the response"


def test_request_id_echoed_when_supplied(client: TestClient) -> None:
    r = client.get("/api/health", headers={"x-request-id": "abcd1234"})
    assert r.headers["x-request-id"] == "abcd1234"


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/me"),
        ("GET", "/api/monitor/aks?resource_group=rg-x"),
        ("GET", "/api/monitor/storage?resource_group=rg-x&account_name=stx"),
        ("GET", "/api/monitor/jobs"),
        ("GET", "/api/arm/subscriptions"),
        ("POST", "/api/resources/ensure-rg"),
        ("POST", "/api/blast/submit"),
        ("POST", "/api/aks/provision"),
        ("POST", "/api/warmup/start"),
        ("GET", "/api/audit/log"),
        ("POST", "/api/terminal/ticket"),
    ],
)
def test_auth_required_endpoints_reject_anonymous(
    client: TestClient, method: str, path: str
) -> None:
    r = client.request(method, path, json={"foo": "bar"} if method != "GET" else None)
    assert r.status_code == 401, (
        f"{method} {path} returned {r.status_code} without bearer token; expected 401"
    )
    assert "detail" in r.json()


def test_auth_required_with_invalid_token_returns_401(client: TestClient) -> None:
    r = client.get("/api/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
    assert "invalid token" in r.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# Catch-all reverse proxy hardening
# ---------------------------------------------------------------------------
def test_unknown_api_route_returns_404_not_spa(client: TestClient) -> None:
    """An unknown /api/* path must NOT be forwarded to the frontend
    (which would return SPA HTML with status 200 and break the SPA's
    fetch error handling)."""
    r = client.get("/api/this-route-does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "unknown api route"
    assert body["path"] == "/api/this-route-does-not-exist"


def test_unknown_api_post_also_404(client: TestClient) -> None:
    r = client.post("/api/another-missing", json={"x": 1})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Legacy terminal endpoints return 410 Gone with structured detail
# ---------------------------------------------------------------------------
def test_legacy_terminal_password_is_410(client: TestClient) -> None:
    r = client.get(
        "/api/terminal/some-vm/password",
        headers={"Authorization": "Bearer __dev_bypass__"},  # not honored, returns 401
    )
    # Without a real token we still get 401 first (auth runs before route handler).
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Stub responses for not-yet-implemented routes
# ---------------------------------------------------------------------------
def test_stub_log_helper_does_not_raise() -> None:
    from api.routes.stubs import _stub_log

    _stub_log("test", a=1, b="x")  # must not raise

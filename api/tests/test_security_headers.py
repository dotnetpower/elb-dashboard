"""Tests for the security-hardening batch on the api sidecar.

Responsibility: Lock in the security response headers, `Server` banner masking,
sanitized token-error envelope, `/openapi.json` docs gating, and the OpenAPI
bearer security scheme so a regression cannot silently re-open the surface.
Edit boundaries: Assertions only — no Azure or network calls. Builds isolated
apps via `create_app()` so the ENABLE_DOCS / STRICT_CSP env gates can be
exercised in both states.
Key entry points: `test_security_headers_present`,
`test_server_banner_masked`, `test_invalid_token_message_is_generic`,
`test_openapi_hidden_by_default`, `test_openapi_exposed_when_docs_enabled`,
`test_openapi_declares_bearer_security_scheme`, `test_csp_gate_off_by_default`,
`test_csp_gate_on_when_strict`.
Risky contracts: The always-on headers must stay persona-neutral (additive,
never strip access). `/openapi.json` must stay hidden when ENABLE_DOCS is unset.
Validation: `uv run pytest -q api/tests/test_security_headers.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def _fresh_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build an isolated app honouring the current env gates."""
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import create_app

    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Security response headers (finding #1) — always on, persona-neutral
# ---------------------------------------------------------------------------
def test_security_headers_present(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Strict-Transport-Security", "").startswith("max-age=")
    assert r.headers.get("Referrer-Policy")
    assert r.headers.get("Permissions-Policy")


def test_security_headers_on_401_responses(client: TestClient) -> None:
    # Headers must also apply to auth-rejected responses, not just 200s.
    r = client.get("/api/me")
    assert r.status_code == 401
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"


def test_server_banner_masked(client: TestClient) -> None:
    r = client.get("/api/health")
    # The api-sidecar response must not leak the framework banner.
    assert r.headers.get("Server") == "ElasticBLAST"


# ---------------------------------------------------------------------------
# CSP gate (finding #1) — default OFF per charter §12a Rule 4
# ---------------------------------------------------------------------------
def test_csp_gate_off_by_default(client: TestClient) -> None:
    r = client.get("/api/health")
    assert "Content-Security-Policy" not in r.headers


def test_csp_gate_on_when_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRICT_CSP", "true")
    c = _fresh_client(monkeypatch)
    r = c.get("/api/health")
    assert "default-src 'self'" in r.headers.get("Content-Security-Policy", "")


# ---------------------------------------------------------------------------
# Token error sanitization (finding #6)
# ---------------------------------------------------------------------------
def test_invalid_token_message_is_generic(client: TestClient) -> None:
    r = client.get("/api/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
    detail = r.json()["detail"]
    # Must not leak the PyJWT internal reason (e.g. "Not enough segments").
    assert detail == "invalid token"
    assert "segment" not in detail.lower()


# ---------------------------------------------------------------------------
# /openapi.json docs gating (finding #2)
# ---------------------------------------------------------------------------
def test_openapi_hidden_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_DOCS", raising=False)
    c = _fresh_client(monkeypatch)
    # With openapi_url=None the api no longer serves the spec route. The path
    # falls through to the catch-all frontend proxy (which returns nginx's 404
    # in production; the sidecar is absent in tests so it surfaces as 502).
    # Either way the machine-readable spec is NOT served by the api.
    assert c.get("/openapi.json").status_code != 200
    assert c.get("/api/docs").status_code == 404


def test_openapi_exposed_when_docs_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_DOCS", "true")
    c = _fresh_client(monkeypatch)
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/api/docs").status_code == 200


# ---------------------------------------------------------------------------
# OpenAPI bearer security scheme (finding #9)
# ---------------------------------------------------------------------------
def test_openapi_declares_bearer_security_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_DOCS", "true")
    c = _fresh_client(monkeypatch)
    spec = c.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert schemes["BearerAuth"]["type"] == "http"
    assert schemes["BearerAuth"]["scheme"] == "bearer"
    assert spec["security"] == [{"BearerAuth": []}]

"""Tests for the second api security-hardening batch.

Responsibility: Lock in the request-id error envelopes (#15), the 405-vs-404
distinction with an `Allow` header on unknown api routes (#7), the default-OFF
`STRICT_READINESS_DETAIL` body slimming (#3), the default-OFF
`ALLOW_ANONYMOUS_CLIENT_LOG` opt-in (#14), and the documented common error
responses in the OpenAPI spec (#10).
Edit boundaries: Assertions only — no Azure or network calls. Builds isolated
apps via `create_app()` so the env gates can be exercised in both states.
Key entry points: `test_error_body_carries_request_id`,
`test_unknown_api_route_returns_404`, `test_wrong_method_returns_405_with_allow`,
`test_readiness_detail_default_on`, `test_readiness_detail_stripped_when_strict`,
`test_client_log_requires_auth_by_default`,
`test_client_log_allows_anonymous_when_enabled`,
`test_openapi_documents_common_error_responses`.
Risky contracts: The default-OFF gates must preserve existing behaviour; the
405/404 guard must never forward unknown `/api/*` paths to the SPA.
Validation: `uv run pytest -q api/tests/test_api_hardening_batch2.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _fresh_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build an isolated app honouring the current env gates."""
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import create_app

    return TestClient(create_app())


# ---------------------------------------------------------------------------
# #15 — request_id in error envelopes
# ---------------------------------------------------------------------------
def test_error_body_carries_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _fresh_client(monkeypatch)
    r = client.get("/api/me")
    assert r.status_code == 401
    body = r.json()
    assert body.get("request_id")
    assert body["request_id"] == r.headers.get("X-Request-ID")


def test_validation_error_carries_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    client = _fresh_client(monkeypatch)
    # Auth passes via dev-bypass, so an invalid body reaches the 422 handler.
    r = client.post("/api/client-log", json={"level": "not-a-level"})
    assert r.status_code == 422
    body = r.json()
    assert "detail" in body
    assert body.get("request_id")


# ---------------------------------------------------------------------------
# #7 — 405-vs-404 distinction on unknown api routes
# ---------------------------------------------------------------------------
def test_unknown_api_route_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _fresh_client(monkeypatch)
    r = client.get("/api/this-route-does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body.get("detail") == "unknown api route"
    assert body.get("request_id")


def test_wrong_method_returns_405_with_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _fresh_client(monkeypatch)
    # `/api/health` only serves GET; a DELETE falls through to the catch-all,
    # which detects the partial match and answers 405 + Allow.
    r = client.delete("/api/health")
    assert r.status_code == 405
    assert r.json().get("detail") == "method not allowed"
    allow = r.headers.get("Allow", "")
    assert "GET" in allow


# ---------------------------------------------------------------------------
# #3 — STRICT_READINESS_DETAIL gate
# ---------------------------------------------------------------------------
def test_readiness_detail_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRICT_READINESS_DETAIL", raising=False)
    client = _fresh_client(monkeypatch)
    r = client.get("/api/health/ready")
    body = r.json()
    assert "components" in body


def test_readiness_detail_stripped_when_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRICT_READINESS_DETAIL", "true")
    client = _fresh_client(monkeypatch)
    r = client.get("/api/health/ready")
    body = r.json()
    assert "components" not in body
    assert body.get("status") in {"ready", "not_ready"}


# ---------------------------------------------------------------------------
# #14 — ALLOW_ANONYMOUS_CLIENT_LOG gate
# ---------------------------------------------------------------------------
def test_client_log_requires_auth_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOW_ANONYMOUS_CLIENT_LOG", raising=False)
    client = _fresh_client(monkeypatch)
    r = client.post(
        "/api/client-log", json={"level": "error", "message": "boom"}
    )
    assert r.status_code == 401


def test_client_log_allows_anonymous_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_ANONYMOUS_CLIENT_LOG", "true")
    client = _fresh_client(monkeypatch)
    r = client.post(
        "/api/client-log", json={"level": "error", "message": "boom"}
    )
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# #10 — common error responses documented in the OpenAPI spec
# ---------------------------------------------------------------------------
def test_openapi_documents_common_error_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_DOCS", "true")
    client = _fresh_client(monkeypatch)
    spec = client.get("/openapi.json").json()
    assert "ErrorResponse" in spec["components"]["schemas"]
    # Pick the always-present auth-gated `/api/me` GET operation.
    op = spec["paths"]["/api/me"]["get"]
    for code in ("401", "403", "404", "500"):
        assert code in op["responses"], f"missing {code} response on /api/me"

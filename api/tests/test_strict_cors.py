"""Strict CORS narrowing tests (audit P2 #12).

Module summary: When `STRICT_CORS=true`, the CORSMiddleware allow_methods
and allow_headers are narrowed from wildcard to an explicit allowlist.
When the flag is unset the behaviour is the legacy wildcard so callers
that were working today keep working.

Responsibility: Cover both the ON and OFF paths per charter §12a Rule 4.
Edit boundaries: Focus on the CORS preflight reply — request-handler
behaviour is exercised elsewhere.
Key entry points: per-test functions.
Risky contracts: Default OFF (`STRICT_CORS` unset) must keep `*` for
methods and headers — flipping the gate is a separate post-soak PR.
Validation: `uv run pytest -q api/tests/test_strict_cors.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _cors_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("STRICT_CORS", raising=False)
    monkeypatch.delenv("STRICT_CORS_ALLOW_METHODS", raising=False)
    monkeypatch.delenv("STRICT_CORS_ALLOW_HEADERS", raising=False)
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://localhost:8090")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    yield


def _preflight(client: TestClient, *, method: str, headers: str) -> dict[str, str]:
    """Issue a CORS preflight and return the response headers (lowercased)."""
    r = client.options(
        "/api/health",
        headers={
            "Origin": "https://localhost:8090",
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )
    # Starlette returns 200 on a successful preflight, 400 on rejection.
    return {k.lower(): v for k, v in r.headers.items()} | {"_status": str(r.status_code)}


# ---------------------------------------------------------------------------
# Default OFF: legacy wildcard behaviour preserved.
# ---------------------------------------------------------------------------


def test_strict_cors_off_echoes_request_method(_cors_env: None) -> None:
    """With STRICT_CORS unset, any HTTP method is accepted at preflight."""
    from api.main import create_app

    client = TestClient(create_app())
    h = _preflight(client, method="PATCH", headers="X-Made-Up-Header")
    assert h["_status"] == "200"
    # Starlette expands `*` into the full default method list and echoes
    # the requested header — both indicate the wildcard codepath is active.
    methods = h.get("access-control-allow-methods", "")
    assert "PATCH" in methods
    assert "DELETE" in methods
    assert "OPTIONS" in methods


def test_strict_cors_off_echoes_request_headers(_cors_env: None) -> None:
    """With STRICT_CORS unset, any requested header is echoed back."""
    from api.main import create_app

    client = TestClient(create_app())
    h = _preflight(client, method="GET", headers="X-Some-Custom-Header")
    assert h["_status"] == "200"
    headers_allowed = h.get("access-control-allow-headers", "")
    # Starlette echoes the requested header verbatim under wildcard mode.
    assert "X-Some-Custom-Header" in headers_allowed


# ---------------------------------------------------------------------------
# Strict ON: narrow allowlists.
# ---------------------------------------------------------------------------


def test_strict_cors_on_allows_known_method(
    _cors_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET / POST / PUT / DELETE remain accepted under strict mode."""
    monkeypatch.setenv("STRICT_CORS", "true")
    from api.main import create_app

    client = TestClient(create_app())
    h = _preflight(client, method="POST", headers="Authorization")
    assert h["_status"] == "200"
    methods = h.get("access-control-allow-methods", "")
    assert "POST" in methods
    assert "*" not in methods


def test_strict_cors_on_rejects_unknown_method(
    _cors_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PATCH preflight is rejected (PATCH is not in the default allowlist)."""
    monkeypatch.setenv("STRICT_CORS", "true")
    from api.main import create_app

    client = TestClient(create_app())
    h = _preflight(client, method="PATCH", headers="Authorization")
    # Starlette returns 400 with no CORS headers when the preflight is rejected.
    assert h["_status"] == "400"


def test_strict_cors_on_allows_known_header(
    _cors_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authorization is in the default header allowlist under strict mode."""
    monkeypatch.setenv("STRICT_CORS", "true")
    from api.main import create_app

    client = TestClient(create_app())
    h = _preflight(client, method="GET", headers="Authorization")
    assert h["_status"] == "200"
    headers_allowed = h.get("access-control-allow-headers", "").lower()
    assert "authorization" in headers_allowed
    assert "*" not in headers_allowed


def test_strict_cors_on_rejects_unknown_header(
    _cors_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A header that is not in the allowlist is refused at preflight."""
    monkeypatch.setenv("STRICT_CORS", "true")
    from api.main import create_app

    client = TestClient(create_app())
    h = _preflight(client, method="GET", headers="X-Not-Allowed")
    assert h["_status"] == "400"


def test_strict_cors_on_honours_method_override(
    _cors_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators can extend the method allowlist via STRICT_CORS_ALLOW_METHODS."""
    monkeypatch.setenv("STRICT_CORS", "true")
    monkeypatch.setenv("STRICT_CORS_ALLOW_METHODS", "GET,POST,PATCH,OPTIONS")
    from api.main import create_app

    client = TestClient(create_app())
    h = _preflight(client, method="PATCH", headers="Authorization")
    assert h["_status"] == "200"
    assert "PATCH" in h.get("access-control-allow-methods", "")


def test_strict_cors_on_honours_header_override(
    _cors_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators can extend the header allowlist via STRICT_CORS_ALLOW_HEADERS."""
    monkeypatch.setenv("STRICT_CORS", "true")
    monkeypatch.setenv(
        "STRICT_CORS_ALLOW_HEADERS",
        "Authorization,Content-Type,X-Custom",
    )
    from api.main import create_app

    client = TestClient(create_app())
    h = _preflight(client, method="GET", headers="X-Custom")
    assert h["_status"] == "200"
    headers_allowed = h.get("access-control-allow-headers", "").lower()
    assert "x-custom" in headers_allowed

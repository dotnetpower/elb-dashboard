"""Regression tests for security-audit items #9 (CORS), #10 (caller IP), #11 (frontend strip).

Responsibility: Cover the small-radius hardening landed on 2026-05-22 for
audit items #9 (CORS wildcard + credentials guard at app boot) and #11
(strip Authorization / Cookie / X-ELB-API-Token before forwarding to the
frontend nginx sidecar). Caller-IP (#10) lives in
``test_storage_public_access.py`` next to its production module.
Edit boundaries: Keep these tests focused on the security invariants;
behavioural assertions for the frontend proxy proper (catch-all 404 for
``/api/*``, content streaming, header passthrough) belong elsewhere.
Key entry points: ``test_cors_wildcard_with_credentials_refuses_to_boot``,
``test_cors_explicit_origins_still_work``,
``test_frontend_proxy_strips_authorization``,
``test_frontend_proxy_strips_cookie_and_api_token``.
Risky contracts: The frontend sidecar must never see the caller's MSAL
bearer; CORS must never combine ``*`` with ``allow_credentials=True``.
Validation: ``uv run pytest -q api/tests/test_security_audit_bundle.py``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# #9 CORS wildcard + credentials guard
# ---------------------------------------------------------------------------
def test_cors_wildcard_with_credentials_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CORS_ALLOW_ORIGINS='*'`` must crash at create_app, not silently
    enable the OWASP-listed broken combination."""
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    # Import without the wildcard env first so the module-level
    # ``app = create_app()`` evaluation succeeds; otherwise the
    # RuntimeError would fire at import time and never reach pytest.raises.
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    from api.main import create_app

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    with pytest.raises(RuntimeError, match=r"CORS_ALLOW_ORIGINS='\*' is not allowed"):
        create_app()


def test_cors_explicit_origins_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Listing concrete origins (the supported pattern) must keep working."""
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://localhost:8090,https://dev.example")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")

    from api.main import create_app

    app = create_app()
    client = TestClient(app)
    # Hit a real GET so the CORS middleware actually emits the response
    # header (only sent when Origin is present).
    r = client.get("/api/health", headers={"Origin": "https://localhost:8090"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://localhost:8090"
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_cors_disabled_when_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var = no middleware (production posture: same-origin via ingress)."""
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")

    from api.main import create_app

    app = create_app()
    client = TestClient(app)
    r = client.get("/api/health", headers={"Origin": "https://attacker.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------------
# #11 Frontend proxy header strip
# ---------------------------------------------------------------------------
@pytest.fixture()
def app_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    from api.main import app

    return TestClient(app)


class _RecordingAsyncClient:
    """Test stub for httpx.AsyncClient used by the frontend proxy."""

    last_headers: dict[str, str] | None = None

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    def build_request(
        self,
        _method: str,
        _url: str,
        *,
        headers: dict[str, str],
        content: bytes | None,
    ) -> httpx.Request:
        del content
        _RecordingAsyncClient.last_headers = dict(headers)
        # Build a real httpx.Request so the proxy's ``client.send`` stub
        # below can ignore it without violating the protocol.
        return httpx.Request(_method, _url, headers=headers)

    async def send(self, _request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        del _request, stream
        # Return a streaming-friendly response so the proxy's
        # ``upstream_resp.aiter_raw()`` consumer works end-to-end.
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"<html>spa</html>"),
            headers={"content-type": "text/html"},
        )

    async def aclose(self) -> None:
        return None


def _patch_frontend_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the frontend proxy to use our recording client.

    The proxy caches its client at module scope so we have to reset it.
    """
    from api.routes import frontend_proxy

    frontend_proxy._client = _RecordingAsyncClient()
    _RecordingAsyncClient.last_headers = None


def test_frontend_proxy_strips_authorization(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MSAL bearer must NEVER reach the frontend sidecar."""
    _patch_frontend_client(monkeypatch)

    r = app_client.get(
        "/index.html",
        headers={
            "Authorization": "Bearer pretend-msal-jwt",
            "Accept": "text/html",
        },
    )
    assert r.status_code == 200
    forwarded = _RecordingAsyncClient.last_headers or {}
    # Case-insensitive: httpx may keep header keys as-sent.
    forwarded_lower = {k.lower(): v for k, v in forwarded.items()}
    assert "authorization" not in forwarded_lower, (
        f"authorization header leaked to frontend nginx: {forwarded}"
    )
    # Sanity: an unrelated header still passes through.
    assert forwarded_lower.get("accept") == "text/html"


def test_frontend_proxy_strips_cookie_and_api_token(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cookie + X-ELB-API-Token (admin token for elb-openapi) must also
    be stripped — they have no legitimate use at the frontend sidecar
    and would leak into the nginx access log."""
    _patch_frontend_client(monkeypatch)

    r = app_client.get(
        "/assets/app.js",
        headers={
            "Cookie": "session=foo",
            "X-ELB-API-Token": "should-not-leak",
        },
    )
    assert r.status_code == 200
    forwarded_lower = {
        k.lower(): v for k, v in (_RecordingAsyncClient.last_headers or {}).items()
    }
    assert "cookie" not in forwarded_lower
    assert "x-elb-api-token" not in forwarded_lower


# ---------------------------------------------------------------------------
# Hardening pass (same-day self-critique)
# ---------------------------------------------------------------------------
def test_cors_null_origin_with_credentials_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``null`` is the literal origin for sandboxed iframes / data: / file:.
    Combining it with allow_credentials=True is a textbook CSRF surface."""
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    from api.main import create_app

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://localhost:8090,null")
    with pytest.raises(RuntimeError, match=r"contains 'null'"):
        create_app()


def test_cors_entry_missing_scheme_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``localhost:8090`` (no scheme) is a common typo that would silently
    disable CORS for the intended origin — fail loudly instead."""
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    from api.main import create_app

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "localhost:8090")
    with pytest.raises(RuntimeError, match=r"not a valid scheme://host origin"):
        create_app()


def test_frontend_proxy_strips_x_forwarded_authorization_variants(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse-proxy auth chains use X-Forwarded-Authorization /
    X-Forwarded-User / X-Forwarded-Access-Token / X-Forwarded-Id-Token.
    If an ingress in front of the api adds any of these, they must NOT
    be laundered through to the frontend nginx sidecar."""
    _patch_frontend_client(monkeypatch)

    r = app_client.get(
        "/index.html",
        headers={
            "X-Forwarded-Authorization": "Bearer indirect-jwt",
            "X-Forwarded-User": "alice@example",
            "X-Forwarded-Access-Token": "should-not-leak",
            "X-Forwarded-Id-Token": "id-token-not-for-nginx",
            "Accept": "text/html",
        },
    )
    assert r.status_code == 200
    forwarded_lower = {
        k.lower(): v for k, v in (_RecordingAsyncClient.last_headers or {}).items()
    }
    for header in (
        "x-forwarded-authorization",
        "x-forwarded-user",
        "x-forwarded-access-token",
        "x-forwarded-id-token",
    ):
        assert header not in forwarded_lower, (
            f"{header} leaked to frontend nginx: {forwarded_lower}"
        )
    assert forwarded_lower.get("accept") == "text/html"


def test_caller_ip_lookup_url_must_be_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ELB_CALLER_IP_LOOKUP_URLS`` must reject any non-HTTPS entry at
    module import; a plaintext probe lets a network attacker forge the
    discovered IP (and the value lands in logs / UI)."""
    monkeypatch.setenv("ELB_CALLER_IP_LOOKUP_URLS", "http://attacker.example/ip")
    # Force a fresh import of the module so the env-var validation runs.
    import importlib
    import sys

    sys.modules.pop("api.services.storage_public_access", None)
    with pytest.raises(RuntimeError, match=r"must be https://"):
        importlib.import_module("api.services.storage_public_access")
    # Restore a clean module so other tests don't observe a half-loaded
    # state.
    monkeypatch.delenv("ELB_CALLER_IP_LOOKUP_URLS", raising=False)
    sys.modules.pop("api.services.storage_public_access", None)
    importlib.import_module("api.services.storage_public_access")

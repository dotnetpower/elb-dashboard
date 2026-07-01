"""Universal M2M shared-token auth tests for :func:`api.auth.require_caller`.

Module summary: The M2M ``X-ELB-API-Token`` path used to be limited to the
two read-only OpenAPI database catalogue routes; per operator policy
(2026-07) it is now folded into ``require_caller`` itself so **every**
``require_caller``-gated route accepts the shared token when the opt-in
``ALLOW_OPENAPI_TOKEN_AUTH`` gate is on. These tests pin the new contract by:

    * exercising the ``require_caller`` dependency directly with a valid /
      invalid shared token (unit level), and
    * hitting a real mutating route (``POST /api/aks/openapi/deploy``) that
      historically required an MSAL bearer, and confirming the shared token
      alone authenticates it end-to-end.

Responsibility: Auth-contract tests for the universal M2M shared-token path.
Edit boundaries: Keep this file focused on ``require_caller`` /
    ``require_caller_or_openapi_token`` (alias) behaviour; do NOT re-test the
    read-only OpenAPI database routes here — that is
    ``test_aks_openapi_databases.py``.
Key entry points: ``test_require_caller_*``, ``test_mutating_route_*``.
Risky contracts: The mutating-route case picks a specific POST route
    (``/api/aks/openapi/deploy``). If that route is retired or renamed, swap
    it for any other mutating ``require_caller``-gated route — the assertion
    is only that a mutating gate accepts the shared token, not the specifics
    of that particular endpoint.
Validation: ``uv run pytest -q api/tests/test_m2m_token_universal.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from api import auth
from api.auth import (
    OPENAPI_TOKEN_OID,
    AuthError,
    is_openapi_token_caller,
    require_caller,
    require_caller_or_openapi_token,
)
from fastapi.testclient import TestClient


def _run(coro: Any) -> Any:
    """Drive an async dependency coroutine to completion in a sync test."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Unit level — require_caller dependency itself.
# ---------------------------------------------------------------------------


def test_require_caller_accepts_shared_token_when_gate_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("ALLOW_OPENAPI_TOKEN_AUTH", "true")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "secret-tok")

    identity = _run(require_caller(authorization=None, x_elb_api_token="secret-tok"))

    assert identity.object_id == OPENAPI_TOKEN_OID
    assert is_openapi_token_caller(identity)


def test_require_caller_rejects_wrong_shared_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("ALLOW_OPENAPI_TOKEN_AUTH", "true")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "secret-tok")

    with pytest.raises(AuthError) as exc:
        _run(require_caller(authorization=None, x_elb_api_token="WRONG"))
    assert exc.value.status_code == 401
    assert "X-ELB-API-Token" in exc.value.detail


def test_require_caller_gate_off_ignores_shared_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the gate OFF the header is inert and the bearer path runs."""
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.delenv("ALLOW_OPENAPI_TOKEN_AUTH", raising=False)
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "secret-tok")

    with pytest.raises(AuthError) as exc:
        _run(require_caller(authorization=None, x_elb_api_token="secret-tok"))
    # NOT the token-path error — proves the gate short-circuits M2M cleanly.
    assert exc.value.detail == "missing bearer token"


def test_require_caller_gate_on_but_expected_empty_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("ALLOW_OPENAPI_TOKEN_AUTH", "true")
    monkeypatch.delenv("ELB_OPENAPI_API_TOKEN", raising=False)
    monkeypatch.setattr(
        "api.services.openapi.runtime.get_openapi_api_token",
        lambda *a, **k: "",
    )

    with pytest.raises(AuthError) as exc:
        _run(require_caller(authorization=None, x_elb_api_token="any"))
    assert exc.value.status_code == 401


def test_require_caller_or_openapi_token_is_alias() -> None:
    """The legacy alias must resolve to the same dependency object.

    Existing callers (``api/routes/aks/openapi_databases.py``, tests, docs)
    still import ``require_caller_or_openapi_token``; keeping it as a
    same-object alias is what preserves FastAPI's per-dependency caching
    within a request.
    """
    assert require_caller_or_openapi_token is require_caller


def test_require_caller_dev_bypass_wins_over_shared_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH_DEV_BYPASS keeps its precedence even with the M2M gate ON."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("ALLOW_OPENAPI_TOKEN_AUTH", "true")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "secret-tok")

    identity = _run(require_caller(authorization=None, x_elb_api_token="secret-tok"))

    # Dev bypass identity, not the M2M sentinel.
    assert identity.object_id == auth.DEV_BYPASS_OID
    assert not is_openapi_token_caller(identity)


# ---------------------------------------------------------------------------
# Integration level — a real mutating route authed by the shared token alone.
# ---------------------------------------------------------------------------


@pytest.fixture()
def m2m_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Real-auth mode (no dev bypass) with the M2M gate on."""
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("ALLOW_OPENAPI_TOKEN_AUTH", "true")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "secret-tok")
    from api.main import app

    return TestClient(app)


def test_mutating_route_accepts_shared_token(m2m_client: TestClient) -> None:
    """POST /api/aks/openapi/deploy — a mutating action — must accept M2M.

    The route validates the JSON body before doing any work; sending an
    empty body triggers the route's 400 ``missing_parameters`` branch
    (well after the auth gate). A 400 therefore proves the auth gate
    accepted the shared token — a bearer-only revision would 401 first.
    """
    resp = m2m_client.post(
        "/api/aks/openapi/deploy",
        headers={"X-ELB-API-Token": "secret-tok"},
        json={},
    )
    assert resp.status_code == 400
    # An error middleware flattens the HTTPException `detail` onto the top
    # level, so `code` lives at the root of the response body.
    assert resp.json()["code"] == "missing_parameters"


def test_mutating_route_rejects_wrong_shared_token(m2m_client: TestClient) -> None:
    resp = m2m_client.post(
        "/api/aks/openapi/deploy",
        headers={"X-ELB-API-Token": "WRONG"},
        json={},
    )
    assert resp.status_code == 401
    assert "X-ELB-API-Token" in resp.json()["detail"]


def test_mutating_route_no_auth_still_rejects(m2m_client: TestClient) -> None:
    """Gate ON but no token header + no bearer -> 401, unchanged bearer path."""
    resp = m2m_client.post("/api/aks/openapi/deploy", json={})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "missing bearer token"

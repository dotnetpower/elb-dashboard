"""Per-token rate-limit behavior tests for the OpenAPI submit surface.

Responsibility: Verify the OpenAPI rate-limit middleware throttles the right
    paths, lets unrelated paths through, returns 429 + Retry-After on overflow,
    and keys correctly by `X-ELB-API-Token` then falls back to client IP.
Edit boundaries: Test-only. If the middleware grows new path prefixes or key
    sources, add an explicit assertion here so regressions surface immediately.
Key entry points: `test_*` functions.
Risky contracts: Tests set a tiny `OPENAPI_RATE_LIMIT_REQUESTS_PER_WINDOW` via
    monkeypatch so they finish quickly. The middleware re-reads the env on every
    request — do not refactor it to a module-level constant without keeping a
    test-friendly hook.
Validation: `uv run pytest -q api/tests/test_openapi_rate_limit.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _build_client(monkeypatch: pytest.MonkeyPatch, *, limit: int, window: float = 60.0) -> TestClient:
    """Build a fresh FastAPI app with the rate-limit middleware tuned for tests."""
    monkeypatch.setenv("OPENAPI_RATE_LIMIT_REQUESTS_PER_WINDOW", str(limit))
    monkeypatch.setenv("OPENAPI_RATE_LIMIT_WINDOW_SECONDS", str(window))
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    # Make sure the rate-limit middleware is enabled even if a previous test
    # turned it off.
    monkeypatch.delenv("OPENAPI_RATE_LIMIT_DISABLED", raising=False)
    # Re-import api.main so the middleware picks up the new env values.
    import importlib
    import api.main as api_main

    importlib.reload(api_main)
    return TestClient(api_main.app)


def test_rate_limit_passes_when_under_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _build_client(monkeypatch, limit=5)
    # Health endpoint isn't a limited path, so this should never 429
    # regardless of how many times we hit it.
    for _ in range(20):
        r = client.get("/api/health")
        assert r.status_code in (200, 503), r.status_code


def test_rate_limit_rejects_after_quota_on_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _build_client(monkeypatch, limit=3)
    headers = {"X-ELB-API-Token": "test-token-1"}
    # First 3 requests get through (they'll fail with a non-429 status because
    # we don't mock the upstream; that's fine — we're checking the middleware
    # didn't 429 them).
    statuses: list[int] = []
    for _ in range(3):
        # Missing required `resource_group` etc. — backend will 422/400 but
        # the request still passes through the middleware. That's all we care
        # about here.
        r = client.get(
            "/api/aks/openapi/proxy",
            params={"resource_group": "rg", "cluster_name": "c", "path": "/healthz"},
            headers=headers,
        )
        statuses.append(r.status_code)
        assert r.status_code != 429
    # 4th request must be 429.
    r = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg", "cluster_name": "c", "path": "/healthz"},
        headers=headers,
    )
    assert r.status_code == 429
    body = r.json()
    assert body["code"] == "rate_limited"
    assert body["retry_after_seconds"] >= 1
    assert r.headers.get("Retry-After") == str(body["retry_after_seconds"])
    assert body["key_kind"] == "token"


def test_rate_limit_keyed_by_token_not_shared_across_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client(monkeypatch, limit=2)
    # Caller A burns through their quota.
    for _ in range(2):
        client.get(
            "/api/aks/openapi/proxy",
            params={"resource_group": "rg", "cluster_name": "c", "path": "/healthz"},
            headers={"X-ELB-API-Token": "caller-a"},
        )
    blocked = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg", "cluster_name": "c", "path": "/healthz"},
        headers={"X-ELB-API-Token": "caller-a"},
    )
    assert blocked.status_code == 429

    # Caller B with a different token must still get through.
    ok = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg", "cluster_name": "c", "path": "/healthz"},
        headers={"X-ELB-API-Token": "caller-b"},
    )
    assert ok.status_code != 429


def test_rate_limit_falls_back_to_ip_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client(monkeypatch, limit=2)
    for _ in range(2):
        client.get(
            "/api/aks/openapi/proxy",
            params={"resource_group": "rg", "cluster_name": "c", "path": "/healthz"},
        )
    r = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg", "cluster_name": "c", "path": "/healthz"},
    )
    assert r.status_code == 429
    assert r.json()["key_kind"] == "ip"


def test_rate_limit_does_not_affect_unrelated_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_client(monkeypatch, limit=2)
    # Burn through the OpenAPI quota.
    for _ in range(2):
        client.get(
            "/api/aks/openapi/proxy",
            params={"resource_group": "rg", "cluster_name": "c", "path": "/healthz"},
            headers={"X-ELB-API-Token": "shared-token"},
        )
    blocked = client.get(
        "/api/aks/openapi/proxy",
        params={"resource_group": "rg", "cluster_name": "c", "path": "/healthz"},
        headers={"X-ELB-API-Token": "shared-token"},
    )
    assert blocked.status_code == 429

    # Unrelated dashboard route (`/api/health`) must NOT be throttled.
    r = client.get("/api/health", headers={"X-ELB-API-Token": "shared-token"})
    assert r.status_code != 429


def test_rate_limit_can_be_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAPI_RATE_LIMIT_DISABLED", "true")
    client = _build_client(monkeypatch, limit=1)
    # The setenv inside _build_client cleared OPENAPI_RATE_LIMIT_DISABLED;
    # re-apply it after the reload so the middleware sees the override.
    monkeypatch.setenv("OPENAPI_RATE_LIMIT_DISABLED", "true")
    headers = {"X-ELB-API-Token": "stress"}
    for _ in range(5):
        r = client.get(
            "/api/aks/openapi/proxy",
            params={"resource_group": "rg", "cluster_name": "c", "path": "/healthz"},
            headers=headers,
        )
        assert r.status_code != 429

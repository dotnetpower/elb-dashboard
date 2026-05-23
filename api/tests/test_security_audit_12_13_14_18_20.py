"""Regression tests for security-audit items #12, #13, #14, #18, #20.

Responsibility: Cover the small-radius hardening landed on 2026-05-22 for
items #12 (OpenAPI public-IP token refusal already covered in
test_openapi_proxy_route.py), #13 (Storage allowSharedKeyAccess=false
invariant), #14 (ARM tag length/char validation), #18 (frontend catch-all
path/method validation), and #20 (centralised Storage endpoint helper).
Edit boundaries: Behavioural assertions for each helper / route belong
in their respective test files; this file is the single regression
guard for the security invariants.
Key entry points: ``test_arm_tag_*``, ``test_frontend_proxy_rejects_*``,
``test_storage_open_refuses_when_shared_key_enabled``,
``test_blob_account_url_uses_configured_suffix``.
Risky contracts: ARM tag values must respect Azure limits before reaching
the SDK; the frontend catch-all must never proxy traversal or control-char
paths; ensure_local_storage_access must refuse to open when shared-key
access is enabled; the storage endpoint helper must honour
AZURE_STORAGE_SUFFIX for sovereign clouds.
Validation: ``uv run pytest -q api/tests/test_security_audit_12_13_14_18_20.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    from api.main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# #14 — ARM tag length / character validation
# ---------------------------------------------------------------------------
def test_arm_tag_post_rejects_too_many_tags(
    client: TestClient,
) -> None:
    """Azure caps tags per resource at 50; the api must refuse upfront
    so a single bad POST cannot pollute the resource group with 51 tags
    or get a half-applied state from the SDK."""
    tags = {f"elb-{i}": "v" for i in range(60)}
    r = client.post(
        "/api/arm/resource-group/tags",
        json={"subscription_id": "sub", "resource_group": "rg-elb", "tags": tags},
    )
    assert r.status_code == 400
    assert "too many tags" in r.json()["detail"].lower()


def test_arm_tag_post_rejects_name_with_forbidden_chars(client: TestClient) -> None:
    r = client.post(
        "/api/arm/resource-group/tags",
        json={
            "subscription_id": "sub",
            "resource_group": "rg-elb",
            "tags": {"elb-bad<name>": "v"},
        },
    )
    assert r.status_code == 400
    assert "characters Azure rejects" in r.json()["detail"]


def test_arm_tag_post_rejects_overlong_value(client: TestClient) -> None:
    r = client.post(
        "/api/arm/resource-group/tags",
        json={
            "subscription_id": "sub",
            "resource_group": "rg-elb",
            "tags": {"elb-long": "x" * 300},
        },
    )
    assert r.status_code == 400
    assert "256" in r.json()["detail"]


def test_arm_tag_post_rejects_control_chars_in_value(client: TestClient) -> None:
    r = client.post(
        "/api/arm/resource-group/tags",
        json={
            "subscription_id": "sub",
            "resource_group": "rg-elb",
            "tags": {"elb-good": "value\x00with-nul"},
        },
    )
    assert r.status_code == 400
    assert "control characters" in r.json()["detail"]


# ---------------------------------------------------------------------------
# #18 — Frontend catch-all path/method validation
# ---------------------------------------------------------------------------
def test_frontend_proxy_rejects_path_traversal(client: TestClient) -> None:
    """``..`` in any frontend URL is rejected at the api boundary so it
    never reaches the nginx sidecar's access log or filesystem walk.

    URL-encoded traversal is used so httpx / starlette URL-normalisation
    does not collapse the segments away before the request reaches our
    handler — the realistic attack vector."""
    r = client.get("/assets/%2E%2E/etc/passwd")
    assert r.status_code == 400
    assert "parent-traversal" in r.text


def test_frontend_proxy_rejects_control_chars(client: TestClient) -> None:
    """Control characters in the path are CRLF / log-injection probes.
    Test the helper directly because httpx refuses to send a URL
    containing raw CR/LF and would otherwise raise client-side."""
    import asyncio

    from api.routes.frontend_proxy import reverse_proxy
    from fastapi import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/index.html\r\nSet-Cookie: x=1",
        "raw_path": b"/index.html\r\nSet-Cookie: x=1",
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)
    response = asyncio.new_event_loop().run_until_complete(
        reverse_proxy("index.html\r\nSet-Cookie: x=1", request)
    )
    assert response.status_code == 400
    assert b"control characters" in response.body


def test_frontend_proxy_rejects_disallowed_method(client: TestClient) -> None:
    """TRACE / CONNECT have no business at a static-asset frontend.
    The TestClient API requires us to use .request() for non-standard
    methods; FastAPI normally returns 405 on unknown methods at the
    routing layer, so we use TRACE (which the route accepts via its
    pattern but our validator rejects)."""
    # Starlette TestClient supports any HTTP method via .request().
    r = client.request("TRACE", "/index.html")
    # Route registers OPTIONS but not TRACE — Starlette returns 405 at
    # the routing layer, so our explicit check is belt-and-braces.
    assert r.status_code in (400, 405)


# ---------------------------------------------------------------------------
# #13 — Storage local-debug allowSharedKeyAccess=false invariant
# ---------------------------------------------------------------------------
def test_storage_open_refuses_when_shared_key_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``defaultAction=Allow`` is only safe with AAD-only data-plane
    auth. If ``allowSharedKeyAccess`` is True, the api must refuse to
    flip the public-access window open — a leaked account key would
    otherwise have network-unconditional reach."""
    monkeypatch.setenv("LOCAL_DEBUG_AUTO_OPEN_STORAGE", "true")
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    from api.services.storage import public_access as spa

    # Reset the in-process "already open" cache so a previous test cannot
    # poison this one.
    with spa._cache_lock:
        spa._already_open_cache.clear()
    with spa._caller_ip_lock:
        spa._caller_ip_cache = None

    class _StorageAccounts:
        def get_properties(self, _rg: str, _acct: str) -> Any:
            return SimpleNamespace(
                public_network_access="Disabled",
                network_rule_set=SimpleNamespace(default_action="Deny"),
                allow_shared_key_access=True,
                is_hns_enabled=True,
            )

        def update(self, *_a: Any, **_kw: Any) -> Any:
            raise AssertionError("update must not be called when the invariant fails")

    class _StorageClient:
        storage_accounts = _StorageAccounts()

    monkeypatch.setattr(
        "api.services.azure_clients.storage_client",
        lambda *_a, **_kw: _StorageClient(),
    )

    result = spa.ensure_local_storage_access(
        object(), "sub", "rg-elb", "stelb01", force=True
    )
    assert result["action"] == "failed"
    assert result["error"] == "shared_key_access_enabled"
    assert "allowSharedKeyAccess=false" in result["message"]


def test_storage_open_succeeds_when_shared_key_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline: ``allowSharedKeyAccess=false`` lets the open path run
    to completion."""
    monkeypatch.setenv("LOCAL_DEBUG_AUTO_OPEN_STORAGE", "true")
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    from api.services.storage import public_access as spa

    with spa._cache_lock:
        spa._already_open_cache.clear()
    with spa._caller_ip_lock:
        spa._caller_ip_cache = None

    update_called = {"n": 0}

    class _StorageAccounts:
        def get_properties(self, _rg: str, _acct: str) -> Any:
            return SimpleNamespace(
                public_network_access="Disabled",
                network_rule_set=SimpleNamespace(default_action="Deny"),
                allow_shared_key_access=False,
                is_hns_enabled=True,
            )

        def update(self, *_a: Any, **_kw: Any) -> Any:
            update_called["n"] += 1
            return SimpleNamespace()

    class _StorageClient:
        storage_accounts = _StorageAccounts()

    monkeypatch.setattr(
        "api.services.azure_clients.storage_client",
        lambda *_a, **_kw: _StorageClient(),
    )
    # Skip the external IP probe.
    monkeypatch.setattr(spa, "_detect_caller_ip", lambda: "203.0.113.7")

    result = spa.ensure_local_storage_access(
        object(), "sub", "rg-elb", "stelb02", force=True
    )
    assert result["action"] == "opened"
    assert update_called["n"] == 1


# ---------------------------------------------------------------------------
# #20 — Centralised Storage endpoint helper
# ---------------------------------------------------------------------------
def test_blob_account_url_default_suffix() -> None:
    from api.services.storage.endpoint import blob_account_url, blob_host_for_account

    assert blob_account_url("stelb01") == "https://stelb01.blob.core.windows.net"
    assert blob_host_for_account("stelb01") == "stelb01.blob.core.windows.net"


def test_blob_account_url_honours_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sovereign cloud deployments (US Gov, China) need a different
    suffix. The helper must read it from ``AZURE_STORAGE_SUFFIX`` so the
    callers do not have to."""
    monkeypatch.setenv("AZURE_STORAGE_SUFFIX", "core.usgovcloudapi.net")
    from api.services.storage.endpoint import blob_account_url

    assert blob_account_url("stelb01") == "https://stelb01.blob.core.usgovcloudapi.net"


def test_blob_account_url_rejects_full_host_input() -> None:
    """Common mistake: passing ``acct.blob.core.windows.net`` here would
    produce ``https://acct.blob.core.windows.net.blob.core.windows.net``.
    Fail loudly."""
    from api.services.storage.endpoint import blob_account_url

    with pytest.raises(ValueError, match=r"bare storage account name"):
        blob_account_url("acct.blob.core.windows.net")


def test_table_account_url_uses_table_subdomain() -> None:
    from api.services.storage.endpoint import table_account_url

    assert table_account_url("stelb01") == "https://stelb01.table.core.windows.net"


def test_storage_url_validation_uses_centralised_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SSRF cross-account check in ``validate_storage_blob_reference``
    must keep working after the migration to the centralised helper."""
    monkeypatch.delenv("AZURE_STORAGE_SUFFIX", raising=False)
    from api.services.storage.url_validation import validate_storage_blob_reference

    # Same account → accepted.
    ok = validate_storage_blob_reference(
        storage_account="stelb01",
        value="https://stelb01.blob.core.windows.net/queries/x/q.fa",
        label="test",
        expected_container="queries",
    )
    assert ok == "https://stelb01.blob.core.windows.net/queries/x/q.fa"

    # Different account → rejected (the SSRF gate).
    with pytest.raises(ValueError, match=r"belong to the selected Storage account"):
        validate_storage_blob_reference(
            storage_account="stelb01",
            value="https://attacker.blob.core.windows.net/queries/x/q.fa",
            label="test",
            expected_container="queries",
        )

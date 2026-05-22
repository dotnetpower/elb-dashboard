"""Tests for the auth-layer caches and dev bypass.

Responsibility: Tests for the auth-layer caches and dev bypass
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_reset_state`, `test_get_credential_returns_singleton`,
`test_reset_credential_creates_new_instance`,
`test_claims_cache_returns_cached_identity_within_ttl`, `test_claims_cache_evicts_after_ttl`,
`test_claims_cache_caps_ttl_at_max`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_auth_caching.py`.
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    """Make every test start with empty caches and a clean env."""
    from api import auth as auth_mod
    from api import services as services_pkg

    auth_mod.reset_caches()
    services_pkg.reset_credential()
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    yield
    auth_mod.reset_caches()
    services_pkg.reset_credential()


# ---------------------------------------------------------------------------
# Credential singleton
# ---------------------------------------------------------------------------
def test_get_credential_returns_singleton() -> None:
    from api.services import get_credential

    a = get_credential()
    b = get_credential()
    assert a is b, "get_credential() must reuse the cached Azure credential"


def test_reset_credential_creates_new_instance() -> None:
    from api.services import get_credential, reset_credential

    a = get_credential()
    reset_credential()
    b = get_credential()
    assert a is not b, "reset_credential() must invalidate the singleton"


# ---------------------------------------------------------------------------
# Claims cache
# ---------------------------------------------------------------------------
def _identity(oid: str = "00000000-0000-0000-0000-deadbeefcafe"):
    from api.auth import CallerIdentity

    return CallerIdentity(
        object_id=oid,
        tenant_id="t",
        upn="u",
        raw_token="",  # never persist the raw token in the cache fixture
        claims={"oid": oid},
    )


def test_claims_cache_returns_cached_identity_within_ttl() -> None:
    from api.auth import _claims_cache_get, _claims_cache_put, _token_cache_key

    token = "test-token-fresh"
    key = _token_cache_key(token)
    ident = _identity()

    _claims_cache_put(key, ident, exp_claim=time.time() + 600)
    assert _claims_cache_get(key) is ident


def test_claims_cache_evicts_after_ttl() -> None:
    from api.auth import _claims_cache_get, _claims_cache_put, _token_cache_key

    token = "test-token-stale"
    key = _token_cache_key(token)
    ident = _identity()

    # exp already in the past => ttl <= 0 => entry never inserted.
    _claims_cache_put(key, ident, exp_claim=time.time() - 1)
    assert _claims_cache_get(key) is None


def test_claims_cache_caps_ttl_at_max() -> None:
    """A token that says it lives for 1 day should still expire from our cache
    in <= 5 min so a revoked token cannot survive arbitrarily long."""
    from api import auth as auth_mod

    token = "test-token-long-exp"
    key = auth_mod._token_cache_key(token)
    ident = _identity()

    far_future = time.time() + 86400
    auth_mod._claims_cache_put(key, ident, exp_claim=far_future)
    expires_at, _ = auth_mod._CLAIMS_CACHE[key]
    cap = auth_mod._CLAIMS_CACHE_MAX_TTL_SECONDS
    assert expires_at <= time.time() + cap + 1, (
        f"cache TTL must be capped at {cap}s; got {expires_at - time.time():.0f}s"
    )


def test_claims_cache_key_does_not_leak_raw_token() -> None:
    """SHA-256 of the token must not contain the raw token substring."""
    from api.auth import _token_cache_key

    token = "header.payload.SECRET-SIGNATURE"
    key = _token_cache_key(token)
    assert "SECRET-SIGNATURE" not in key
    assert len(key) == 64  # sha256 hex digest


def test_claims_cache_soft_cap_evicts_when_full(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import auth as auth_mod

    monkeypatch.setattr(auth_mod, "_CLAIMS_CACHE_SOFT_CAP", 4)
    auth_mod.reset_caches()

    # Insert exactly _SOFT_CAP fresh entries.
    for i in range(4):
        auth_mod._claims_cache_put(f"key-{i}", _identity(), exp_claim=time.time() + 600)
    assert len(auth_mod._CLAIMS_CACHE) == 4

    # Inserting a 5th must trigger eviction so size stays at-or-below soft cap.
    auth_mod._claims_cache_put("key-new", _identity(), exp_claim=time.time() + 600)
    assert len(auth_mod._CLAIMS_CACHE) <= 4


# ---------------------------------------------------------------------------
# AUTH_DEV_BYPASS
# ---------------------------------------------------------------------------
def test_dev_bypass_returns_synthetic_identity_without_header() -> None:
    os.environ["AUTH_DEV_BYPASS"] = "true"
    try:
        import asyncio

        from api.auth import require_caller

        ident = asyncio.run(require_caller(authorization=None))
        assert ident.object_id == "00000000-0000-0000-0000-000000000000"
        assert ident.upn == "dev-bypass@local"
        assert ident.claims.get("dev_bypass") is True
        # Synthetic identity must carry an empty raw token so any code that
        # tries to use it for downstream auth fails loudly.
        assert ident.raw_token == ""
    finally:
        del os.environ["AUTH_DEV_BYPASS"]


def test_no_bypass_still_requires_bearer() -> None:
    """Without AUTH_DEV_BYPASS=true the dependency must reject empty headers."""
    import asyncio

    from api.auth import require_caller

    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_caller(authorization=None))
    assert exc.value.status_code == 401

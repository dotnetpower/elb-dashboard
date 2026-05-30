"""Strict-mode JWT validation tests (audit P1 #6 #9).

Module summary: When `STRICT_JWT=true` the bearer-token validator
additionally pins the token to a known app via the `azp`/`appid` claim
and caps the claims-cache TTL at 60 s so a revoked SPA cannot linger
in the cache for the legacy 5-minute window. When the flag is unset
the behaviour is unchanged.

Responsibility: Cover both the ON and OFF paths per charter §12a Rule 4.
Edit boundaries: Token shape helpers reuse the pattern from
  `test_security_audit_4_8.py`; do not duplicate the JWKS stub.
Key entry points: per-test functions.
Risky contracts: Default OFF must keep the existing AUTH_DEV_BYPASS and
  legacy-tenant-only flows working unchanged.
Validation: `uv run pytest -q api/tests/test_strict_jwt.py`.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from fastapi import HTTPException

_TEST_TENANT_ID = "11111111-2222-3333-4444-555555555555"
_TEST_API_CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_OTHER_APP_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"


def _make_token(claims: dict[str, Any], key: str) -> str:
    return jwt.encode(claims, key, algorithm="HS256")


@pytest.fixture()
def _auth_env(monkeypatch: pytest.MonkeyPatch):
    from api import auth as auth_mod

    auth_mod.reset_caches()
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.delenv("STRICT_JWT", raising=False)
    monkeypatch.delenv("JWT_ALLOWED_APPIDS", raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", _TEST_TENANT_ID)
    monkeypatch.setenv("API_CLIENT_ID", _TEST_API_CLIENT_ID)
    yield
    auth_mod.reset_caches()


def _patch_jwt(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    from api import auth as auth_mod

    class _StubSigningKey:
        def __init__(self, k: str) -> None:
            self.key = k

    class _StubJwks:
        def get_signing_key_from_jwt(self, _token: str) -> _StubSigningKey:
            return _StubSigningKey(key)

    monkeypatch.setattr(auth_mod, "_get_jwks_client", lambda _tid: _StubJwks())
    real_decode = jwt.decode

    def _decode(token: str, signing_key: str, **kwargs: Any) -> Any:
        kwargs["algorithms"] = ["HS256"]
        return real_decode(token, signing_key, **kwargs)

    monkeypatch.setattr(auth_mod.jwt, "decode", _decode)


def _baseline_claims() -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": f"https://login.microsoftonline.com/{_TEST_TENANT_ID}/v2.0",
        "aud": _TEST_API_CLIENT_ID,
        "tid": _TEST_TENANT_ID,
        "oid": "alice-oid",
        "upn": "alice@example.com",
        "iat": now,
        "exp": now + 300,
    }


# ---------------------------------------------------------------------------
# Default-OFF: existing behaviour unchanged.
# ---------------------------------------------------------------------------


def test_strict_jwt_defaults_off(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token without `azp`/`appid` claim is accepted when STRICT_JWT is off."""
    from api.auth import _is_strict_jwt, _validate_token

    assert _is_strict_jwt() is False
    secret = "secret"
    _patch_jwt(monkeypatch, secret)

    token = _make_token(_baseline_claims(), secret)
    identity = _validate_token(token)
    assert identity.object_id == "alice-oid"


def test_strict_jwt_off_does_not_shorten_cache(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache TTL ceiling stays at 300 s when strict is off."""
    from api.auth import _CLAIMS_CACHE_MAX_TTL_SECONDS, _claims_cache_ttl_cap

    assert _claims_cache_ttl_cap() == _CLAIMS_CACHE_MAX_TTL_SECONDS


# ---------------------------------------------------------------------------
# Strict-ON: azp / appid enforcement.
# ---------------------------------------------------------------------------


def test_strict_jwt_accepts_matching_azp(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`azp == API_CLIENT_ID` is accepted under strict mode."""
    from api.auth import _validate_token

    monkeypatch.setenv("STRICT_JWT", "true")
    secret = "secret"
    _patch_jwt(monkeypatch, secret)

    claims = _baseline_claims()
    claims["azp"] = _TEST_API_CLIENT_ID
    token = _make_token(claims, secret)
    identity = _validate_token(token)
    assert identity.object_id == "alice-oid"


def test_strict_jwt_accepts_matching_appid(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`appid` (v1 claim) equal to API_CLIENT_ID is also accepted."""
    from api.auth import _validate_token

    monkeypatch.setenv("STRICT_JWT", "true")
    secret = "secret"
    _patch_jwt(monkeypatch, secret)

    claims = _baseline_claims()
    claims["appid"] = _TEST_API_CLIENT_ID
    token = _make_token(claims, secret)
    identity = _validate_token(token)
    assert identity.object_id == "alice-oid"


def test_strict_jwt_rejects_missing_azp_and_appid(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token without either azp or appid is 401 under strict mode."""
    from api.auth import _validate_token

    monkeypatch.setenv("STRICT_JWT", "true")
    secret = "secret"
    _patch_jwt(monkeypatch, secret)

    token = _make_token(_baseline_claims(), secret)
    with pytest.raises(HTTPException) as excinfo:
        _validate_token(token)
    assert excinfo.value.status_code == 401
    assert "azp" in str(excinfo.value.detail).lower()


def test_strict_jwt_rejects_unauthorized_app(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Foreign appid that is not in the allowlist is rejected with 401."""
    from api.auth import _validate_token

    monkeypatch.setenv("STRICT_JWT", "true")
    secret = "secret"
    _patch_jwt(monkeypatch, secret)

    claims = _baseline_claims()
    claims["azp"] = _OTHER_APP_ID
    token = _make_token(claims, secret)
    with pytest.raises(HTTPException) as excinfo:
        _validate_token(token)
    assert excinfo.value.status_code == 401
    assert "unauthorized" in str(excinfo.value.detail).lower()


def test_strict_jwt_honours_allowed_appids_override(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators can list extra SPAs via JWT_ALLOWED_APPIDS."""
    from api.auth import _validate_token

    monkeypatch.setenv("STRICT_JWT", "true")
    monkeypatch.setenv(
        "JWT_ALLOWED_APPIDS",
        f"{_TEST_API_CLIENT_ID},{_OTHER_APP_ID}",
    )
    secret = "secret"
    _patch_jwt(monkeypatch, secret)

    claims = _baseline_claims()
    claims["azp"] = _OTHER_APP_ID
    token = _make_token(claims, secret)
    identity = _validate_token(token)
    assert identity.object_id == "alice-oid"


def test_strict_jwt_caps_claims_cache_ttl_at_60s(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_claims_cache_ttl_cap()` returns 60 s under strict mode."""
    from api.auth import _CLAIMS_CACHE_STRICT_TTL_SECONDS, _claims_cache_ttl_cap

    monkeypatch.setenv("STRICT_JWT", "true")
    assert _claims_cache_ttl_cap() == _CLAIMS_CACHE_STRICT_TTL_SECONDS
    assert _CLAIMS_CACHE_STRICT_TTL_SECONDS == 60


def test_strict_jwt_cache_uses_strict_ttl_in_put(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_claims_cache_put` honours the strict cap (effective expires_at <= now+60)."""
    from api.auth import (
        _CLAIMS_CACHE,
        CallerIdentity,
        _claims_cache_put,
        _token_cache_key,
    )

    monkeypatch.setenv("STRICT_JWT", "true")
    identity = CallerIdentity(
        object_id="alice-oid",
        tenant_id=_TEST_TENANT_ID,
        upn="alice@example.com",
        raw_token="bearer",
        claims={},
    )
    far_future_exp = time.time() + 86_400  # one day from now
    key = _token_cache_key("bearer")
    _claims_cache_put(key, identity, far_future_exp)
    stored = _CLAIMS_CACHE[key]
    expires_at, _ = stored
    # The strict cap is 60 s; with the 30 s skew the strict-cap branch
    # delivers an effective TTL of 60 s (the min of `exp - now - skew`
    # and 60). Tolerate <=61 s for clock drift on slow runners.
    assert (expires_at - time.time()) <= 61


def test_strict_jwt_helper_reads_env_lazily(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_is_strict_jwt` re-reads the env on every call (no module reload)."""
    from api.auth import _is_strict_jwt

    monkeypatch.delenv("STRICT_JWT", raising=False)
    assert _is_strict_jwt() is False
    monkeypatch.setenv("STRICT_JWT", "true")
    assert _is_strict_jwt() is True
    monkeypatch.setenv("STRICT_JWT", "FALSE")  # case-insensitive
    assert _is_strict_jwt() is False

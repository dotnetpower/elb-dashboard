"""MSAL bearer-token validation for FastAPI routes.

Responsibility: MSAL bearer-token validation for FastAPI routes
Edit boundaries: Keep changes scoped to this module responsibility and update nearby tests.
Key entry points: `CallerIdentity`, `AuthError`, `_discovery_url`, `require_caller`,
`reset_caches`
Risky contracts: Keep imports lightweight and preserve existing public contracts.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient

LOGGER = logging.getLogger(__name__)

_JWKS_CACHE: dict[str, tuple[float, PyJWKClient]] = {}
_JWKS_TTL_SECONDS = 3600

# Validated CallerIdentity cache. Key = sha256(token); value = (expires_at, identity).
# Bounded soft cap to prevent unbounded growth if many distinct tokens hit the
# sidecar (e.g. fuzzers / load tests). Eviction policy: opportunistic on every
# insertion, drop entries whose `expires_at` is in the past.
_CLAIMS_CACHE: dict[str, tuple[float, CallerIdentity]] = {}
_CLAIMS_CACHE_LOCK = threading.Lock()
_CLAIMS_CACHE_MAX_TTL_SECONDS = 300  # never trust the cache longer than 5 min
_CLAIMS_CACHE_SOFT_CAP = 1024
_CLAIMS_SKEW_SECONDS = 30


@dataclass(frozen=True)
class CallerIdentity:
    """Validated caller, derived from a verified bearer token."""

    object_id: str
    tenant_id: str
    upn: str | None
    raw_token: str
    claims: dict[str, Any]


class AuthError(HTTPException):
    """Raised when token validation fails. Subclass of HTTPException so
    FastAPI returns the right status without extra plumbing."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(status_code=status_code, detail=detail)


def _discovery_url(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"


def _get_jwks_client(tenant_id: str) -> PyJWKClient:
    cached = _JWKS_CACHE.get(tenant_id)
    now = time.time()
    if cached and cached[0] > now:
        return cached[1]

    with httpx.Client(timeout=10.0) as client:
        oidc = client.get(_discovery_url(tenant_id)).raise_for_status().json()
    jwks_uri = oidc["jwks_uri"]
    new_client = PyJWKClient(jwks_uri, cache_keys=True, lifespan=_JWKS_TTL_SECONDS)
    _JWKS_CACHE[tenant_id] = (now + _JWKS_TTL_SECONDS, new_client)
    return new_client


def _token_cache_key(token: str) -> str:
    """Hash the bearer token so we never use it directly as a dict key.

    SHA-256 is collision-resistant enough that a hit means *the same token*
    was previously validated. We never persist the raw token to the cache key.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _claims_cache_get(key: str) -> CallerIdentity | None:
    entry = _CLAIMS_CACHE.get(key)
    if entry is None:
        return None
    expires_at, identity = entry
    if expires_at <= time.time():
        # Lazily evict expired entries on read.
        with _CLAIMS_CACHE_LOCK:
            _CLAIMS_CACHE.pop(key, None)
        return None
    return identity


def _claims_cache_put(key: str, identity: CallerIdentity, exp_claim: int | float | None) -> None:
    now = time.time()
    if exp_claim is None:
        # No exp -> token is invalid (jwt.decode would have raised), but be defensive.
        return
    ttl = min(float(exp_claim) - now - _CLAIMS_SKEW_SECONDS, float(_CLAIMS_CACHE_MAX_TTL_SECONDS))
    if ttl <= 0:
        return
    expires_at = now + ttl
    with _CLAIMS_CACHE_LOCK:
        # Opportunistic eviction: if we're at the soft cap, drop the oldest
        # half of expired entries before inserting. Cheap and predictable.
        if len(_CLAIMS_CACHE) >= _CLAIMS_CACHE_SOFT_CAP:
            stale = [k for k, (e, _) in _CLAIMS_CACHE.items() if e <= now]
            for k in stale:
                _CLAIMS_CACHE.pop(k, None)
            # If still over cap (all entries fresh), drop the soonest-to-expire
            # entries to free at least one slot — protects against unbounded
            # growth from fuzzed tokens that all happen to be valid.
            if len(_CLAIMS_CACHE) >= _CLAIMS_CACHE_SOFT_CAP:
                ordered = sorted(_CLAIMS_CACHE.items(), key=lambda item: item[1][0])
                for k, _ in ordered[: max(1, _CLAIMS_CACHE_SOFT_CAP // 4)]:
                    _CLAIMS_CACHE.pop(k, None)
        _CLAIMS_CACHE[key] = (expires_at, identity)


def _validate_token(token: str) -> CallerIdentity:
    cache_key = _token_cache_key(token)
    cached = _claims_cache_get(cache_key)
    if cached is not None:
        return cached

    tenant_id = os.environ.get("AZURE_TENANT_ID")
    api_client_id = os.environ.get("API_CLIENT_ID")
    if not tenant_id or not api_client_id:
        raise AuthError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "AZURE_TENANT_ID / API_CLIENT_ID not configured",
        )

    try:
        jwks_client = _get_jwks_client(tenant_id)
        signing_key = jwks_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=[api_client_id, f"api://{api_client_id}"],
            issuer=[
                f"https://login.microsoftonline.com/{tenant_id}/v2.0",
                f"https://sts.windows.net/{tenant_id}/",
            ],
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        LOGGER.warning("token validation failed: %s", exc)
        raise AuthError(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc

    oid = claims.get("oid")
    if not oid:
        raise AuthError(status.HTTP_401_UNAUTHORIZED, "token missing 'oid' claim")

    identity = CallerIdentity(
        object_id=oid,
        tenant_id=claims.get("tid", tenant_id),
        upn=claims.get("upn") or claims.get("preferred_username"),
        raw_token=token,
        claims=claims,
    )
    _claims_cache_put(cache_key, identity, claims.get("exp"))
    return identity


def _dev_bypass_identity() -> CallerIdentity:
    """Synthetic identity returned when AUTH_DEV_BYPASS=true.

    NEVER set AUTH_DEV_BYPASS in production. The synthetic identity carries
    a clearly-fake ``oid`` and an empty raw token so any code that tries to
    use it for downstream auth will fail loudly rather than silently leak.
    """
    return CallerIdentity(
        object_id="00000000-0000-0000-0000-000000000000",
        tenant_id=os.environ.get("AZURE_TENANT_ID", "dev-bypass"),
        upn="dev-bypass@local",
        raw_token="",
        claims={"dev_bypass": True},
    )


def require_caller(authorization: str | None = Header(default=None)) -> CallerIdentity:
    """FastAPI dependency that returns a validated CallerIdentity or raises 401.

    Usage:
        @router.get("/me")
        def me(caller: CallerIdentity = Depends(require_caller)):
            ...

    With ``AUTH_DEV_BYPASS=true`` (development only) returns a synthetic
    identity without inspecting the Authorization header.
    """
    if os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true":
        return _dev_bypass_identity()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    return _validate_token(token)


def reset_caches() -> None:
    """Clear all auth-layer caches.

    Test-only helper — exercised by ``api/tests`` to keep state isolated
    between test cases.
    """
    _JWKS_CACHE.clear()
    with _CLAIMS_CACHE_LOCK:
        _CLAIMS_CACHE.clear()

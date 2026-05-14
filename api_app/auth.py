"""MSAL bearer-token validation for the FastAPI api sidecar.

Reuses the same OIDC discovery + JWKS caching strategy as the Azure Functions
backend (see `api/auth/token.py`). Kept independent so the `api_app/`
package has no `azure.functions` import dependency.
"""

from __future__ import annotations

import logging
import os
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


def _validate_token(token: str) -> CallerIdentity:
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

    return CallerIdentity(
        object_id=oid,
        tenant_id=claims.get("tid", tenant_id),
        upn=claims.get("upn") or claims.get("preferred_username"),
        raw_token=token,
        claims=claims,
    )


def require_caller(authorization: str | None = Header(default=None)) -> CallerIdentity:
    """FastAPI dependency that returns a validated CallerIdentity or raises 401.

    Usage:
        @router.get("/me")
        def me(caller: CallerIdentity = Depends(require_caller)):
            ...
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    return _validate_token(token)

"""Bearer-token validation for incoming HTTP requests.

Validates a Microsoft Entra access token issued for this Function App's
App Registration. The token is expected to carry the ARM scope so the
backend can call Azure on behalf of the user.

References:
- OIDC discovery: https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration
- JWKS:           the `jwks_uri` returned from the discovery doc
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

LOGGER = logging.getLogger(__name__)

_JWKS_CACHE: dict[str, tuple[float, PyJWKClient]] = {}
_JWKS_TTL_SECONDS = 3600

# Sentinel used in CallerIdentity.raw_token when AUTH_DEV_BYPASS=true.
# `services.azure_clients.credential_for_caller` recognises this and falls
# back to DefaultAzureCredential (i.e. the developer's local az login).
DEV_BYPASS_TOKEN = "__dev_bypass__"  # noqa: S105 — sentinel, not a credential


@dataclass(frozen=True)
class CallerIdentity:
    """Validated caller, derived from a verified bearer token."""

    object_id: str
    tenant_id: str
    upn: str | None
    raw_token: str
    claims: dict[str, Any]


class AuthError(Exception):
    """Raised when token validation fails."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _discovery_url(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"


def _get_jwks_client(tenant_id: str) -> PyJWKClient:
    """Return a cached PyJWKClient for the given tenant."""
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


def validate_bearer_token(authorization_header: str | None) -> CallerIdentity:
    """Validate an `Authorization: Bearer <token>` header.

    Raises AuthError on any failure. Returns a CallerIdentity on success.
    """
    if os.environ.get("AUTH_DEV_BYPASS", "false").lower() == "true":
        LOGGER.warning("AUTH_DEV_BYPASS=true — skipping token validation. DO NOT USE IN PROD.")
        return CallerIdentity(
            object_id="dev-bypass-oid",
            tenant_id=os.environ.get("AZURE_TENANT_ID", "common"),
            upn="dev@local",
            raw_token=DEV_BYPASS_TOKEN,
            claims={"oid": "dev-bypass-oid"},
        )

    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        raise AuthError(401, "missing bearer token")

    token = authorization_header.split(" ", 1)[1].strip()

    tenant_id = os.environ.get("AZURE_TENANT_ID")
    api_client_id = os.environ.get("API_CLIENT_ID")
    if not tenant_id or not api_client_id:
        raise AuthError(500, "AZURE_TENANT_ID / API_CLIENT_ID not configured")

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
        raise AuthError(401, f"invalid token: {exc}") from exc

    oid = claims.get("oid")
    if not oid:
        raise AuthError(401, "token missing 'oid' claim")

    return CallerIdentity(
        object_id=oid,
        tenant_id=claims.get("tid", tenant_id),
        upn=claims.get("upn") or claims.get("preferred_username"),
        raw_token=token,
        claims=claims,
    )

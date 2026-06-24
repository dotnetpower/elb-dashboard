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
import hmac
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Header, HTTPException, Request, status
from jwt import PyJWKClient

LOGGER = logging.getLogger(__name__)

_JWKS_CACHE: dict[str, tuple[float, PyJWKClient]] = {}
_JWKS_TTL_SECONDS = int(os.environ.get("AUTH_JWKS_TTL_SECONDS", "43200"))
_JWKS_CACHE_MAX_TENANTS = max(1, int(os.environ.get("AUTH_JWKS_CACHE_MAX_TENANTS", "100")))
# Single-flight coordination for the JWKS fetch — see `_get_jwks_client`.
_JWKS_INFLIGHT: dict[str, threading.Event] = {}
_JWKS_INFLIGHT_LOCK = threading.Lock()
# Guards the eviction+write of ``_JWKS_CACHE``. Single-flight only serialises
# leaders for the *same* tenant; leaders for *different* tenants run
# concurrently and both reach the eviction ``min(...)`` below. Without this
# lock one leader iterating the dict while another inserts raises
# ``RuntimeError: dictionary changed size during iteration`` — a 500 on the
# auth path. The critical section is pure in-memory bookkeeping (the slow
# OIDC/JWKS fetch stays outside it), so the lock is held only briefly.
_JWKS_CACHE_LOCK = threading.Lock()

# Validated CallerIdentity cache. Key = sha256(token); value = (expires_at, identity).
# Bounded soft cap to prevent unbounded growth if many distinct tokens hit the
# sidecar (e.g. fuzzers / load tests). Eviction policy: opportunistic on every
# insertion, drop entries whose `expires_at` is in the past.
_CLAIMS_CACHE: dict[str, tuple[float, CallerIdentity]] = {}
_CLAIMS_CACHE_LOCK = threading.Lock()
_CLAIMS_CACHE_MAX_TTL_SECONDS = 300  # legacy cap (used when STRICT_JWT is off)
_CLAIMS_CACHE_STRICT_TTL_SECONDS = 60  # audit P1 #9: lower cap when STRICT_JWT=true
_CLAIMS_CACHE_SOFT_CAP = 1024
_CLAIMS_SKEW_SECONDS = 30

# Audit P1 #6 + #9: strict JWT enforcement adds two extra checks on every
# validated token and shortens the claims cache TTL so a revoked SPA cannot
# linger for more than ~60 s. Gated behind `STRICT_JWT` per charter §12a
# Rule 4 (default OFF preserves existing behaviour). When operators flip
# it on, the validator also requires the `azp` (v2) or `appid` (v1) claim
# to be in `JWT_ALLOWED_APPIDS` (defaults to the configured
# `API_CLIENT_ID` so single-app deployments need no extra config).
_STRICT_JWT_ENV = "STRICT_JWT"
_JWT_ALLOWED_APPIDS_ENV = "JWT_ALLOWED_APPIDS"


def _is_strict_jwt() -> bool:
    """Return True when `STRICT_JWT=true` is set in the environment.

    Read at call time so tests can flip the env via `monkeypatch.setenv`
    without re-importing the module.
    """
    return os.environ.get(_STRICT_JWT_ENV, "").lower() == "true"


def _claims_cache_ttl_cap() -> int:
    """Effective claims-cache TTL ceiling for the current request.

    Strict mode caps the cache at 60 s so a SPA whose admin consent has
    been revoked stops accepting cached tokens within a minute. Legacy
    mode keeps the 5-minute cap so the existing low-churn polling
    profile (status / sidecars cards) is unaffected.
    """
    if _is_strict_jwt():
        return _CLAIMS_CACHE_STRICT_TTL_SECONDS
    return _CLAIMS_CACHE_MAX_TTL_SECONDS


def _jwt_allowed_appids(api_client_id: str) -> frozenset[str]:
    """Return the set of `azp` / `appid` values accepted in strict mode.

    Defaults to the configured `API_CLIENT_ID` so a single-app deployment
    needs no extra configuration. Operators with separate SPA + API app
    registrations override via `JWT_ALLOWED_APPIDS=<spa-id>,<other-id>`.
    """
    raw = os.environ.get(_JWT_ALLOWED_APPIDS_ENV, "").strip()
    if raw:
        return frozenset(x.strip() for x in raw.split(",") if x.strip())
    return frozenset({api_client_id})


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

    # Single-flight election: while one thread builds the PyJWKClient
    # (synchronous OIDC discovery + JWKS fetch via ``httpx.Client``),
    # other threads asking for the same tenant wait on a per-tenant
    # ``threading.Event`` instead of all paying the full discovery
    # round-trip. Avoids the thundering-herd thrash that used to fire
    # on a cold start when the dashboard's first authenticated polls
    # arrived concurrently.
    with _JWKS_INFLIGHT_LOCK:
        cached = _JWKS_CACHE.get(tenant_id)
        if cached and cached[0] > now:
            return cached[1]
        event = _JWKS_INFLIGHT.get(tenant_id)
        if event is None:
            event = threading.Event()
            _JWKS_INFLIGHT[tenant_id] = event
            leader = True
        else:
            leader = False
    if not leader:
        # Wait briefly for the leader to populate the cache, then retry.
        # On timeout we fall through to elect ourselves instead of
        # blocking forever — covers the case where the leader crashed.
        if not event.wait(timeout=15.0):
            LOGGER.info("jwks single-flight wait timed out for tenant=%s", tenant_id)
        cached = _JWKS_CACHE.get(tenant_id)
        if cached and cached[0] > time.time():
            return cached[1]
        # Leader's cache write failed; fall through and try ourselves.
    try:
        # Pooled client so the OIDC discovery fetch reuses one TLS handshake
        # across cold tenants. This path runs at most once per tenant per
        # `_JWKS_TTL_SECONDS`, but a process churning through tenants on cold
        # startup still benefited from skipping the per-call connect cost.
        from api.services.httpx_pool import get_pooled_client

        client = get_pooled_client("auth-oidc-discovery", timeout=10.0)
        oidc = client.get(_discovery_url(tenant_id)).raise_for_status().json()
        jwks_uri = oidc["jwks_uri"]
        new_client = PyJWKClient(jwks_uri, cache_keys=True, lifespan=_JWKS_TTL_SECONDS)
        with _JWKS_CACHE_LOCK:
            if tenant_id not in _JWKS_CACHE and len(_JWKS_CACHE) >= _JWKS_CACHE_MAX_TENANTS:
                # Evict the entry closest to expiry rather than FIFO, so a still-valid
                # tenant is not dropped while a soon-to-expire one survives.
                soonest = min(_JWKS_CACHE, key=lambda t: _JWKS_CACHE[t][0])
                _JWKS_CACHE.pop(soonest, None)
            _JWKS_CACHE[tenant_id] = (now + _JWKS_TTL_SECONDS, new_client)
        return new_client
    finally:
        with _JWKS_INFLIGHT_LOCK:
            _JWKS_INFLIGHT.pop(tenant_id, None)
            event.set()


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
    ttl = min(float(exp_claim) - now - _CLAIMS_SKEW_SECONDS, float(_claims_cache_ttl_cap()))
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
        # Surface a generic message to the client. The PyJWT exception text
        # (e.g. "Not enough segments", "Signature verification failed")
        # describes *why* validation failed and is useful recon for an
        # attacker probing token shapes — keep it server-side in the log
        # above only.
        raise AuthError(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc

    # Defence-in-depth: explicitly verify the ``tid`` (tenant id) claim
    # in addition to the issuer URL check above. The issuer list already
    # constrains tokens to ``login.microsoftonline.com/<tenant>/v2.0``
    # and ``sts.windows.net/<tenant>/``, but a future regression that
    # broadens the issuer list (e.g. accepts ``common``) would otherwise
    # silently let cross-tenant tokens through. ``tid`` is a mandatory
    # AAD claim and equals the issuer's tenant for a correctly-issued
    # token, so a mismatch indicates either tampering, a misconfigured
    # multi-tenant App Registration, or the regression we want to catch.
    token_tid = claims.get("tid")
    if not token_tid or str(token_tid).lower() != tenant_id.lower():
        LOGGER.warning(
            "token tenant mismatch: claim tid=%s expected=%s",
            token_tid,
            tenant_id,
        )
        raise AuthError(
            status.HTTP_401_UNAUTHORIZED,
            "token tenant_id does not match configured AZURE_TENANT_ID",
        )

    oid = claims.get("oid")
    if not oid:
        raise AuthError(status.HTTP_401_UNAUTHORIZED, "token missing 'oid' claim")

    # Audit P1 #6: when `STRICT_JWT=true` is set, additionally pin the
    # token to a known SPA / app-registration via the `azp` (v2) or
    # `appid` (v1) claim. Without this, any other app that has minted a
    # token for our API audience inside the same tenant would be
    # accepted. Default OFF preserves existing single-app behaviour per
    # charter §12a Rule 4.
    if _is_strict_jwt():
        appid_claim = claims.get("azp") or claims.get("appid")
        if not appid_claim:
            raise AuthError(
                status.HTTP_401_UNAUTHORIZED,
                "token missing 'azp'/'appid' claim",
            )
        if appid_claim not in _jwt_allowed_appids(api_client_id):
            LOGGER.warning(
                "token issued by unauthorized app: appid=%s",
                appid_claim,
            )
            raise AuthError(
                status.HTTP_401_UNAUTHORIZED,
                "token issued by unauthorized app",
            )

    identity = CallerIdentity(
        object_id=oid,
        tenant_id=claims.get("tid", tenant_id),
        upn=claims.get("upn") or claims.get("preferred_username"),
        raw_token=token,
        claims=claims,
    )
    _claims_cache_put(cache_key, identity, claims.get("exp"))
    return identity


# Public sentinels reused by ownership / authorization gates. Keep these
# in lockstep with `_dev_bypass_identity` — callers compare against the
# constant rather than hardcoding the literal so a future bypass-oid
# change does not silently break authorization helpers.
DEV_BYPASS_OID: str = "00000000-0000-0000-0000-000000000000"


def is_dev_bypass_caller(caller: CallerIdentity) -> bool:
    """Return True when ``caller`` was produced by the AUTH_DEV_BYPASS path.

    Use this from authorization helpers that need to short-circuit owner
    checks during local development. Do NOT hardcode the sentinel oid
    elsewhere — that's how the autostop ownership guard was silently
    broken when the bypass oid changed shape.

    SECURITY: deployed Container Apps always refuse to honour the dev
    bypass even if ``AUTH_DEV_BYPASS=true`` slipped through to a cloud
    revision (e.g. a stale ``.env`` import). In a deployed environment
    ``CONTAINER_APP_NAME`` is set by the platform; when present we
    refuse to recognise the bypass identity so the dev-bypass GUID
    cannot turn into a privilege-escalation primitive on top of a real
    operator action.
    """
    if not caller:
        return False
    if os.environ.get("CONTAINER_APP_NAME"):
        return False
    return caller.object_id == DEV_BYPASS_OID


def _dev_bypass_identity() -> CallerIdentity:
    """Synthetic identity returned when AUTH_DEV_BYPASS=true.

    NEVER set AUTH_DEV_BYPASS in production. The synthetic identity carries
    a clearly-fake ``oid`` and an empty raw token so any code that tries to
    use it for downstream auth will fail loudly rather than silently leak.
    """
    return CallerIdentity(
        object_id=DEV_BYPASS_OID,
        tenant_id=os.environ.get("AZURE_TENANT_ID", "dev-bypass"),
        upn="dev-bypass@local",
        raw_token="",
        claims={"dev_bypass": True},
    )


async def require_caller(
    authorization: str | None = Header(default=None),
) -> CallerIdentity:
    """FastAPI dependency that returns a validated CallerIdentity or raises 401.

    Usage:
        @router.get("/me")
        def me(caller: CallerIdentity = Depends(require_caller)):
            ...

    With ``AUTH_DEV_BYPASS=true`` (development only) returns a synthetic
    identity without inspecting the Authorization header.

    Async so the JWT validation (cache miss → synchronous OIDC discovery
    + JWKS fetch via ``httpx.Client``) runs on ``asyncio.to_thread``
    instead of blocking one of FastAPI's threadpool slots for every
    bearer-authed request. Cache hit short-circuits and pays no
    threadpool round-trip — that's the common case under dashboard
    polling.
    """
    if os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true":
        return _dev_bypass_identity()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    # Cache hit: no IO, return directly without burning a threadpool slot.
    cached = _claims_cache_get(_token_cache_key(token))
    if cached is not None:
        return cached
    # Cache miss: validation involves a synchronous JWKS fetch on first
    # tenant access. Run it in a worker thread so the event loop stays
    # responsive for SSE / WebSocket / streaming responses.
    import asyncio

    return await asyncio.to_thread(_validate_token, token)


# --------------------------------------------------------------------------- #
# Opt-in shared-token auth for READ-ONLY OpenAPI control-plane routes.
#
# The cluster-independent database catalogue routes
# (``GET /api/aks/openapi/databases[/{db_name}]``) mirror the in-cluster
# ``elb-openapi`` ``/v1/databases*`` reads. A caller that already holds the
# ``elb-openapi`` admin token (``X-ELB-API-Token``) can, when this opt-in gate
# is ON, use the SAME token to authenticate the dashboard's read-only mirror —
# one credential instead of two. This is DELIBERATELY limited to read-only
# routes; cost-bearing / mutating actions (e.g. ensure-running) stay MSAL-only
# because the shared token has no Azure RBAC gate.
#
# Charter §12a Rule 4: ships default-OFF behind ``ALLOW_OPENAPI_TOKEN_AUTH``.
# Unset / falsey => existing behaviour (MSAL bearer only) preserved exactly.
# --------------------------------------------------------------------------- #
_ALLOW_OPENAPI_TOKEN_AUTH_ENV = "ALLOW_OPENAPI_TOKEN_AUTH"  # noqa: S105 - env var name, not a secret.
# Clearly non-UUID sentinel so any code that mistakes a token caller for an
# Azure AD principal (and tries to reuse it for a downstream Azure call) fails
# loudly rather than leaking. Kept distinct from ``DEV_BYPASS_OID``.
OPENAPI_TOKEN_OID: str = "openapi-token-caller"  # noqa: S105 - sentinel oid, not a secret.


def _openapi_token_auth_enabled() -> bool:
    """Return True only when the opt-in shared-token gate is explicitly on.

    Read at call time so tests / operators can flip ``ALLOW_OPENAPI_TOKEN_AUTH``
    without re-importing. Default (unset) is OFF — MSAL bearer only.
    """
    return os.environ.get(_ALLOW_OPENAPI_TOKEN_AUTH_ENV, "").lower() == "true"


def _resolve_expected_openapi_token() -> str:
    """Return the authoritative ``elb-openapi`` API token, or ``""`` if unknown.

    Reads the deploy-time env first (``ELB_OPENAPI_API_TOKEN`` is set on the api
    sidecar and survives a cluster stop), then the Redis runtime cache (the beat
    reconciler re-syncs it from the AKS deployment). It deliberately never reads
    the cluster directly — that would couple a cheap per-request auth check to a
    slow K8s round trip and defeat the stopped-cluster use case. An empty return
    means "token auth unavailable", which the caller treats as REJECT (never
    bypass).
    """
    token = os.environ.get("ELB_OPENAPI_API_TOKEN", "").strip()
    if token:
        return token
    try:
        from api.services.openapi.runtime import get_openapi_api_token

        return (get_openapi_api_token() or "").strip()
    except Exception as exc:  # pragma: no cover - defensive, cache optional
        LOGGER.debug("openapi token resolve from cache failed: %s", type(exc).__name__)
        return ""


def _openapi_token_identity() -> CallerIdentity:
    """Synthetic identity for a request authenticated by the shared token.

    Carries a clearly non-UUID object id and an empty raw token so any code that
    mistakes it for an Azure AD caller fails loudly instead of leaking a token.
    Only the read-only OpenAPI database routes accept this identity.
    """
    return CallerIdentity(
        object_id=OPENAPI_TOKEN_OID,
        tenant_id=os.environ.get("AZURE_TENANT_ID", ""),
        upn="openapi-token@local",
        raw_token="",
        claims={"openapi_token_auth": True},
    )


def is_openapi_token_caller(caller: CallerIdentity) -> bool:
    """True when the caller authenticated via the shared ``X-ELB-API-Token``."""
    return bool(caller) and caller.claims.get("openapi_token_auth") is True


async def require_caller_or_openapi_token(
    authorization: str | None = Header(default=None),
    x_elb_api_token: str | None = Header(default=None, alias="X-ELB-API-Token"),
) -> CallerIdentity:
    """Like :func:`require_caller` but ALSO accepts the shared ``X-ELB-API-Token``.

    Auth precedence:

    1. With the opt-in gate ON (``ALLOW_OPENAPI_TOKEN_AUTH=true``) AND a
       non-empty ``X-ELB-API-Token`` header: validate it (constant-time) against
       the authoritative token. Match => synthetic token identity;
       present-but-wrong (or token unknown server-side) => 401. We do NOT
       silently fall back to MSAL on a present-but-wrong token — that would mask
       a bad token as a confusing "missing bearer" error.
    2. Otherwise: the standard MSAL bearer path (:func:`require_caller`). With
       the gate OFF the ``X-ELB-API-Token`` header is ignored entirely, so the
       existing MSAL behaviour is preserved exactly.

    SECURITY: only mount this on READ-ONLY routes. The shared token has no Azure
    RBAC gate, so it must never reach a cost-bearing or mutating action.
    """
    provided = (x_elb_api_token or "").strip()
    if _openapi_token_auth_enabled() and provided:
        expected = _resolve_expected_openapi_token()
        # ``expected`` empty => token auth unavailable => reject (never bypass).
        if expected and hmac.compare_digest(provided, expected):
            return _openapi_token_identity()
        LOGGER.warning(
            "X-ELB-API-Token auth rejected (gate on; token mismatch or unavailable)"
        )
        raise AuthError(status.HTTP_401_UNAUTHORIZED, "invalid X-ELB-API-Token")
    return await require_caller(authorization=authorization)


# Clearly non-UUID sentinel for a request authenticated by a signed download
# token (see ``api/services/download_token.py``). Distinct from the bearer and
# openapi-token sentinels so a token caller can never be mistaken for an Azure
# AD principal in a downstream call.
DOWNLOAD_TOKEN_OID: str = "download-token-caller"  # noqa: S105 - sentinel oid, not a secret.


def _download_token_identity() -> CallerIdentity:
    """Synthetic identity for a request authorised by a signed download token.

    The token is already scoped to one ``(job_id, file_id)`` pair, so the gate
    grants exactly the read this URL was minted for. Carries a non-UUID object
    id and an empty raw token so any code that mistakes it for an Azure AD caller
    fails loudly instead of leaking a token.
    """
    return CallerIdentity(
        object_id=DOWNLOAD_TOKEN_OID,
        tenant_id=os.environ.get("AZURE_TENANT_ID", ""),
        upn="download-token@local",
        raw_token="",
        claims={"download_token_auth": True},
    )


def is_download_token_caller(caller: CallerIdentity) -> bool:
    """True when the caller authorised via a signed download-URL token."""
    return bool(caller) and caller.claims.get("download_token_auth") is True


async def require_caller_or_download_token(request: Request) -> CallerIdentity:
    """Like :func:`require_caller` but ALSO accepts a signed ``?token=`` query.

    Auth precedence:

    1. A non-empty ``token`` query parameter that verifies (constant-time, HMAC,
       unexpired) against this route's ``job_id`` + ``file_id`` path params =>
       synthetic download-token identity. This is how a Service Bus completion
       consumer downloads a result file by URL alone, with no bearer token.
    2. Otherwise: the standard MSAL bearer path (:func:`require_caller`). A
       present-but-invalid token does NOT short-circuit to 401 — we fall through
       to the bearer path so an interactive browser request (no token, valid
       bearer) still works on the same route.

    SECURITY: only mount this on the READ-ONLY result-file download route. The
    token has no Azure RBAC gate, so it must never reach a mutating action; its
    scope is enforced entirely by the ``(job_id, file_id)`` signature.
    """
    from api.services.download_token import verify_download_token

    token = (request.query_params.get("token") or "").strip()
    if token:
        job_id = str(request.path_params.get("job_id") or "")
        file_id = str(request.path_params.get("file_id") or "")
        if job_id and file_id and verify_download_token(token, job_id, file_id):
            return _download_token_identity()
    authorization = request.headers.get("authorization")
    return await require_caller(authorization=authorization)


def reset_caches() -> None:
    """Clear all auth-layer caches.

    Test-only helper — exercised by ``api/tests`` to keep state isolated
    between test cases.
    """
    _JWKS_CACHE.clear()
    with _CLAIMS_CACHE_LOCK:
        _CLAIMS_CACHE.clear()

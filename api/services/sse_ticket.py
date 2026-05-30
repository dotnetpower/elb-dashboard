"""Single-source helper for SSE-ticket binding and origin enforcement.

Module summary: EventSource cannot send `Authorization` headers, so the
SSE routes in `api/routes/monitor/sidecars.py` and
`api/routes/monitor/logs.py` must stay ticket-gated (per charter §12a
Rule 5). This module provides the additional binding logic — Origin
allowlist on the *issue* endpoint, plus client-IP + User-Agent binding
on the *consume* path — so a leaked ticket cannot be redeemed from a
different browser, network, or origin.

Responsibility: Compute stable short hashes for client IP and User-Agent,
  evaluate the Origin allowlist, and expose a single env-driven flag
  (`STRICT_SSE_TICKET_BINDING`, default OFF) that gates the new
  enforcement per charter §12a Rule 4.
Edit boundaries: New helpers go here; route modules import the public
  surface and never recompute hashes inline. Do NOT add `Depends(require_caller)`
  to the SSE consume endpoints — that breaks EventSource per §12a Rule 5.
Key entry points: `is_strict`, `client_ip_hash`, `user_agent_hash`,
  `origin_allowed`, `enforce_issue_origin`, `binding_matches`.
Risky contracts: When `STRICT_SSE_TICKET_BINDING=true` the issue endpoint
  rejects foreign origins with 403 and the consume endpoint treats a
  binding mismatch the same as an expired ticket (returns 204 from the
  route so the browser stops auto-reconnecting). Tests in
  `api/tests/test_sse_ticket_binding.py` cover both the ON and OFF paths.
Validation: `uv run pytest -q api/tests/test_sse_ticket_binding.py`.
"""

from __future__ import annotations

import hashlib
import os

from fastapi import HTTPException, Request, status

# Audit P0 #2 #3: gate behind a `STRICT_*` env var per charter §12a Rule 4.
# Default OFF preserves the existing behaviour. Operators flip this on once
# the soak window confirms no legitimate browser is bouncing IP/origin.
_STRICT_ENV = "STRICT_SSE_TICKET_BINDING"

# Mirror the WebSocket allowlist contract: the same env var that controls
# `/api/terminal/ws` controls SSE origin enforcement so operators only have
# one knob to tune. Empty list = same-origin only.
_ALLOWED_ORIGINS_RAW = os.environ.get("TERMINAL_WS_ALLOWED_ORIGINS", "").strip()
_ALLOWED_ORIGINS: frozenset[str] = frozenset(
    o.strip().rstrip("/") for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()
)


def is_strict() -> bool:
    """Return True when the strict-binding feature flag is enabled.

    Read at call time (not at import) so tests can flip the env var with
    `monkeypatch.setenv` without reloading the module.
    """
    return os.environ.get(_STRICT_ENV, "").lower() == "true"


def _short_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def client_ip_hash(request: Request) -> str:
    """Stable short hash of the caller's IP, honouring the trusted XFF first hop.

    `Request.client.host` reflects the immediate TCP peer, which inside a
    Container App is the ingress proxy — every caller would collapse to one
    hash. Prefer the *first* hop of `X-Forwarded-For` (the real client) when
    present; fall back to `request.client.host` otherwise. We never trust
    any hop beyond the first because the proxy chain is the only thing
    the platform vouches for.
    """
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return _short_sha256(first)
    if request.client is not None and request.client.host:
        return _short_sha256(request.client.host)
    return _short_sha256("unknown")


def user_agent_hash(request: Request) -> str:
    """Stable short hash of the caller's User-Agent header (or 'unknown')."""
    ua = (request.headers.get("user-agent") or "").strip() or "unknown"
    return _short_sha256(ua)


def origin_allowed(request: Request) -> bool:
    """Return True if the request's Origin header passes the allowlist check.

    Mirrors the CSWSH defence in `api/routes/terminal/ws.py::_origin_allowed`:
    same-origin is always allowed, additional origins come from
    `TERMINAL_WS_ALLOWED_ORIGINS`, and missing Origin (native clients) is
    allowed because the bearer token already authenticated the caller.
    """
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        return True
    if origin in _ALLOWED_ORIGINS:
        return True
    host = (request.headers.get("host") or "").strip().lower()
    if host:
        for scheme in ("https", "http"):
            if origin == f"{scheme}://{host}".rstrip("/"):
                return True
    return False


def enforce_issue_origin(request: Request) -> None:
    """Raise 403 if strict mode is on AND the Origin header is foreign.

    Called from `/sidecars/ticket` and `/logs/ticket`. Issue endpoints stay
    `require_caller`-gated, so this is an extra defence-in-depth layer for
    the case where a valid bearer token is replayed from a hostile page.
    """
    if not is_strict():
        return
    if origin_allowed(request):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="origin not allowed",
    )


def binding_matches(
    *,
    request: Request,
    ticket_ip_hash: str | None,
    ticket_ua_hash: str | None,
) -> bool:
    """Return True when the consume request matches the issued ticket's binding.

    When strict mode is off (the default), return True unconditionally so
    legacy callers keep working. When on, both `ip_hash` and `ua_hash`
    must equal the values captured at issue time; either mismatch makes
    the ticket invalid and the SSE route maps that to 204 (same as
    expired / consumed) so the browser stops auto-reconnecting.
    """
    if not is_strict():
        return True
    if ticket_ip_hash is None or ticket_ua_hash is None:
        # Strict mode is on but the ticket pre-dates the binding rollout —
        # treat as a failure so an attacker cannot strip the fields off a
        # forged ticket.
        return False
    return (
        client_ip_hash(request) == ticket_ip_hash
        and user_agent_hash(request) == ticket_ua_hash
    )

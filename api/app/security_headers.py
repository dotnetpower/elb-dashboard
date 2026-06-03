"""Security response-header middleware for the api sidecar.

Responsibility: Stamp every HTTP response (API JSON *and* proxied SPA assets)
with a conservative set of security headers and strip the upstream `Server`
version banner, so the public Container Apps ingress never leaks framework
versions or omits baseline browser protections.
Edit boundaries: Response-header shaping only. No auth, no routing, no body
rewriting. Content-Security-Policy is the only header that can break the SPA,
so it stays behind the default-OFF `STRICT_CSP` gate (charter §12a Rule 4).
Key entry points: `SecurityHeadersMiddleware`.
Risky contracts: The always-on headers (HSTS, nosniff, frame-deny, referrer,
permissions) must never strip a caller's access — they are additive and
persona-neutral. `Server` is overwritten (not deleted) so well-behaved clients
still receive a value; uvicorn's own `server` banner is suppressed separately
via the `--no-server-header` flag in `api/Dockerfile`.
Validation: `uv run pytest -q api/tests/test_security_headers.py`.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

# Generic banner that replaces the upstream `uvicorn` / `nginx/<version>`
# value. Carrying a constant string keeps the header well-formed for clients
# that parse it while leaking neither the framework nor its version.
_SERVER_BANNER = "ElasticBLAST"

# Always-on, persona-neutral headers. These are additive browser protections
# that cannot strip a caller's API access, so they default ON (the CSP header,
# which *can* break the SPA, is gated separately below).
_STATIC_HEADERS: dict[str, str] = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

# Conservative CSP for a same-origin React SPA. `'unsafe-inline'` is required
# for Vite-injected styles and the MSAL redirect bootstrap; `connect-src`
# allows the Microsoft identity platform endpoints the SPA talks to. The
# `www.ncbi.nlm.nih.gov` origin is allowed in script/style/connect/img/frame so
# the opt-in NCBI Sequence Viewer embed on SequenceDetail can load — kept in
# sync with `web/nginx.conf`. Only applied when `STRICT_CSP=true` so a
# misconfigured policy can never silently break the dashboard in production
# (charter §12a Rule 4 — default OFF).
_DEFAULT_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https://www.ncbi.nlm.nih.gov; "
    "style-src 'self' 'unsafe-inline' https://www.ncbi.nlm.nih.gov; "
    "script-src 'self' https://www.ncbi.nlm.nih.gov; "
    "connect-src 'self' https://login.microsoftonline.com "
    "https://graph.microsoft.com https://www.ncbi.nlm.nih.gov; "
    "frame-src https://www.ncbi.nlm.nih.gov; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject baseline security headers and mask the `Server` version banner.

    Runs on every response, including the catch-all reverse-proxied SPA
    responses, so static assets and API JSON share the same posture.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        # `setdefault` so we fill gaps on api-sidecar JSON responses without
        # clobbering the SPA-tuned values the frontend nginx sidecar already
        # sets on proxied static assets (e.g. its own Referrer-Policy / CSP).
        for name, value in _STATIC_HEADERS.items():
            response.headers.setdefault(name, value)
        # Overwrite (not setdefault) so the upstream version banner is masked
        # even when nginx forwarded a `Server: nginx/<version>` value. Uvicorn's
        # own banner is suppressed at the server level via --no-server-header.
        response.headers["Server"] = _SERVER_BANNER
        if os.environ.get("STRICT_CSP", "").lower() == "true":
            csp = os.environ.get("STRICT_CSP_POLICY", _DEFAULT_CSP)
            response.headers["Content-Security-Policy"] = csp
        return response

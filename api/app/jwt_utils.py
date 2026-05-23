"""Best-effort JWT / client-IP extraction for the request inspector.

Responsibility: Pull the `upn` claim out of an Authorization header (no signature
verification) and the originating client IP from headers. Display-only — the
route's own `require_caller` dependency remains the real auth gate.
Edit boundaries: Keep this module pure. No I/O, no Azure SDK, no logging.
Key entry points: `_decode_jwt_upn`, `_extract_client_ip`.
Risky contracts: Never trust the returned values for authorisation decisions.
Truncate display output to bounded sizes (UPN 128, IP 64) so logs stay readable.
Validation: `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

import base64
import json

from fastapi import Request


def _decode_jwt_upn(authz: str | None) -> str | None:
    """Best-effort caller extraction for the inspector — NOT auth.

    Just base64-decodes the JWT payload (no signature verify) and pulls
    `upn` / `preferred_username`. The route's own `require_caller`
    dependency is the real auth gate; this is for display only.
    """
    if not authz or not authz.lower().startswith("bearer "):
        return None
    parts = authz.split(" ", 1)[1].strip().split(".")
    if len(parts) < 2:
        return None
    try:
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return None
    upn = payload.get("upn") or payload.get("preferred_username")
    return str(upn)[:128] if upn else None


def _extract_client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return None

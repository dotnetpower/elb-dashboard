"""Shared helpers for /api/monitor route modules.

Responsibility: Shared helpers for /api/monitor route modules
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_sub_default`, `_cache_key`, `_graceful`, `_classify_exception`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate. `degraded_reason` codes returned by `_graceful` are part of the SPA contract — the
diagnostics banner branches on `auth_wrong_tenant` / `unauthorized` / `forbidden` / `not_found`
to render actionable guidance, so renaming codes requires a coordinated SPA change.
Validation: `uv run pytest -q api/tests/test_route_contracts.py
api/tests/test_monitor_cache.py api/tests/test_monitor_graceful.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError

from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)


def _sub_default() -> str:
    return os.environ.get("AZURE_SUBSCRIPTION_ID", "")


def _cache_key(*parts: object) -> str:
    return ":".join(str(part) for part in parts)


# Azure ARM error codes that unambiguously signal a tenant/issuer mismatch.
# Prefer matching these structured codes (Azure guarantees them in
# ``HttpResponseError.error.code``) over substring matching the message,
# which would silently break the moment Azure rewords the error text.
_WRONG_TENANT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "InvalidAuthenticationTokenTenant",
        "InvalidAuthenticationToken",
        "AuthorizationFailed",
    }
)

# Substrings still used as a fallback when ``error.code`` is not populated
# (some older SDK paths only expose the raw message). Keep the list tiny
# so we do not accidentally match unrelated 401 responses.
_WRONG_TENANT_MESSAGE_MARKERS: tuple[str, ...] = (
    "InvalidAuthenticationTokenTenant",
    "wrong issuer",
    "AADSTS50020",
)


def _looks_like_wrong_tenant(exc: HttpResponseError) -> bool:
    """Return True if ``exc`` matches a wrong-tenant/wrong-issuer 401.

    Inspect the structured error code first (stable across SDK locales) and
    fall back to substring matching on the message body only when ``error``
    is missing — this prevents the classification from breaking the moment
    Azure rewrites an error string.
    """
    err = getattr(exc, "error", None)
    code = getattr(err, "code", None) if err is not None else None
    if isinstance(code, str) and code in _WRONG_TENANT_ERROR_CODES:
        # AuthorizationFailed is broader than wrong-tenant; only treat it as
        # such when the message also carries the issuer marker so a plain
        # missing-role 403 (also "AuthorizationFailed") does not collide.
        if code == "AuthorizationFailed":
            message = str(exc)
            return any(marker in message for marker in _WRONG_TENANT_MESSAGE_MARKERS)
        return True
    message = str(exc)
    return any(marker in message for marker in _WRONG_TENANT_MESSAGE_MARKERS)


def _classify_exception(exc: Exception) -> str:
    """Map an SDK exception to a stable degraded-reason code consumed by the SPA.

    The SPA uses these codes to decide between a generic error label and an
    actionable banner (e.g. `auth_wrong_tenant` → "your az login is on a
    different tenant than the selected subscription"). Codes must stay stable.
    """
    if isinstance(exc, ResourceNotFoundError):
        return "not_found"
    if isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        if status == 401:
            return "auth_wrong_tenant" if _looks_like_wrong_tenant(exc) else "unauthorized"
        if status == 403:
            return "forbidden"
        if status == 404:
            return "not_found"
        return f"http_{status or 'error'}"
    if isinstance(exc, AzureError):
        return "azure_error"
    return type(exc).__name__


def _graceful(op: str, exc: Exception, *, empty: Any) -> Any:
    code = _classify_exception(exc)
    LOGGER.warning("%s gracefully degraded: %s (%s)", op, code, sanitise(str(exc))[:200])
    out = dict(empty) if isinstance(empty, dict) else {"items": empty}
    out["degraded"] = True
    out["degraded_reason"] = code
    return out

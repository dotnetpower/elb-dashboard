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
import threading
from typing import Any

from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError

from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)


# OpenTelemetry counter for "a monitor route returned a degraded payload to the
# browser". This is the user-visible SLO signal: it fires only when `_graceful`
# actually serves an empty/degraded body, NOT on every loader hiccup. The
# sibling `elb_monitor_snapshot_refresh_failed` counter (in
# `api.services.monitor_cache`) fires on every loader failure including the ones
# masked by a stale-cache fallback, so cache-counter ≥ route-counter and the gap
# is exactly the degradation the stale cache absorbed. Labelled by `op` (the
# route operation) and `reason` (the classified degraded code) so operators can
# see WHICH card is broken and WHY. Lazily created so a process without OTel
# initialised still imports cleanly.
_DEGRADED_COUNTER: Any = None
_DEGRADED_COUNTER_LOCK = threading.Lock()


class _NullCounter:
    def add(self, value: float, attributes: dict[str, Any] | None = None) -> None:
        return None


def _get_degraded_counter() -> Any:
    global _DEGRADED_COUNTER
    if _DEGRADED_COUNTER is not None:
        return _DEGRADED_COUNTER
    with _DEGRADED_COUNTER_LOCK:
        if _DEGRADED_COUNTER is not None:
            return _DEGRADED_COUNTER
        try:
            from opentelemetry import metrics

            meter = metrics.get_meter("api.routes.monitor.common")
            _DEGRADED_COUNTER = meter.create_counter(
                "elb_monitor_route_degraded",
                unit="1",
                description=(
                    "Count of monitor routes that served a degraded/empty "
                    "payload to the browser, labelled by route op and "
                    "degraded reason."
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.debug("OTel meter unavailable: %s", type(exc).__name__)
            _DEGRADED_COUNTER = _NullCounter()
    return _DEGRADED_COUNTER


def _reset_degraded_counter() -> None:
    """Test-only: drop the cached counter so the meter is re-resolved on next
    use (e.g. after monkeypatching OTel)."""
    global _DEGRADED_COUNTER
    with _DEGRADED_COUNTER_LOCK:
        _DEGRADED_COUNTER = None


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
    # Dashboard polls every 5-30 s, so a sustained AKS / Storage degrade
    # would emit a fresh WARNING per route per tick without dedup. Key by
    # (op, classification) so a NEW failure class still surfaces inside
    # the dedup window; repeats drop to DEBUG.
    from api.services.log_dedup import dedup_log_warning

    dedup_log_warning(
        LOGGER,
        ("monitor_graceful", op, code),
        "%s gracefully degraded: %s (%s)",
        op,
        code,
        sanitise(str(exc))[:200],
    )
    try:
        _get_degraded_counter().add(1, {"op": op, "reason": code})
    except Exception as counter_exc:  # pragma: no cover - never fail a degrade path
        LOGGER.debug("degraded counter add skipped: %s", type(counter_exc).__name__)
    out = dict(empty) if isinstance(empty, dict) else {"items": empty}
    out["degraded"] = True
    out["degraded_reason"] = code
    return out

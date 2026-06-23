"""Message-flow monitor route.

Responsibility: HTTP shaping for the dashboard "Message Flow" card. Exposes a
    single read-only ``GET /message-flow`` that returns the Producers/Broker/
    Consumers snapshot built by ``api.services.message_flow``.
Edit boundaries: Keep HTTP validation and response shaping here; all
    aggregation/Service-Bus/jobstate work lives in the service layer.
Key entry points: ``message_flow``.
Risky contracts: Every non-health `/api/*` route enforces ``require_caller``.
    The route never 500s — it degrades to an empty snapshot via ``_graceful`` so
    the card can hide itself instead of breaking the dashboard.
Validation: ``uv run pytest -q api/tests/test_message_flow.py
    api/tests/test_route_contracts.py``.
"""

from __future__ import annotations

import os
from typing import Any, cast

from fastapi import APIRouter, Depends, Query

from api.auth import CallerIdentity, require_caller
from api.routes.monitor.common import _cache_key, _graceful
from api.services.message_flow import build_message_flow

router = APIRouter()

_DISABLED: dict[str, Any] = {"enabled": False}

# The message-flow card carries a LIVE request-queue preview, so it runs a
# fresher snapshot TTL than the shared 30s monitor default: a queued message
# that is not being drained yet (cluster warming up under queue-arrival
# auto-start, no consumer running, or one injected via the Azure portal) would
# otherwise linger up to the full 30s cache window before it surfaces. This TTL
# is applied PER-CALL (``ttl_seconds`` below), so only this card refreshes
# faster — every other monitor card keeps ``MONITOR_SNAPSHOT_TTL_SECONDS``. The
# snapshot cache is stale-while-revalidate, so the lower TTL refreshes in the
# background and never blocks the poll. Tunable via the env override for
# operators who want to trade Service Bus peek frequency against freshness.
_DEFAULT_MESSAGE_FLOW_TTL_SECONDS = 10.0


def _message_flow_ttl_seconds() -> float:
    """Resolve the message-flow snapshot TTL (env override, fail-safe default)."""
    raw = os.environ.get("MONITOR_MESSAGE_FLOW_TTL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_MESSAGE_FLOW_TTL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MESSAGE_FLOW_TTL_SECONDS
    return value if value > 0 else _DEFAULT_MESSAGE_FLOW_TTL_SECONDS


@router.get("/message-flow")
def message_flow(
    limit: int = Query(default=200, ge=1, le=200),
    refresh: bool = Query(
        default=False,
        description=(
            "Bypass the ~30s snapshot cache and re-query the Table + Service Bus "
            "immediately. Used by the modal's manual refresh control so an operator "
            "can catch a lingering queue message without waiting out the cache TTL."
        ),
    ),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the Service Bus message-flow snapshot (read-only).

    Returns ``{"enabled": false}`` when the integration is off so the SPA hides
    the card. On any unexpected failure the response degrades to the same
    disabled shape rather than surfacing an error to the dashboard.

    The enabled snapshot is served through the shared monitor cache, but with a
    dedicated, shorter TTL (``MONITOR_MESSAGE_FLOW_TTL_SECONDS``, default 10s)
    than the 30s monitor default so a live request-queue message surfaces faster
    on this card without changing the freshness of any other monitor card. The
    per-poll Table scan + Service Bus call still run at most once per TTL window
    regardless of how many browser tabs are open. The cache key is
    isolated per caller (or a single ``shared`` bucket when the dev
    shared-visibility flag is on) so one caller's private active-job list is
    never served to another from a shared cache entry. ``refresh=true`` forces a
    synchronous re-query (still stored for subsequent normal reads) so the modal
    refresh control returns an authoritative reading instead of the cached one.
    """
    try:
        from api.services.blast.job_state import blast_shared_visibility_enabled
        from api.services.service_bus_pref import service_bus_enabled

        if not service_bus_enabled():
            return dict(_DISABLED)

        if blast_shared_visibility_enabled():
            scope_key = "shared"
        else:
            scope_key = caller.object_id or "anon"
        from api.services.monitor_cache import cached_snapshot

        return cached_snapshot(
            _cache_key("monitor", "message-flow", scope_key, str(limit)),
            lambda: build_message_flow(
                caller.object_id,
                list_limit=limit,
                tenant_id=getattr(caller, "tenant_id", "") or "",
            ),
            ttl_seconds=_message_flow_ttl_seconds(),
            force=refresh,
        )
    except Exception as exc:
        return cast(dict[str, Any], _graceful("message_flow", exc, empty=dict(_DISABLED)))


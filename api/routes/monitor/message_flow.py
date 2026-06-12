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

from typing import Any, cast

from fastapi import APIRouter, Depends, Query

from api.auth import CallerIdentity, require_caller
from api.routes.monitor.common import _cache_key, _graceful
from api.services.message_flow import build_message_flow

router = APIRouter()

_DISABLED: dict[str, Any] = {"enabled": False}


@router.get("/message-flow")
def message_flow(
    limit: int = Query(default=200, ge=1, le=200),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the Service Bus message-flow snapshot (read-only).

    Returns ``{"enabled": false}`` when the integration is off so the SPA hides
    the card. On any unexpected failure the response degrades to the same
    disabled shape rather than surfacing an error to the dashboard.

    The enabled snapshot is served through the shared monitor cache (TTL ~30s)
    so the per-poll Table scan + Service Bus management call run at most once per
    window regardless of how many browser tabs are open. The cache key is
    isolated per caller (or a single ``shared`` bucket when the dev
    shared-visibility flag is on) so one caller's private active-job list is
    never served to another from a shared cache entry.
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
            lambda: build_message_flow(caller.object_id, list_limit=limit),
        )
    except Exception as exc:
        return cast(dict[str, Any], _graceful("message_flow", exc, empty=dict(_DISABLED)))


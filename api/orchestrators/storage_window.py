"""Storage public-network-access auto-toggle orchestrator.

Encodes the discipline from `azure-prereq.md` Step 9:
- Storage account is normally `Disabled`.
- For each ElasticBLAST run we Enable, wait for propagation, do work, Disable.

This orchestrator is intentionally minimal — it just flips the switch and
sleeps for a TTL, then re-disables. The UI calls it as a fire-and-forget
"keep public access on for N minutes" affordance.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import azure.durable_functions as df

DEFAULT_TTL_SECONDS = 5 * 60
PROPAGATION_DELAY_SECONDS = 15


def storage_public_access_window_orchestrator(
    context: df.DurableOrchestrationContext,
) -> dict[str, Any]:
    """Enable public access, wait, then disable. Always disables on exit."""
    request: dict[str, Any] = context.get_input() or {}
    ttl_seconds = int(request.get("ttl_seconds", DEFAULT_TTL_SECONDS))

    enable_payload = {**request, "enabled": True}
    disable_payload = {**request, "enabled": False}

    yield context.call_activity("set_storage_public_access_activity", enable_payload)
    propagation = context.current_utc_datetime + timedelta(seconds=PROPAGATION_DELAY_SECONDS)
    yield context.create_timer(propagation)
    context.set_custom_status({"phase": "enabled", "ttl_seconds": ttl_seconds})

    deadline = context.current_utc_datetime + timedelta(seconds=ttl_seconds)
    yield context.create_timer(deadline)

    try:
        yield context.call_activity("set_storage_public_access_activity", disable_payload)
        return {"final_state": "Disabled"}
    except Exception:
        return {"final_state": "Unknown"}

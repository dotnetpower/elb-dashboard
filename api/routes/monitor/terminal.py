"""Terminal topology monitor routes.

Responsibility: Terminal topology monitor routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `terminal_status`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py
api/tests/test_monitor_cache.py`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.auth import CallerIdentity, require_caller

router = APIRouter()


@router.get("/terminal")
def terminal_status(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    vm_name: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    return {
        "vm_name": "",
        "power_state": "n/a",
        "provisioning_state": "n/a",
        "fqdn": "",
        "public_ip": "",
        "size": "",
        "degraded": True,
        "degraded_reason": "no_terminal_vm_in_container_apps_topology",
    }


# ---------------------------------------------------------------------------
# Cluster card (phase-0 stub, kept for legacy SPA paths)
# ---------------------------------------------------------------------------

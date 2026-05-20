"""Terminal topology monitor routes."""

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

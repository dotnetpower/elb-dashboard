"""Legacy cluster-card monitor route.

Responsibility: Legacy cluster-card monitor route
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `cluster_stub`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py
api/tests/test_monitor_cache.py`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.auth import CallerIdentity, require_caller

router = APIRouter()


@router.get("/cluster")
def cluster_stub(caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    return {
        "status": "stub",
        "caller_oid": caller.object_id,
        "note": "use /api/monitor/aks?resource_group=... for real data",
    }


# ---------------------------------------------------------------------------
# Jobs (read jobstate from Storage table)
# ---------------------------------------------------------------------------

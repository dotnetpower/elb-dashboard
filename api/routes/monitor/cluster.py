"""Legacy cluster-card monitor route."""

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

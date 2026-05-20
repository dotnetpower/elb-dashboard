"""/api/blast schedule routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _stub_log

router = APIRouter()


@router.get("/schedules")
def blast_schedules_list(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/list")
    return {
        "schedules": [],
        "degraded": True,
        "degraded_reason": "beat_scheduler_not_yet_implemented",
    }


@router.post("/schedules")
def blast_schedules_create(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/create", body_keys=list(body.keys()))
    raise HTTPException(
        503,
        detail={
            "code": "celery_beat_pending",
            "message": "Beat-driven schedules not yet implemented in the Container Apps backend.",
        },
    )


@router.delete("/schedules/{schedule_id}")
def blast_schedules_delete(
    schedule_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/delete", id=schedule_id)
    raise HTTPException(503, detail={"code": "celery_beat_pending"})


@router.post("/schedules/{schedule_id}/run")
def blast_schedules_run(
    schedule_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/run", id=schedule_id)
    raise HTTPException(503, detail={"code": "celery_beat_pending"})

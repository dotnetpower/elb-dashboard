"""Job-state monitor routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller
from api.routes.monitor.common import _graceful

router = APIRouter()


@router.get("/jobs")
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        rows = repo.list_for_owner(caller.object_id, limit=limit)
        return {
            "jobs": [
                {
                    "job_id": j.job_id,
                    "type": j.type,
                    "status": j.status,
                    "phase": j.phase,
                    "task_id": j.task_id,
                    "created_at": j.created_at,
                    "updated_at": j.updated_at,
                    "error_code": j.error_code,
                }
                for j in rows
            ]
        }
    except Exception as exc:
        return _graceful("list_jobs", exc, empty={"jobs": []})


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        if state.owner_oid and state.owner_oid != caller.object_id:
            raise HTTPException(403, "not owner")
        history = repo.get_history(job_id, limit=200)
        return {
            "state": {
                "job_id": state.job_id,
                "type": state.type,
                "status": state.status,
                "phase": state.phase,
                "task_id": state.task_id,
                "owner_oid": state.owner_oid,
                "tenant_id": state.tenant_id,
                "error_code": state.error_code,
                "created_at": state.created_at,
                "updated_at": state.updated_at,
                "payload": state.payload,
            },
            "history": history,
        }
    except HTTPException:
        raise
    except Exception as exc:
        return _graceful("get_job", exc, empty={"state": None, "history": []})


# ---------------------------------------------------------------------------
# Control-plane sidecars — snapshot + ticket + SSE
#
# Browsers cannot attach Authorization headers to `EventSource`, so we mirror
# the ticket pattern from /api/terminal/ws: the SPA POSTs to /sidecars/ticket
# with its bearer, gets a single-use opaque token back, then connects to
# /sidecars/events?ticket=... — the GET handler validates the ticket
# without re-reading the bearer.
# ---------------------------------------------------------------------------

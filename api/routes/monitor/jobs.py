"""Job-state monitor routes.

Responsibility: Job-state monitor routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `list_jobs`, `get_job`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py
api/tests/test_monitor_cache.py`.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller
from api.routes.monitor.common import _graceful
from api.services.blast.job_state import (
    _assert_job_owner,
    blast_shared_visibility_enabled,
)

router = APIRouter()


def _build_jobs_snapshot(caller: CallerIdentity, limit: int) -> dict[str, Any]:
    """Read the most-recent jobs for the caller and project the summary shape.

    Pulled out of the route so the polling hot path can serve it through the
    shared monitor cache (SWR): the per-poll full-table scan + in-memory sort
    in ``_list_recent_sorted`` runs at most once per TTL window regardless of
    how many browser tabs poll ``/api/monitor/jobs``.
    """
    from api.services.state_repo import get_state_repo

    repo = get_state_repo()
    # Only summary fields are returned; ``include_payload=False`` skips the
    # potentially-large payload_json column on the Table-Storage row so
    # dashboard polls do not pull megabytes per refresh.
    if blast_shared_visibility_enabled() and hasattr(repo, "list_all"):
        rows = repo.list_all(limit=limit, include_payload=False)
    else:
        rows = repo.list_for_owner(caller.object_id, limit=limit, include_payload=False)
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


@router.get("/jobs")
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api.services.monitor_cache import cached_snapshot

        # Cache key is isolated per caller (or a single ``shared`` bucket when
        # the dev shared-visibility flag is on) so one caller's job list is
        # never served to another from a shared entry — mirrors the
        # message-flow card's keying.
        if blast_shared_visibility_enabled():
            scope_key = "shared"
        else:
            scope_key = caller.object_id or "anon"
        return cached_snapshot(
            f"monitor:jobs:{scope_key}:{limit}",
            lambda: _build_jobs_snapshot(caller, limit),
            ttl_seconds=10.0,
        )
    except Exception as exc:
        return cast(dict[str, Any], _graceful("list_jobs", exc, empty={"jobs": []}))


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    try:
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        _assert_job_owner(state.owner_oid, caller)
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
        return cast(dict[str, Any], _graceful("get_job", exc, empty={"state": None, "history": []}))


# ---------------------------------------------------------------------------
# Control-plane sidecars — snapshot + ticket + SSE
#
# Browsers cannot attach Authorization headers to `EventSource`, so we mirror
# the ticket pattern from /api/terminal/ws: the SPA POSTs to /sidecars/ticket
# with its bearer, gets a single-use opaque token back, then connects to
# /sidecars/events?ticket=... — the GET handler validates the ticket
# without re-reading the bearer.
# ---------------------------------------------------------------------------

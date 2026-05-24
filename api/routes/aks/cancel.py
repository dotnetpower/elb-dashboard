"""AKS provision cancellation route.

Responsibility: HTTP wrapper that revokes a running `provision_aks`
    Celery task and marks its JobState row as `cancelled`. Pure-domain
    work (Azure cleanup) intentionally **not** performed — Azure does
    not expose a clean "stop in-flight create_or_update" verb, and
    deleting a half-built cluster from a cancelled task races with the
    LRO completing. The user can hit the Delete button on the cluster
    card if Azure ends up creating the resource anyway.
Edit boundaries: HTTP shaping only. Ownership check mirrors the
    `/api/tasks/{id}` contract so a different caller cannot cancel
    another tenant's task.
Key entry points: `aks_cancel_provision`.
Risky contracts: Revoking with `terminate=True` only works if the
    Celery worker honors SIGTERM. Workers running long-blocking
    Azure SDK polls will pick up the revocation only at the next
    yield (typically <= 20 s — the ARM poll interval). The route
    documents this in the response payload.
Validation: `uv run pytest -q api/tests/test_aks_cancel_provision.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path

from api.auth import CallerIdentity, require_caller
from api.celery_app import celery_app
from api.services.state_repo import JobStateRepository
from api.tasks.azure.helpers import update_state

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _enforce_task_ownership(task_id: str, caller: CallerIdentity) -> None:
    """Reject if the caller is not the original task owner.

    Mirrors `api.routes.tasks._enforce_task_ownership` exactly so the
    cancel route never offers a softer authorization than the read
    route — otherwise an attacker who could not *see* a task could
    still *cancel* it.
    """
    try:
        state = JobStateRepository().find_by_task_id(task_id)
    except Exception as exc:
        LOGGER.warning(
            "cancel ownership lookup failed task_id=%s err=%s",
            task_id,
            type(exc).__name__,
        )
        if os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true":
            return
        raise HTTPException(
            status_code=503,
            detail={"code": "ownership_check_unavailable", "retryable": True},
        ) from exc
    if state is None:
        return
    owner = getattr(state, "owner_oid", None)
    if owner and owner != caller.object_id:
        raise HTTPException(status_code=403, detail="not owner")


@router.post("/cancel-provision/{task_id}")
def aks_cancel_provision(
    task_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Revoke a running `provision_aks` Celery task.

    The route is idempotent — calling it on an already-terminal task
    (SUCCESS / FAILURE / already REVOKED) returns 200 with the
    current status and a `was_running=False` flag so the FE knows
    not to flash a "cancelled" toast on noise.
    """
    _enforce_task_ownership(task_id, caller)

    from celery.result import AsyncResult

    result = AsyncResult(task_id, app=celery_app)
    status = str(result.status or "PENDING").upper()
    was_running = status in {"PENDING", "RECEIVED", "STARTED", "RETRY"}

    if was_running:
        try:
            # `terminate=True` sends SIGTERM to the worker process. The
            # provision task is mostly blocking inside the Azure SDK
            # ARM poll loop, so revocation lands at the next yield
            # (~_ARM_POLL_INTERVAL_SECONDS = 20 s). The signal arg keeps
            # the default behaviour explicit so we don't accidentally
            # hard-kill the worker on a future Celery upgrade.
            celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
        except Exception as exc:
            LOGGER.warning(
                "celery revoke failed task_id=%s err=%s",
                task_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=502,
                detail={"code": "revoke_failed", "retryable": True},
            ) from exc

    # Best-effort state update. We look up by task_id (not job_id) to
    # avoid forcing the caller to know both — `update_state` operates on
    # job_id so we go through the repo.
    job_id: str | None = None
    try:
        state = JobStateRepository().find_by_task_id(task_id)
        if state is not None:
            job_id = state.job_id
            update_state(
                job_id,
                "cancelled_by_user",
                status="cancelled",
                error_code="cancelled_by_user",
            )
    except Exception as exc:
        LOGGER.debug("state update on cancel failed: %s", type(exc).__name__)

    return {
        "task_id": task_id,
        "job_id": job_id,
        "previous_status": status,
        "was_running": was_running,
        "cancelled": True,
        # The worker may take up to one ARM poll interval (~20 s) to
        # honor the SIGTERM; the FE surfaces this in the toast.
        "settle_after_seconds": 20 if was_running else 0,
    }

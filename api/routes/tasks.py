"""Task status endpoint - poll Celery task results.

Responsibility: Task status endpoint - poll Celery task results
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `get_task_status`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Path

from api.auth import CallerIdentity, require_caller
from api.celery_app import celery_app
from api.services.state.repository import JobStateRepository

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _enforce_task_ownership(task_id: str, caller: CallerIdentity) -> None:
    """Reject the request unless ``caller`` owns the JobState for ``task_id``.

    See `api.routes.operations._enforce_task_ownership` for the shared
    contract. The two helpers must stay behaviour-equivalent so the
    legacy `/api/tasks/{id}` alias never offers a softer ownership gate
    than the canonical `/api/operations/{id}` route.
    """
    try:
        state = JobStateRepository().find_by_task_id(task_id)
    except Exception as exc:
        LOGGER.warning(
            "tasks ownership lookup failed task_id=%s err=%s",
            task_id,
            type(exc).__name__,
        )
        if os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true":
            return
        # Local dev escape hatch: a workstation `az login` identity often
        # lacks Storage Table RBAC on the deployed account, so this lookup
        # 503s and the FE task-status poller never advances (a freshly
        # provisioned cluster looks like it's spinning forever even though
        # the worker is actually running). When no `CONTAINER_APP_NAME` is
        # set we are by definition not the deployed control plane, so
        # logging a warning and proceeding is strictly safer than crashing
        # the loop. Production (where `CONTAINER_APP_NAME` is always set
        # by Azure Container Apps) still fails closed.
        if not os.environ.get("CONTAINER_APP_NAME"):
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


@router.get("/{task_id}")
def get_task_status(
    task_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the current state of a Celery async task."""
    _enforce_task_ownership(task_id, caller)
    result = AsyncResult(task_id, app=celery_app)

    response: dict[str, Any] = {
        "task_id": task_id,
        "status": result.status,  # PENDING, STARTED, SUCCESS, FAILURE, RETRY, REVOKED
        "ready": result.ready(),
    }

    if result.ready():
        if result.successful():
            response["result"] = result.result
        elif result.failed():
            response["error"] = str(result.result)
    elif result.info and isinstance(result.info, dict):
        # Task can publish progress via self.update_state(meta={...})
        response["progress"] = result.info

    return response

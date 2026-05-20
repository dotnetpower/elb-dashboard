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
from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, Path

from api.auth import CallerIdentity, require_caller
from api.celery_app import celery_app

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{task_id}")
def get_task_status(
    task_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the current state of a Celery async task."""
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

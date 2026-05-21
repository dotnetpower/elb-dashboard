"""Operation status routes for long-running control-plane workflows.

Responsibility: Operation status routes for long-running control-plane workflows.
Edit boundaries: Keep HTTP response shaping here; task execution and domain state stay in
task modules and services.
Key entry points: `get_operation_status`.
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate; keep `/api/tasks/{id}` as a legacy alias while `/api/operations/{id}` is adopted.
Validation: `uv run pytest -q api/tests/test_operations_route.py`.
"""

from __future__ import annotations

from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, Path, Request

from api.auth import CallerIdentity, require_caller
from api.celery_app import celery_app
from api.services.response_contracts import build_meta, build_operation, request_id_from_scope

router = APIRouter(prefix="/api/operations", tags=["operations"])

_CELERY_TO_OPERATION_STATE = {
    "PENDING": "queued",
    "RECEIVED": "queued",
    "STARTED": "running",
    "RETRY": "retrying",
    "SUCCESS": "succeeded",
    "FAILURE": "failed",
    "REVOKED": "cancelled",
}


def _operation_state(celery_status: str) -> str:
    return _CELERY_TO_OPERATION_STATE.get(celery_status.upper(), celery_status.lower())


def _progress_payload(result: AsyncResult[Any]) -> dict[str, Any] | None:
    if result.info and isinstance(result.info, dict):
        return dict(result.info)
    return None


@router.get("/{operation_id}")
def get_operation_status(
    request: Request,
    operation_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the current state of a long-running operation.

    The first implementation is a Celery projection. This keeps the public API
    centered on `operation_id` while preserving the existing task backend.
    """

    del caller
    result: AsyncResult[Any] = AsyncResult(operation_id, app=celery_app)
    celery_status = str(result.status)
    state = _operation_state(celery_status)
    response: dict[str, Any] = {
        "status": "ok",
        "operation": build_operation(
            operation_id=operation_id,
            operation_type="celery.task",
            state=state,
            links={
                "self": f"/api/operations/{operation_id}",
                "legacy_task": f"/api/tasks/{operation_id}",
            },
        ),
        "celery": {
            "task_id": operation_id,
            "status": celery_status,
            "ready": result.ready(),
        },
        "meta": build_meta(request_id=request_id_from_scope(request)),
    }
    progress = _progress_payload(result)
    if progress is not None:
        response["progress"] = progress
    if result.ready():
        if result.successful():
            response["result"] = result.result
        elif result.failed():
            response["error"] = str(result.result)
    return response

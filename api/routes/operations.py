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

import logging
import os
from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Path, Request

from api.auth import CallerIdentity, require_caller
from api.celery_app import celery_app
from api.services.response_contracts import build_meta, build_operation, request_id_from_scope
from api.services.state_repo import JobStateRepository

LOGGER = logging.getLogger(__name__)

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


def _enforce_task_ownership(task_id: str, caller: CallerIdentity) -> None:
    """Reject the request unless ``caller`` owns the JobState for ``task_id``.

    Lookup misses (no JobState row — system / diag tasks such as
    ``diag_noop``) are permitted: those tasks do not carry per-user
    payload.

    Lookup *failures* fail **closed** with 503: a transient table outage
    or a credential blip must not be exploitable as an ownership bypass.
    ``AUTH_DEV_BYPASS=true`` is the single exception — without a real
    state backend the dev loop would otherwise hard-fail on every call,
    and the dev-bypass synthetic identity is already trust-flagged.
    """
    try:
        state = JobStateRepository().find_by_task_id(task_id)
    except Exception as exc:
        LOGGER.warning(
            "operations ownership lookup failed task_id=%s err=%s",
            task_id,
            type(exc).__name__,
        )
        if os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true":
            return
        # Local dev escape hatch — same reasoning as in api/routes/tasks.py:
        # a workstation `az login` without Storage Table RBAC would otherwise
        # 503 every operation poll and freeze the UI even when the worker
        # is healthy. Production (CONTAINER_APP_NAME set by Azure Container
        # Apps) keeps the strict fail-closed behaviour.
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

    _enforce_task_ownership(operation_id, caller)
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

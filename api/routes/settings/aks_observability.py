"""AKS Container Insights settings routes.

Responsibility: Read the omsagent addon state on an AKS cluster and enqueue
Celery tasks to enable/disable it.
Edit boundaries: HTTP shaping only. SDK wrapper lives in
`api.services.aks_observability`. Long-running enablement runs through
`api.tasks.azure.enable_aks_container_insights` or
`api.tasks.azure.disable_aks_container_insights`.
Key entry points: `get_status`, `enable`, `disable`.
Risky contracts: Every route enforces `require_caller`. The cluster
`begin_create_or_update` is *additive* on `addon_profiles` to avoid
clobbering other addons.
Validation: `uv run pytest -q api/tests/test_settings_aks_observability.py`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay
from api.services import get_credential
from api.services.aks_observability import (
    get_container_insights_status,
)
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_RE_SUB = re.compile(r"^[0-9a-fA-F-]{36}$")
_RE_RG = re.compile(r"^[-\w._()]{1,90}$")
_RE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{1,254}$")
_RE_WORKSPACE_ID = re.compile(
    r"^/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[-\w._()]{1,90}"
    r"/providers/Microsoft\.OperationalInsights/workspaces/[a-zA-Z0-9][a-zA-Z0-9._\-]{1,254}$"
)


def _require(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise HTTPException(400, f"invalid {label}: '{sanitise(str(value)[:80])}'")
    return value


@router.get("")
def get_status(
    subscription_id: str = Query(...),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the current Container Insights state for the AKS cluster."""
    _require(subscription_id, _RE_SUB, "subscription_id")
    _require(resource_group, _RE_RG, "resource_group")
    _require(cluster_name, _RE_NAME, "cluster_name")

    cred = get_credential()
    try:
        state = get_container_insights_status(
            cred, subscription_id, resource_group, cluster_name
        )
    except ResourceNotFoundError as exc:
        raise HTTPException(404, "AKS cluster not found") from exc
    except Exception as exc:
        LOGGER.warning(
            "aks_observability status failed cluster=%s err=%s",
            cluster_name,
            type(exc).__name__,
        )
        raise HTTPException(
            500, f"failed to read container insights state: {sanitise(str(exc))[:200]}"
        ) from exc
    return state


@router.post("/enable")
def enable(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Enqueue the enable_aks_container_insights Celery task."""
    subscription_id = _require(body.get("subscription_id"), _RE_SUB, "subscription_id")
    resource_group = _require(body.get("resource_group"), _RE_RG, "resource_group")
    cluster_name = _require(body.get("cluster_name"), _RE_NAME, "cluster_name")
    workspace_resource_id = _require(
        body.get("workspace_resource_id"), _RE_WORKSPACE_ID, "workspace_resource_id"
    )

    from api.tasks.azure import enable_aks_container_insights

    result = _safe_delay(
        enable_aks_container_insights,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        workspace_resource_id=workspace_resource_id,
    )
    LOGGER.info(
        "aks_observability enable enqueued by oid=%s sub=%s cluster=%s workspace=%s",
        caller.object_id,
        subscription_id,
        cluster_name,
        workspace_resource_id.rsplit("/", 1)[-1],
    )
    return {
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }


@router.post("/disable")
def disable(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Enqueue the disable_aks_container_insights Celery task."""
    subscription_id = _require(body.get("subscription_id"), _RE_SUB, "subscription_id")
    resource_group = _require(body.get("resource_group"), _RE_RG, "resource_group")
    cluster_name = _require(body.get("cluster_name"), _RE_NAME, "cluster_name")

    from api.tasks.azure import disable_aks_container_insights

    result = _safe_delay(
        disable_aks_container_insights,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    LOGGER.info(
        "aks_observability disable enqueued by oid=%s sub=%s cluster=%s",
        caller.object_id,
        subscription_id,
        cluster_name,
    )
    return {
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }

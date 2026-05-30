"""Application Insights settings routes.

Responsibility: Read the deployment-injected connection string, look up
existing Application Insights components, and enqueue Celery tasks to create
a new component or apply an existing connection string to server sidecars.
Edit boundaries: HTTP shaping only. SDK work lives in
`api.services.app_insights_provisioning`. Long-running provision runs through
`api.tasks.azure.provision_app_insights` and
`api.tasks.azure.apply_app_insights_to_deployment`.
Key entry points: `get_status`, `lookup`, `provision`, `apply_to_deployment`.
Risky contracts: Every route enforces `require_caller`. The deployment
connection string is returned verbatim because it is a write-only telemetry
credential (Microsoft pattern); routes never log it.
Validation: `uv run pytest -q api/tests/test_settings_app_insights.py`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay
from api.services import get_credential
from api.services.app_insights_provisioning import (
    deployment_connection_string,
    find_application_insights_by_name,
    get_application_insights,
)
from api.services.sanitise import redact_oid, sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_RE_SUB = re.compile(r"^[0-9a-fA-F-]{36}$")
_RE_RG = re.compile(r"^[-\w._()]{1,90}$")
_RE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{1,254}$")
_RE_REGION = re.compile(r"^[a-z][a-z0-9]{2,29}$")


def _require_connection_string(value: Any) -> str:
    if not isinstance(value, str):
        raise HTTPException(400, "invalid connection_string")
    connection_string = value.strip()
    if not connection_string or len(connection_string) > 4096:
        raise HTTPException(400, "invalid connection_string")
    if "InstrumentationKey=" not in connection_string:
        raise HTTPException(400, "invalid connection_string")
    return connection_string


def _require(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise HTTPException(400, f"invalid {label}: '{sanitise(str(value)[:40])}'")
    return value


_ALLOWED_RETENTION_DAYS = (7, 14, 30, 60, 90, 120, 180, 270, 365, 550, 730)


def _require_retention_days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "invalid retention_days: must be an integer") from exc
    if days not in _ALLOWED_RETENTION_DAYS:
        raise HTTPException(
            400,
            f"invalid retention_days: must be one of {list(_ALLOWED_RETENTION_DAYS)}",
        )
    return days


@router.get("")
def get_status(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Return the deployment-injected connection string (or empty when unset).

    The SPA `useAppInsights` hook combines this with a user-supplied value in
    `localStorage["elb-prefs"].appInsightsConnectionString`. User-supplied
    takes precedence; if both are empty the SDK is not initialised.
    """
    cs = deployment_connection_string()
    return {
        "deployment_connection_string": cs,
        "deployment_configured": bool(cs),
    }


@router.post("/lookup")
def lookup(
    body: dict[str, Any] = Body(...),
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Look up an existing Application Insights component without creating one.

    Used by the Settings panel to populate the connection string field when
    the user types an existing resource name instead of clicking "Provision".
    """
    subscription_id = _require(body.get("subscription_id"), _RE_SUB, "subscription_id")
    resource_group_raw = body.get("resource_group")
    resource_group = (
        _require(resource_group_raw, _RE_RG, "resource_group") if resource_group_raw else ""
    )
    component_name = _require(body.get("component_name"), _RE_NAME, "component_name")

    cred = get_credential()
    try:
        if resource_group:
            component = get_application_insights(
                cred, subscription_id, resource_group, component_name
            )
        else:
            matches = find_application_insights_by_name(cred, subscription_id, component_name)
            if len(matches) > 1:
                raise HTTPException(
                    409,
                    {
                        "code": "multiple_components_found",
                        "message": (
                            "multiple App Insights resources match this name; "
                            "specify a resource group"
                        ),
                        "matches": [m.get("id") for m in matches],
                    },
                )
            component = matches[0] if matches else None
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        LOGGER.warning(
            "app_insights lookup failed sub=%s rg=%s name=%s err=%s",
            subscription_id,
            resource_group or "*",
            component_name,
            type(exc).__name__,
        )
        raise HTTPException(
            500, f"failed to look up application insights: {sanitise(str(exc))[:200]}"
        ) from exc
    if component is None:
        raise HTTPException(404, "application insights component not found")
    return {"component": component}


@router.post("/provision")
def provision(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Enqueue the provision_app_insights Celery task and return the task id."""
    subscription_id = _require(body.get("subscription_id"), _RE_SUB, "subscription_id")
    resource_group = _require(body.get("resource_group"), _RE_RG, "resource_group")
    component_name = _require(body.get("component_name"), _RE_NAME, "component_name")
    region = _require(body.get("region"), _RE_REGION, "region")
    workspace_name = _require(body.get("workspace_name"), _RE_NAME, "workspace_name")
    workspace_rg_raw = body.get("workspace_resource_group")
    workspace_rg = (
        _require(workspace_rg_raw, _RE_RG, "workspace_resource_group") if workspace_rg_raw else None
    )
    retention_days_raw = body.get("retention_days")
    retention_days = (
        _require_retention_days(retention_days_raw) if retention_days_raw is not None else None
    )

    from api.tasks.azure import provision_app_insights

    result = _safe_delay(
        provision_app_insights,
        subscription_id=subscription_id,
        resource_group=resource_group,
        component_name=component_name,
        region=region,
        workspace_name=workspace_name,
        workspace_resource_group=workspace_rg,
        retention_days=retention_days,
    )
    LOGGER.info(
        "app_insights provision enqueued by oid=%s sub=%s rg=%s name=%s",
        redact_oid(caller.object_id),
        subscription_id,
        resource_group,
        component_name,
    )
    return {
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }


@router.post("/apply")
def apply_to_deployment(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Enqueue a task that applies an App Insights string to server sidecars."""
    connection_string = _require_connection_string(body.get("connection_string"))

    from api.tasks.azure import apply_app_insights_to_deployment

    result = _safe_delay(
        apply_app_insights_to_deployment,
        connection_string=connection_string,
    )
    LOGGER.info(
        "app_insights deployment apply enqueued by oid=%s",
        redact_oid(caller.object_id),
    )
    return {
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }


@router.post("/clear")
def clear_deployment(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Enqueue a task that removes the App Insights env var from server sidecars.

    The Settings panel calls this when the operator clicks "Clear server
    override" — it reverts api/worker/beat to whatever the Bicep / deployment
    template provided originally.
    """
    from api.tasks.azure import clear_app_insights_from_deployment

    result = _safe_delay(clear_app_insights_from_deployment)
    LOGGER.info(
        "app_insights deployment clear enqueued by oid=%s",
        redact_oid(caller.object_id),
    )
    return {
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }

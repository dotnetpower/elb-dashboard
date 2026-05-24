"""Celery tasks: provision an Application Insights component on demand.

Responsibility: Wrap the long-running ARM pollers in `app_insights_provisioning`
behind a Celery task so the SPA can fire-and-poll instead of holding a request
open for 30-90 s while a Log Analytics workspace + AI component are created.
Edit boundaries: Orchestration only. SDK wrappers live in
`api.services.app_insights_provisioning`.
Key entry points: `provision_app_insights` (Celery task name
`api.tasks.azure.provision_app_insights`).
Risky contracts: Must be idempotent — the SPA may retry on transient failure.
The underlying service helpers already short-circuit when the resource exists.
Validation: `uv run pytest -q api/tests/test_app_insights_task.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from api.services import get_credential
from api.services.app_insights_provisioning import (
    ensure_application_insights,
    ensure_log_analytics_workspace,
)
from api.tasks.azure.helpers import publish_progress

LOGGER = logging.getLogger(__name__)


@shared_task(  # type: ignore[misc]
    bind=True,
    name="api.tasks.azure.provision_app_insights",
    autoretry_for=(),
    max_retries=0,
)
def provision_app_insights(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    component_name: str,
    region: str,
    workspace_name: str,
    workspace_resource_group: str | None = None,
) -> dict[str, Any]:
    """Create (or look up) a workspace-based Application Insights component.

    Steps:
      1. ensure Log Analytics workspace in ``workspace_resource_group`` (or the
         AI resource group when not supplied)
      2. ensure Application Insights component linked to that workspace

    Returns the AI component snapshot — most importantly ``connection_string``,
    which the SPA persists into ``elb-prefs`` so subsequent loads initialise
    the App Insights JS SDK without re-running this task.
    """
    job_id = self.request.id or "provision_app_insights"
    ws_rg = workspace_resource_group or resource_group

    publish_progress(
        self,
        job_id,
        "ensuring_workspace",
        step=1,
        total_steps=2,
        message=f"Ensuring Log Analytics workspace {workspace_name}",
    )
    cred = get_credential()
    workspace = ensure_log_analytics_workspace(
        cred,
        subscription_id=subscription_id,
        resource_group=ws_rg,
        workspace_name=workspace_name,
        region=region,
    )

    publish_progress(
        self,
        job_id,
        "ensuring_component",
        step=2,
        total_steps=2,
        message=f"Ensuring Application Insights component {component_name}",
        workspace_id=workspace.get("id"),
    )
    component = ensure_application_insights(
        cred,
        subscription_id=subscription_id,
        resource_group=resource_group,
        component_name=component_name,
        region=region,
        workspace_resource_id=workspace["id"],
    )

    publish_progress(
        self,
        job_id,
        "completed",
        step=2,
        total_steps=2,
        status="succeeded",
        message="Application Insights ready",
    )
    return {
        "workspace": workspace,
        "component": component,
        "connection_string": component.get("connection_string"),
    }

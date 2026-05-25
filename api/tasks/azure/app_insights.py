"""Celery tasks: provision an Application Insights component on demand.

Responsibility: Wrap the long-running ARM pollers in `app_insights_provisioning`
behind a Celery task so the SPA can fire-and-poll instead of holding a request
open for 30-90 s while a Log Analytics workspace + AI component are created.
Edit boundaries: Orchestration only. SDK wrappers live in
`api.services.app_insights_provisioning`.
Key entry points: `provision_app_insights` (Celery task name
`api.tasks.azure.provision_app_insights`), `apply_app_insights_to_deployment`.
Risky contracts: Must be idempotent — the SPA may retry on transient failure.
The underlying service helpers already short-circuit when the resource exists.
Validation: `uv run pytest -q api/tests/test_settings_app_insights.py
api/tests/test_upgrade_aca_template.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from celery import shared_task

from api.services import get_credential
from api.services.app_insights_provisioning import (
    ensure_application_insights,
    ensure_log_analytics_workspace,
)
from api.services.upgrade.aca_template import (
    CONTAINER_APP_NAME_ENV,
    apply_app_insights_connection_string,
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
        3. apply the connection string to the api / worker / beat sidecars when
            running inside the deployed Container App

    Returns the AI component snapshot — most importantly ``connection_string``,
    which the SPA persists into ``elb-prefs`` so subsequent loads initialise
    the App Insights JS SDK without re-running this task. In production the
    same connection string is also written into the Container App template so
    server-side logs/traces start exporting after the new revision rolls out.
    """
    job_id = self.request.id or "provision_app_insights"
    ws_rg = workspace_resource_group or resource_group

    publish_progress(
        self,
        job_id,
        "ensuring_workspace",
        step=1,
        total_steps=3,
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
        total_steps=3,
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

    connection_string = str(component.get("connection_string") or "").strip()
    deployment_apply = _apply_connection_string_to_deployment(
        self,
        job_id=job_id,
        connection_string=connection_string,
        step=3,
        total_steps=3,
    )

    publish_progress(
        self,
        job_id,
        "completed",
        step=3,
        total_steps=3,
        status="succeeded",
        message=(
            "Application Insights ready and server telemetry applied"
            if deployment_apply.get("status") == "applied"
            else "Application Insights ready"
        ),
    )
    return {
        "workspace": workspace,
        "component": component,
        "connection_string": connection_string,
        "deployment_apply": deployment_apply,
    }


@shared_task(  # type: ignore[misc]
    bind=True,
    name="api.tasks.azure.apply_app_insights_to_deployment",
    autoretry_for=(),
    max_retries=0,
)
def apply_app_insights_to_deployment(
    self: Any,
    *,
    connection_string: str,
) -> dict[str, Any]:
    """Apply an existing App Insights connection string to server sidecars."""
    job_id = self.request.id or "apply_app_insights_to_deployment"
    deployment_apply = _apply_connection_string_to_deployment(
        self,
        job_id=job_id,
        connection_string=connection_string,
        step=1,
        total_steps=1,
    )
    return {"deployment_apply": deployment_apply}


def _apply_connection_string_to_deployment(
    task: Any,
    *,
    job_id: str,
    connection_string: str,
    step: int,
    total_steps: int,
) -> dict[str, Any]:
    if not connection_string:
        return {"status": "skipped", "reason": "empty_connection_string"}
    if not os.environ.get(CONTAINER_APP_NAME_ENV):
        return {"status": "skipped", "reason": "container_app_env_missing"}

    publish_progress(
        task,
        job_id,
        "applying_deployment",
        step=step,
        total_steps=total_steps,
        message="Applying App Insights connection string to api, worker, and beat",
    )
    poller = apply_app_insights_connection_string(connection_string=connection_string)
    result = poller.result()
    revision = getattr(
        getattr(result, "properties", None),
        "latest_revision_name",
        getattr(result, "latest_revision_name", None),
    )
    return {
        "status": "applied",
        "containers": ["api", "worker", "beat"],
        "revision": revision,
    }

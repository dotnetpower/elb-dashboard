"""Celery tasks: enable/disable Container Insights (omsagent addon) on AKS.

Responsibility: Wrap the long-running ManagedClusters.begin_create_or_update
addon patch in a Celery task so the SPA can fire-and-poll instead of holding
a request open for 30-90 s.
Edit boundaries: Orchestration only. SDK wrapper lives in
`api.services.aks_observability`.
Key entry points: `enable_aks_container_insights`,
`disable_aks_container_insights`.
Risky contracts: Idempotent. The service helpers short-circuit when the addon
is already in the requested state.
Validation: `uv run pytest -q api/tests/test_aks_observability_task.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from api.services import get_credential
from api.services.aks_observability import (
    disable_container_insights,
    enable_container_insights,
)
from api.tasks.azure.helpers import publish_progress

LOGGER = logging.getLogger(__name__)


@shared_task(  # type: ignore[misc]
    bind=True,
    name="api.tasks.azure.enable_aks_container_insights",
    autoretry_for=(),
    max_retries=0,
)
def enable_aks_container_insights(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    workspace_resource_id: str,
) -> dict[str, Any]:
    """Patch the omsagent addon on the AKS cluster, returning post-state."""
    job_id = self.request.id or "enable_aks_container_insights"
    publish_progress(
        self,
        job_id,
        "enabling_container_insights",
        step=1,
        total_steps=1,
        message=f"Enabling Container Insights on {cluster_name}",
    )
    cred = get_credential()
    state = enable_container_insights(
        cred,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        workspace_resource_id=workspace_resource_id,
    )
    publish_progress(
        self,
        job_id,
        "completed",
        step=1,
        total_steps=1,
        status="succeeded",
        message="Container Insights enabled",
        **state,
    )
    return state


@shared_task(  # type: ignore[misc]
    bind=True,
    name="api.tasks.azure.disable_aks_container_insights",
    autoretry_for=(),
    max_retries=0,
)
def disable_aks_container_insights(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Disable the omsagent addon on the AKS cluster, returning post-state."""
    job_id = self.request.id or "disable_aks_container_insights"
    publish_progress(
        self,
        job_id,
        "disabling_container_insights",
        step=1,
        total_steps=1,
        message=f"Disabling Container Insights on {cluster_name}",
    )
    cred = get_credential()
    state = disable_container_insights(
        cred,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    publish_progress(
        self,
        job_id,
        "completed",
        step=1,
        total_steps=1,
        status="succeeded",
        message="Container Insights disabled",
        **state,
    )
    return state

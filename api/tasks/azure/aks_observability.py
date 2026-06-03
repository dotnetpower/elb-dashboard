"""Celery tasks: enable/disable Container Insights (omsagent addon) on AKS.

Responsibility: Wrap the long-running ManagedClusters.begin_create_or_update
addon patch in a Celery task so the SPA can fire-and-poll instead of holding
a request open for 30-90 s, and self-heal the linked-scope RBAC the addon
patch needs on the Log Analytics workspace's resource group.
Edit boundaries: Orchestration only. SDK wrapper lives in
`api.services.aks_observability`; the RBAC self-grant lives in
`api.tasks.azure.rbac.ensure_dashboard_mi_resource_group_contributor`.
Key entry points: `enable_aks_container_insights`,
`disable_aks_container_insights`.
Risky contracts: Idempotent. The service helpers short-circuit when the addon
is already in the requested state. Before patching, `enable` self-grants
Contributor to the dashboard MI on the workspace RG (best-effort) and then
retries the addon patch on `LinkedAuthorizationFailed` for a bounded window
(role-assignment propagation). Exhausting the window raises an actionable
error carrying the exact `az role assignment create` recovery command.
Validation: `uv run pytest -q api/tests/test_aks_observability_task.py`.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from celery import shared_task

import api.tasks.azure as _facade
from api.services import get_credential
from api.services.aks_observability import (
    disable_container_insights,
    enable_container_insights,
)
from api.tasks.azure.helpers import publish_progress

LOGGER = logging.getLogger(__name__)

# Bounded retry window for the `LinkedAuthorizationFailed` the addon patch
# raises while the freshly-self-granted Contributor role assignment on the
# workspace RG propagates to the Authorization service. Deliberately capped
# so a *permanently* missing permission (e.g. the MI lacks
# roleAssignments/write entirely) fails fast with an actionable message
# instead of looping forever.
_LINKED_AUTH_RETRY_SECONDS = 150.0
_LINKED_AUTH_INITIAL_DELAY = 10.0
_LINKED_AUTH_MAX_DELAY = 30.0

# Pull the resource-group segment out of an ARM resource id. Literal path
# segments are case-insensitive and Azure returns workspace ids lowercased.
_WORKSPACE_RG_RE = re.compile(r"/resourceGroups/([^/]+)/", re.IGNORECASE)


def _workspace_resource_group(workspace_resource_id: str) -> str | None:
    match = _WORKSPACE_RG_RE.search(workspace_resource_id)
    return match.group(1) if match else None


def _is_linked_authorization_failed(exc: BaseException) -> bool:
    return "LinkedAuthorizationFailed" in str(exc)


def _recovery_command(
    subscription_id: str, workspace_rg: str, mi_principal_id: str
) -> str:
    """Exact CLI a tenant admin can paste to grant the missing permission."""
    principal = mi_principal_id or "<dashboard-managed-identity-object-id>"
    return (
        f"az role assignment create --assignee {principal} "
        "--role Contributor "
        f"--scope /subscriptions/{subscription_id}/resourceGroups/{workspace_rg}"
    )


def _enable_with_linked_auth_retry(
    self: Any,
    job_id: str,
    *,
    cred: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    workspace_resource_id: str,
    recovery_command: str,
) -> dict[str, Any]:
    """Call `enable_container_insights`, retrying on `LinkedAuthorizationFailed`.

    The self-granted Contributor assignment on the workspace RG can take a
    few seconds to become effective for ARM's linked-scope check. Retry the
    (idempotent) addon patch on that specific error until the bounded
    deadline, then raise an actionable error.
    """
    deadline = time.monotonic() + _LINKED_AUTH_RETRY_SECONDS
    delay = _LINKED_AUTH_INITIAL_DELAY
    while True:
        try:
            return enable_container_insights(
                cred,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
                workspace_resource_id=workspace_resource_id,
            )
        except Exception as exc:
            if _is_linked_authorization_failed(exc):
                if time.monotonic() < deadline:
                    LOGGER.info(
                        "container insights enable hit LinkedAuthorizationFailed "
                        "cluster=%s; workspace-RG role propagation, retry in %.0fs",
                        cluster_name,
                        delay,
                    )
                    publish_progress(
                        self,
                        job_id,
                        "waiting_for_workspace_rbac",
                        step=1,
                        total_steps=1,
                        message=(
                            "Waiting for the workspace resource-group permission "
                            "to propagate..."
                        ),
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, _LINKED_AUTH_MAX_DELAY)
                    continue
                raise RuntimeError(
                    "Container Insights enable failed: the dashboard managed "
                    "identity lacks Microsoft.OperationsManagement/solutions/write "
                    "on the Log Analytics workspace's resource group. Grant it and "
                    f"retry: {recovery_command}"
                ) from exc
            raise


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

    # Self-heal the linked-scope RBAC the addon patch needs. Enabling the
    # omsagent addon creates a `ContainerInsights(<workspace>)` OMS solution
    # in the Log Analytics workspace's resource group, which requires
    # `Microsoft.OperationsManagement/solutions/write` there. When the
    # workspace lives in an RG the shared MI has no role on (typically
    # Azure's auto-created `defaultresourcegroup-<loc>`), ARM rejects the
    # patch with `LinkedAuthorizationFailed`. Best-effort grant Contributor
    # (on the ABAC whitelist) to the MI on that RG before patching.
    workspace_rg = _workspace_resource_group(workspace_resource_id)
    mi_principal_id = ""
    if workspace_rg:
        try:
            rbac = _facade._ensure_dashboard_mi_resource_group_contributor(
                cred,
                subscription_id=subscription_id,
                resource_group=workspace_rg,
                progress_callback=lambda phase, message: publish_progress(
                    self, job_id, phase, step=1, total_steps=1, message=message
                ),
            )
            mi_principal_id = rbac.get("mi_principal_id", "")
            if rbac.get("roles_failed"):
                LOGGER.warning(
                    "container insights workspace-RG self-grant incomplete "
                    "cluster=%s workspace_rg=%s failed=%s",
                    cluster_name,
                    workspace_rg,
                    list(rbac["roles_failed"]),
                )
        except Exception as exc:  # never let the self-heal abort the enable
            LOGGER.warning(
                "container insights workspace-RG self-grant raised cluster=%s "
                "workspace_rg=%s err=%s",
                cluster_name,
                workspace_rg,
                type(exc).__name__,
            )

    recovery_command = _recovery_command(
        subscription_id, workspace_rg or "<workspace-resource-group>", mi_principal_id
    )
    state = _enable_with_linked_auth_retry(
        self,
        job_id,
        cred=cred,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        workspace_resource_id=workspace_resource_id,
        recovery_command=recovery_command,
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

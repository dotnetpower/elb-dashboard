"""Application Insights resource provisioning and lookup.

Responsibility: Idempotent helpers that read the deployment-injected
`APPLICATIONINSIGHTS_CONNECTION_STRING` env var, look up existing Application
Insights components, and create new ones (with a backing Log Analytics
workspace) when the caller asks.
Edit boundaries: Azure SDK wrappers only. Do not perform HTTP shaping here —
that lives in `api.routes.settings.app_insights`. Do not block on long ARM
pollers from inside FastAPI request handlers — wrap long calls in a Celery
task (`api.tasks.azure.app_insights`).
Key entry points: `deployment_connection_string`, `get_workspace`,
`ensure_log_analytics_workspace`, `get_application_insights`,
`find_application_insights_by_name`,
`ensure_application_insights`.
Risky contracts: All long-running pollers (`begin_create_or_update`) must
be awaited on the caller's thread — they can take 30-90 s for a fresh
Log Analytics workspace. Callers running in a request thread should defer
to a Celery task.
Validation: `uv run pytest -q api/tests/test_app_insights_provisioning_service.py`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError

LOGGER = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 30

# Naming validators kept lightweight; the underlying ARM API enforces the
# canonical rules so this is purely defence-in-depth against obvious typos
# (and to give the operator a quick local 400 instead of a 60-second ARM
# round-trip failure).
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{1,254}$")


def _validate_name(value: str, label: str) -> None:
    if not _NAME_RE.match(value or ""):
        raise ValueError(f"invalid {label}: {value!r}")


def deployment_connection_string() -> str:
    """Return the effective server-side connection string, or empty string.

    Resolution order:

    1. The ``APPLICATIONINSIGHTS_CONNECTION_STRING`` env var on the running
       sidecar (set by Bicep at deploy time, or by the imperative apply task).
    2. The durable applied override persisted in the ``appinsightspref`` Table
       row. A full ``azd provision`` re-applies the Bicep template with the
       (usually empty) azd env value and wipes the env var; the persisted row
       survives, so telemetry self-heals after a redeploy without the operator
       re-entering the connection string.

    Reading the Table only happens when the env var is empty (the post-provision
    heal path), so the common case stays a zero-I/O env lookup. The fallback
    never raises — a Table/RBAC failure degrades to an empty string.
    """
    env_value = (os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or "").strip()
    if env_value:
        return env_value
    from api.services.app_insights_pref import get_persisted_connection_string

    return get_persisted_connection_string()


def _log_analytics_client(credential: TokenCredential, subscription_id: str) -> Any:
    from azure.mgmt.loganalytics import LogAnalyticsManagementClient

    return LogAnalyticsManagementClient(credential, subscription_id)


def _app_insights_client(credential: TokenCredential, subscription_id: str) -> Any:
    from azure.mgmt.applicationinsights import ApplicationInsightsManagementClient

    return ApplicationInsightsManagementClient(credential, subscription_id)


def get_workspace(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    workspace_name: str,
) -> dict[str, Any] | None:
    """Return a dict describing the Log Analytics workspace, or None when missing.

    Never raises on ``ResourceNotFoundError`` (the missing-workspace path is
    the common case during provisioning). Other ARM exceptions propagate so
    the caller can surface them via the standard error mapper.
    """
    _validate_name(workspace_name, "workspace_name")
    client = _log_analytics_client(credential, subscription_id)
    try:
        ws = client.workspaces.get(resource_group, workspace_name)
    except ResourceNotFoundError:
        return None
    return {
        "id": ws.id,
        "name": ws.name,
        "location": ws.location,
        "customer_id": ws.customer_id,
        "provisioning_state": ws.provisioning_state,
    }


def ensure_log_analytics_workspace(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    workspace_name: str,
    region: str,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> dict[str, Any]:
    """Create the workspace when missing; otherwise return the existing snapshot.

    Idempotent. The poller can take up to ~60 s on a fresh region; routes that
    need to call this must do so via a Celery task (`api.tasks.azure.app_insights`).
    """
    existing = get_workspace(credential, subscription_id, resource_group, workspace_name)
    if existing is not None:
        return existing

    client = _log_analytics_client(credential, subscription_id)
    poller = client.workspaces.begin_create_or_update(
        resource_group,
        workspace_name,
        {
            "location": region,
            "sku": {"name": "PerGB2018"},
            "retention_in_days": retention_days,
            "tags": {"managed-by": "elb-dashboard", "role": "observability"},
        },
    )
    ws = poller.result()
    return {
        "id": ws.id,
        "name": ws.name,
        "location": ws.location,
        "customer_id": ws.customer_id,
        "provisioning_state": ws.provisioning_state,
    }


def get_application_insights(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    component_name: str,
) -> dict[str, Any] | None:
    """Return the AI component snapshot or None when missing."""
    _validate_name(component_name, "component_name")
    client = _app_insights_client(credential, subscription_id)
    try:
        comp = client.components.get(resource_group, component_name)
    except ResourceNotFoundError:
        return None
    return {
        "id": comp.id,
        "name": comp.name,
        "location": comp.location,
        "kind": comp.kind,
        "application_id": comp.app_id,
        "instrumentation_key": comp.instrumentation_key,
        "connection_string": comp.connection_string,
        "workspace_resource_id": comp.workspace_resource_id,
        "provisioning_state": comp.provisioning_state,
    }


def _component_snapshot(comp: Any) -> dict[str, Any]:
    return {
        "id": comp.id,
        "name": comp.name,
        "location": comp.location,
        "kind": comp.kind,
        "application_id": comp.app_id,
        "instrumentation_key": comp.instrumentation_key,
        "connection_string": comp.connection_string,
        "workspace_resource_id": comp.workspace_resource_id,
        "provisioning_state": comp.provisioning_state,
    }


def find_application_insights_by_name(
    credential: TokenCredential,
    subscription_id: str,
    component_name: str,
) -> list[dict[str, Any]]:
    """Find App Insights components with this name across the subscription."""
    _validate_name(component_name, "component_name")
    client = _app_insights_client(credential, subscription_id)
    matches: list[dict[str, Any]] = []
    for comp in client.components.list():
        if str(getattr(comp, "name", "") or "").lower() == component_name.lower():
            matches.append(_component_snapshot(comp))
    return matches


def ensure_application_insights(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    component_name: str,
    region: str,
    workspace_resource_id: str,
) -> dict[str, Any]:
    """Create a workspace-based AI component when missing; return either way.

    The component is created with ``application_type='web'`` (suitable for both
    the SPA and the backend API). ``workspace_resource_id`` is required because
    classic (non-workspace) Application Insights is deprecated and Azure now
    refuses to create new classic components in most regions.
    """
    existing = get_application_insights(
        credential, subscription_id, resource_group, component_name
    )
    if existing is not None:
        return existing

    client = _app_insights_client(credential, subscription_id)
    comp = client.components.create_or_update(
        resource_group,
        component_name,
        {
            "location": region,
            "kind": "web",
            "application_type": "web",
            "workspace_resource_id": workspace_resource_id,
            "ingestion_mode": "LogAnalytics",
            "tags": {"managed-by": "elb-dashboard", "role": "observability"},
        },
    )
    return {
        "id": comp.id,
        "name": comp.name,
        "location": comp.location,
        "kind": comp.kind,
        "application_id": comp.app_id,
        "instrumentation_key": comp.instrumentation_key,
        "connection_string": comp.connection_string,
        "workspace_resource_id": comp.workspace_resource_id,
        "provisioning_state": comp.provisioning_state,
    }

"""AKS lifecycle Celery tasks (`start_aks` / `stop_aks` / `delete_aks`).

Responsibility: Drive the AKS managed-cluster lifecycle ARM operations and, on start,
    enqueue any follow-on side effects (Auto warm reconcile, OpenAPI deploy) that the
    SPA asked for when the user pressed Start.
Edit boundaries: Lifecycle calls and follow-on enqueues only. Provision-time concerns
    (pool layout, runtime RBAC) live in `provision.py` / `rbac.py`.
Key entry points: `start_aks`, `stop_aks`, `delete_aks` (Celery tasks
    `api.tasks.azure.{start,stop,delete}_aks`).
Risky contracts: Task names referenced by routes and tests (`test_warmup_route`
    monkeypatches `api.tasks.azure.start_aks.delay` and
    `api.tasks.azure.assign_aks_roles.delay`). Follow-on enqueues must remain
    non-fatal — a failed reconcile/deploy enqueue must not roll back AKS start.
Validation: `uv run pytest -q api/tests/test_azure_tasks.py
    api/tests/test_warmup_route.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

import api.tasks.azure as _facade

LOGGER = logging.getLogger(__name__)


@shared_task(
    name="api.tasks.azure.start_aks",
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def start_aks(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    auto_warmup: dict[str, Any] | None = None,
    auto_openapi: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a stopped AKS cluster.

    Side effects: starts the AKS control plane/node pools. When an Auto warm
    preference is supplied, persists it and queues storage warmup reconciliation
    after AKS start completes so the browser can be refreshed safely. When an
    OpenAPI deployment preference is supplied, queues the idempotent OpenAPI
    service deployment after the cluster is reachable.
    """
    cred = _facade.get_credential()
    aks = _facade.aks_client(cred, subscription_id)
    poller = aks.managed_clusters.begin_start(resource_group, cluster_name)
    poller.result()
    LOGGER.info("AKS cluster %s started", cluster_name)
    auto_warmup_task_id = ""
    if auto_warmup:
        try:
            from api.celery_app import celery_app
            from api.services.auto_warmup import (
                normalise_preference,
                save_auto_warmup_preference,
            )

            pref_payload = {
                **auto_warmup,
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
            }
            pref = save_auto_warmup_preference(normalise_preference(pref_payload))
            task = celery_app.send_task(
                "api.tasks.storage.reconcile_auto_warmup",
                kwargs={"preference": pref.to_dict(), "force": True},
                queue="storage",
            )
            auto_warmup_task_id = task.id
        except Exception as exc:
            LOGGER.warning("auto warm reconcile enqueue failed after AKS start: %s", exc)
    openapi_task_id = ""
    if auto_openapi:
        try:
            from api.celery_app import celery_app

            openapi_payload = {
                **auto_openapi,
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
            }
            task = celery_app.send_task(
                "api.tasks.openapi.deploy_openapi_service",
                kwargs=openapi_payload,
                queue="azure",
            )
            openapi_task_id = task.id
        except Exception as exc:
            LOGGER.warning("openapi deploy enqueue failed after AKS start: %s", exc)
    return {
        "cluster_name": cluster_name,
        "action": "start",
        "status": "completed",
        "auto_warmup_task_id": auto_warmup_task_id,
        "openapi_task_id": openapi_task_id,
    }


@shared_task(
    name="api.tasks.azure.stop_aks",
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def stop_aks(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Stop a running AKS cluster."""
    cred = _facade.get_credential()
    aks = _facade.aks_client(cred, subscription_id)
    poller = aks.managed_clusters.begin_stop(resource_group, cluster_name)
    poller.result()
    LOGGER.info("AKS cluster %s stopped", cluster_name)
    return {"cluster_name": cluster_name, "action": "stop", "status": "completed"}


@shared_task(
    name="api.tasks.azure.delete_aks",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def delete_aks(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Delete an AKS cluster."""
    cred = _facade.get_credential()
    aks = _facade.aks_client(cred, subscription_id)
    poller = aks.managed_clusters.begin_delete(resource_group, cluster_name)
    poller.result()
    LOGGER.info("AKS cluster %s deleted", cluster_name)
    return {"cluster_name": cluster_name, "action": "delete", "status": "completed"}

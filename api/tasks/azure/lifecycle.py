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
    """Delete an AKS cluster, and the enclosing resource group if it becomes empty.

    Side effects: ARM `managed_clusters.begin_delete` (which also tears down the
    auto-managed `MC_*` node-infra RG). After the AKS LRO completes, the parent RG
    is inspected; if it (a) carries the `managed-by=elb-dashboard` tag we wrote
    at create time **and** (b) contains no remaining resources, it is deleted too
    so the dashboard does not accumulate empty `rg-elb-cluster`-style shells.
    A non-empty RG or an RG without our ownership tag is left untouched — that
    closes both the user-shared-RG case and the TOCTOU race where the RG was
    listed empty here but another caller created a resource between
    `list_by_resource_group` and `begin_delete`. RG cleanup failures are logged
    but do not fail the task — the AKS delete already succeeded.
    """
    cred = _facade.get_credential()
    aks = _facade.aks_client(cred, subscription_id)
    poller = aks.managed_clusters.begin_delete(resource_group, cluster_name)
    poller.result()
    LOGGER.info("AKS cluster %s deleted", cluster_name)

    rg_status = "retained"
    rg_remaining = -1
    try:
        rc = _facade.resource_client(cred, subscription_id)
        # Ownership gate first — never auto-delete an RG we did not
        # create. `provision_aks` tags newly-created RGs with
        # `managed-by: elb-dashboard`. RGs that pre-date this tagging
        # change (or that the user created by hand) are intentionally
        # left for the operator to clean up manually.
        rg_props = rc.resource_groups.get(resource_group)
        rg_tags = dict(getattr(rg_props, "tags", None) or {})
        owns_rg = (
            rg_tags.get("managed-by") == "elb-dashboard"
            or rg_tags.get("managedBy") == "elb-dashboard"
        )
        if not owns_rg:
            rg_status = "retained_not_owned"
            LOGGER.info(
                "Resource group %s retained (no managed-by=elb-dashboard tag; "
                "treating as user-owned)",
                resource_group,
            )
        else:
            remaining = [
                r.name for r in rc.resources.list_by_resource_group(resource_group)
            ]
            rg_remaining = len(remaining)
            if rg_remaining == 0:
                rc.resource_groups.begin_delete(resource_group).result()
                rg_status = "deleted"
                LOGGER.info(
                    "Resource group %s deleted (owned + empty after AKS removal)",
                    resource_group,
                )
            else:
                rg_status = "retained_not_empty"
                LOGGER.info(
                    "Resource group %s retained (%d resource(s) remain: %s)",
                    resource_group,
                    rg_remaining,
                    ", ".join(remaining[:5]) + (" ..." if rg_remaining > 5 else ""),
                )
    except Exception as exc:
        rg_status = "error"
        LOGGER.warning("RG cleanup check failed for %s: %s", resource_group, exc)

    return {
        "cluster_name": cluster_name,
        "action": "delete",
        "status": "completed",
        "resource_group": resource_group,
        "resource_group_status": rg_status,
        "resource_group_remaining": rg_remaining,
    }

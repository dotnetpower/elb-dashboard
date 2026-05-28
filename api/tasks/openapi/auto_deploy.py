"""Helpers to auto-enqueue `deploy_openapi_service` after AKS lifecycle events.

Responsibility: Build the kwargs payload for `api.tasks.openapi.deploy_openapi_service`
    from the platform env (PLATFORM_ACR_NAME / STORAGE_ACCOUNT_NAME / AZURE_TENANT_ID /
    AZURE_RESOURCE_GROUP) plus the AKS coordinates handed in by the lifecycle / provision
    task, and enqueue it on the `azure` queue. Keeps the policy ("always auto-deploy
    unless ELB_AUTO_OPENAPI_DEPLOY=false") in one place so `start_aks` and
    `provision_aks` cannot drift.
Edit boundaries: Wiring only. Never call kubectl or any Azure SDK here. New
    env knobs go in the constants block at the top.
Key entry points: `enqueue_openapi_deploy_after_aks_event`, `auto_deploy_enabled`,
    `build_auto_openapi_payload`.
Risky contracts: Must remain non-fatal — a failed enqueue logs a warning and
    returns `""` so AKS start / provision never rolls back on the OpenAPI side.
    Task name `api.tasks.openapi.deploy_openapi_service` must not change without
    updating `start_aks` / `provision_aks` as well.
Validation: `uv run pytest -q api/tests/test_azure_tasks.py
    api/tests/test_warmup_route.py api/tests/test_openapi_auto_deploy.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

LOGGER = logging.getLogger(__name__)

# Opt-out switch — set on the api/worker sidecar Container Apps revision
# when an operator deliberately wants to skip auto-deploy (e.g. they
# manage `elb-openapi` rollouts via their own GitOps pipeline). The
# default is to ALWAYS auto-deploy, so a stopped AKS that the dashboard
# restarts comes back up with a working OpenAPI Service without
# requiring a separate "Deploy elb-openapi" click.
AUTO_DEPLOY_ENV = "ELB_AUTO_OPENAPI_DEPLOY"


def auto_deploy_enabled() -> bool:
    """Return False only when the opt-out env is set to a falsey value."""
    value = (os.environ.get(AUTO_DEPLOY_ENV, "") or "").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    return True


def build_auto_openapi_payload(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    tenant_id: str = "",
    caller_oid: str = "",
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build the `deploy_openapi_service` kwargs from env + AKS coordinates.

    Returns ``None`` when required platform values are missing — typically
    when the api sidecar is running locally without `PLATFORM_ACR_NAME`
    set, in which case we cannot construct an image reference and should
    not enqueue. Caller-supplied ``overrides`` win over env (used when
    the SPA's Start panel explicitly forwarded an `auto_openapi`
    payload).
    """
    overrides = overrides or {}
    acr_name = (
        overrides.get("acr_name")
        or os.environ.get("PLATFORM_ACR_NAME", "").strip()
    )
    if not acr_name:
        LOGGER.info(
            "auto OpenAPI deploy skipped: PLATFORM_ACR_NAME not set and "
            "no acr_name override supplied"
        )
        return None
    storage_account = (
        overrides.get("storage_account")
        or os.environ.get("AZURE_STORAGE_ACCOUNT", "").strip()
        or os.environ.get("STORAGE_ACCOUNT_NAME", "").strip()
    )
    storage_rg = (
        overrides.get("storage_resource_group")
        or os.environ.get("AZURE_RESOURCE_GROUP", "").strip()
    )
    acr_rg = (
        overrides.get("acr_resource_group")
        or os.environ.get("PLATFORM_ACR_RESOURCE_GROUP", "").strip()
        or os.environ.get("AZURE_RESOURCE_GROUP", "").strip()
    )
    return {
        "subscription_id": subscription_id,
        "resource_group": resource_group,
        "cluster_name": cluster_name,
        "acr_name": acr_name,
        "acr_resource_group": acr_rg,
        "storage_account": storage_account,
        "storage_resource_group": storage_rg,
        "tenant_id": tenant_id or os.environ.get("AZURE_TENANT_ID", "").strip(),
        "caller_oid": caller_oid,
    }


def enqueue_openapi_deploy_after_aks_event(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    tenant_id: str = "",
    caller_oid: str = "",
    overrides: dict[str, Any] | None = None,
    trigger: str = "aks_start",
) -> str:
    """Enqueue `deploy_openapi_service` on the `azure` queue. Returns task id or "".

    Best-effort: any failure (Celery broker unreachable, missing env, etc.)
    is logged and swallowed so the calling lifecycle task can still report
    success. Always returns a string (never raises) so callers can record
    the task id in their own result payload without an extra try/except.
    """
    if not auto_deploy_enabled():
        LOGGER.info("auto OpenAPI deploy disabled via %s=false", AUTO_DEPLOY_ENV)
        return ""
    payload = build_auto_openapi_payload(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        tenant_id=tenant_id,
        caller_oid=caller_oid,
        overrides=overrides,
    )
    if payload is None:
        return ""
    try:
        from api.celery_app import celery_app

        task = celery_app.send_task(
            "api.tasks.openapi.deploy_openapi_service",
            kwargs=payload,
            queue="azure",
        )
        LOGGER.info(
            "auto OpenAPI deploy enqueued task=%s trigger=%s cluster=%s",
            task.id,
            trigger,
            cluster_name,
        )
        return str(task.id or "")
    except Exception as exc:  # pragma: no cover - exercised via lifecycle tests with patched celery
        LOGGER.warning(
            "auto OpenAPI deploy enqueue failed (trigger=%s cluster=%s): %s",
            trigger,
            cluster_name,
            exc,
        )
        return ""


__all__ = [
    "AUTO_DEPLOY_ENV",
    "auto_deploy_enabled",
    "build_auto_openapi_payload",
    "enqueue_openapi_deploy_after_aks_event",
]

"""Orchestrate a single-action ``elb-openapi`` rebuild + redeploy.

Responsibility: Chain the charter rollout order (build the pinned
    ``elb-openapi`` image in ACR FIRST, wait for it to succeed, THEN redeploy it
    to AKS) into one Celery task so the SPA exposes a single "Rebuild & Deploy"
    action. The image tag is the single source of truth
    ``IMAGE_TAGS["elb-openapi"]`` — this task never invents a tag. Deploy is
    chained as a SEPARATE ``deploy_openapi_service`` task (so its progress is
    tracked by the existing deploy status route); this task returns the
    ``deploy_task_id`` once the build has succeeded.
Edit boundaries: Orchestration + progress checkpoints only. The ACR build
    scheduling reuses ``api.tasks.acr._schedule_acr_build``; the deploy reuses
    the existing ``api.tasks.openapi.deploy_openapi_service`` task by name. Do
    not duplicate ACR-build or kubectl logic here.
Key entry points: ``rebuild_and_redeploy_openapi`` (Celery task
    ``api.tasks.openapi.rebuild_and_redeploy``), plus the monkeypatch-friendly
    module helpers ``_schedule_openapi_build``, ``_poll_acr_build``,
    ``_enqueue_openapi_deploy``.
Risky contracts: The build-success gate is load-bearing — deploy is enqueued
    ONLY when the ACR run reaches ``Succeeded``. A failed / timed-out build
    returns a terminal ``status="failed"`` payload and never deploys, so a
    broken image can never replace the live revision. The poll loop is bounded
    by ``_BUILD_POLL_MAX_SECONDS`` (< the task soft limit) so it cannot spin
    forever. ``dry_run`` performs NO side effects (no build, no deploy) — it is
    the safe live-probe path. Task name
    ``api.tasks.openapi.rebuild_and_redeploy`` is referenced by the route and
    the SPA; do not rename it.
Validation: ``uv run pytest -q api/tests/test_openapi_rebuild.py``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from celery import shared_task

from api.services import get_credential
from api.services.image_tags import IMAGE_BUILD_INFO, IMAGE_TAGS
from api.tasks.openapi.helpers import record_progress

LOGGER = logging.getLogger(__name__)

_IMAGE_NAME = "elb-openapi"
# ACR management api-version that still exposes the build run surface
# (``runs.get`` + ``begin_schedule_run``). Mirrors api/services/monitoring/acr.py.
_ACR_RUN_API_VERSION = "2019-06-01-preview"

# Build poll bound. ACR builds for elb-openapi take ~3 min (observed Run "deku"
# 2m56s); 30 min is a generous ceiling that still terminates a wedged build.
_BUILD_POLL_MAX_SECONDS = int(
    os.environ.get("OPENAPI_REBUILD_BUILD_POLL_MAX_SECONDS", str(30 * 60))
)
_BUILD_POLL_INTERVAL_SECONDS = int(
    os.environ.get("OPENAPI_REBUILD_BUILD_POLL_INTERVAL_SECONDS", "15")
)

# Per-task Celery limits sit ABOVE the poll ceiling so the worker is never
# SIGKILLed mid-poll (which would leave a built-but-undeployed image). Keep
# soft < hard, both > the poll max.
_TASK_SOFT_TIME_LIMIT = int(
    os.environ.get("OPENAPI_REBUILD_TASK_SOFT_TIME_LIMIT", str(_BUILD_POLL_MAX_SECONDS + 5 * 60))
)
_TASK_HARD_TIME_LIMIT = int(
    os.environ.get("OPENAPI_REBUILD_TASK_TIME_LIMIT", str(_BUILD_POLL_MAX_SECONDS + 10 * 60))
)
if _TASK_SOFT_TIME_LIMIT >= _TASK_HARD_TIME_LIMIT:
    raise ValueError(
        "OPENAPI_REBUILD_TASK_SOFT_TIME_LIMIT must be < OPENAPI_REBUILD_TASK_TIME_LIMIT"
    )
if _TASK_HARD_TIME_LIMIT <= _BUILD_POLL_MAX_SECONDS:
    raise ValueError(
        "OPENAPI_REBUILD_TASK_TIME_LIMIT must exceed OPENAPI_REBUILD_BUILD_POLL_MAX_SECONDS"
    )

# ACR run terminal statuses (besides ``Succeeded``). Includes both US/Intl
# spellings of cancelled so a future SDK change cannot silently treat it as
# "still building" and hang the poll until the deadline.
_BUILD_TERMINAL_FAIL = frozenset({"Failed", "Canceled", "Cancelled", "Error", "Timeout"})
# Sentinel returned by ``_poll_acr_build`` when the deadline elapses before the
# run reaches any terminal status.
_BUILD_TIMEOUT = "__timeout__"


def _schedule_openapi_build(
    subscription_id: str, registry_resource_group: str, registry_name: str
) -> str:
    """Schedule the pinned elb-openapi ACR build run. Returns run_id or "".

    Reuses ``api.tasks.acr._schedule_acr_build`` so the build context / working
    directory / Dockerfile resolution stay identical to the ACR card's
    "Build images" action (single source of truth ``IMAGE_BUILD_INFO``).
    """
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient

    from api.tasks.acr import _BUILD_API_VERSION, _schedule_acr_build

    cred = get_credential()
    mgmt = ContainerRegistryManagementClient(cred, subscription_id, api_version=_BUILD_API_VERSION)
    tag = IMAGE_TAGS[_IMAGE_NAME]
    return (
        _schedule_acr_build(
            mgmt,
            registry_resource_group,
            registry_name,
            _IMAGE_NAME,
            tag,
            IMAGE_BUILD_INFO[_IMAGE_NAME],
        )
        or ""
    )


def _poll_acr_build(
    subscription_id: str,
    registry_resource_group: str,
    registry_name: str,
    run_id: str,
    *,
    deadline_seconds: int = _BUILD_POLL_MAX_SECONDS,
    interval_seconds: int = _BUILD_POLL_INTERVAL_SECONDS,
) -> str:
    """Poll an ACR run until it reaches a terminal status or the deadline.

    Returns the terminal status string (``"Succeeded"`` / ``"Failed"`` / …) or
    the ``_BUILD_TIMEOUT`` sentinel. A transient ``runs.get`` error is treated
    as "still building" (keep polling within the deadline) rather than a build
    failure, so an apiserver hiccup does not abort a healthy build.
    """
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient

    cred = get_credential()
    client = ContainerRegistryManagementClient(
        cred, subscription_id, api_version=_ACR_RUN_API_VERSION
    )
    deadline = time.monotonic() + max(1, deadline_seconds)
    while time.monotonic() < deadline:
        try:
            run = client.runs.get(registry_resource_group, registry_name, run_id)
            status = (run.status or "").strip()
        except Exception as exc:  # transient — keep polling within the deadline
            LOGGER.debug("acr runs.get transient failure run_id=%s: %s", run_id, type(exc).__name__)
            status = ""
        if status == "Succeeded":
            return "Succeeded"
        if status in _BUILD_TERMINAL_FAIL:
            return status
        time.sleep(max(1, interval_seconds))
    return _BUILD_TIMEOUT


def _enqueue_openapi_deploy(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    acr_name: str,
    acr_resource_group: str,
    storage_account: str,
    storage_resource_group: str,
    tenant_id: str,
    caller_oid: str,
    confirm_recreate: bool,
) -> str:
    """Enqueue the existing ``deploy_openapi_service`` task. Returns task id or "".

    Chained as a separate task (not run inline) so the deploy's own progress is
    tracked by the existing ``GET /aks/openapi/deploy/{id}/status`` route — the
    SPA switches to polling that route once this orchestrator reports the build
    succeeded.
    """
    try:
        from api.celery_app import celery_app

        task = celery_app.send_task(
            "api.tasks.openapi.deploy_openapi_service",
            kwargs={
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
                "acr_name": acr_name,
                "acr_resource_group": acr_resource_group,
                "storage_account": storage_account,
                "storage_resource_group": storage_resource_group,
                "tenant_id": tenant_id,
                "caller_oid": caller_oid,
                "confirm_recreate": confirm_recreate,
            },
            queue="azure",
        )
        return str(task.id or "")
    except Exception as exc:  # pragma: no cover - exercised via patched celery in tests
        LOGGER.warning("openapi deploy enqueue failed after build: %s", exc)
        return ""


@shared_task(
    name="api.tasks.openapi.rebuild_and_redeploy",
    bind=True,
    soft_time_limit=_TASK_SOFT_TIME_LIMIT,
    time_limit=_TASK_HARD_TIME_LIMIT,
)
def rebuild_and_redeploy_openapi(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    acr_name: str,
    acr_resource_group: str = "",
    storage_account: str = "",
    storage_resource_group: str = "",
    tenant_id: str = "",
    caller_oid: str = "",
    confirm_recreate: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build the pinned elb-openapi image then redeploy it (charter order).

    Side effects (in order): (1) schedule an ACR build of
    ``elb-openapi:<IMAGE_TAGS pin>``, (2) poll until it succeeds (bounded),
    (3) ONLY on success, enqueue ``deploy_openapi_service``. A failed or
    timed-out build returns a terminal ``status="failed"`` payload and never
    deploys, so a broken image can never replace the live revision.

    ``dry_run=True`` performs NO side effects — it returns the image ref +
    target coordinates it WOULD act on, for a safe live probe of the route.
    """
    image_tag = IMAGE_TAGS[_IMAGE_NAME]
    image_ref = f"{_IMAGE_NAME}:{image_tag}"
    # The ACR registry name is the ACR resource name; its RG defaults to the
    # cluster RG only when the caller did not supply the ACR's own RG (mirrors
    # the deploy route's precedence).
    registry_name = acr_name
    registry_resource_group = acr_resource_group or resource_group

    if dry_run:
        record_progress(self, "dry_run_preview", image=image_ref, registry=registry_name)
        return {
            "status": "dry_run",
            "image": image_ref,
            "registry_name": registry_name,
            "registry_resource_group": registry_resource_group,
            "cluster_name": cluster_name,
            "would_build": True,
            "would_deploy": True,
        }

    if not (registry_name and registry_resource_group and cluster_name):
        record_progress(self, "invalid_request", image=image_ref)
        return {
            "status": "failed",
            "stage": "validate",
            "error_code": "missing_parameters",
            "image": image_ref,
        }

    # 1. schedule build ----------------------------------------------------
    record_progress(self, "scheduling_build", image=image_ref, registry=registry_name)
    try:
        run_id = _schedule_openapi_build(subscription_id, registry_resource_group, registry_name)
    except Exception as exc:
        LOGGER.warning("openapi build schedule raised: %s", exc)
        run_id = ""
    if not run_id:
        record_progress(self, "build_schedule_failed", image=image_ref)
        return {
            "status": "failed",
            "stage": "build",
            "error_code": "build_schedule_failed",
            "image": image_ref,
        }

    # Best-effort "Building" hint for the ACR card (never fatal).
    try:
        from api.services.acr_build_state import record_pending_build

        record_pending_build(registry_name, run_id, _IMAGE_NAME, image_tag)
    except Exception as exc:
        LOGGER.debug("record_pending_build skipped run_id=%s: %s", run_id, type(exc).__name__)

    # 2. poll build until terminal (bounded) -------------------------------
    record_progress(self, "building", run_id=run_id, image=image_ref)
    build_status = _poll_acr_build(
        subscription_id, registry_resource_group, registry_name, run_id
    )
    if build_status != "Succeeded":
        error_code = (
            "build_timeout"
            if build_status == _BUILD_TIMEOUT
            else f"acr_build_{build_status.lower()}"
        )
        record_progress(self, "build_failed", run_id=run_id, build_status=build_status)
        return {
            "status": "failed",
            "stage": "build",
            "error_code": error_code,
            "build_run_id": run_id,
            "build_status": build_status,
            "image": image_ref,
        }

    record_progress(self, "build_succeeded", run_id=run_id, image=image_ref)

    # 3. deploy (chained) — ONLY reached on a succeeded build ---------------
    deploy_task_id = _enqueue_openapi_deploy(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        acr_name=acr_name,
        acr_resource_group=registry_resource_group,
        storage_account=storage_account,
        storage_resource_group=storage_resource_group,
        tenant_id=tenant_id,
        caller_oid=caller_oid,
        confirm_recreate=confirm_recreate,
    )
    if not deploy_task_id:
        record_progress(
            self, "deploy_enqueue_failed", run_id=run_id, image=image_ref
        )
        return {
            "status": "failed",
            "stage": "deploy",
            "error_code": "deploy_enqueue_failed",
            "build_run_id": run_id,
            "image": image_ref,
        }

    record_progress(
        self, "deploy_enqueued", run_id=run_id, image=image_ref, deploy_task_id=deploy_task_id
    )
    return {
        "status": "deploy_enqueued",
        "build_run_id": run_id,
        "image": image_ref,
        "deploy_task_id": deploy_task_id,
        "deploy_status_url": f"/api/aks/openapi/deploy/{deploy_task_id}/status",
    }

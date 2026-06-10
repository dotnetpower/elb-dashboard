"""`POST /api/aks/openapi/ensure-running` - wake-on-request readiness gate.

Responsibility: One route that an OpenAPI caller (or the dashboard) polls to
bring the AKS cluster that hosts ``elb-openapi`` up to a serving state. It maps
the cluster to a single phase via
`api.services.aks.ensure_running.evaluate_ensure_running` and, when the cluster
is fully stopped and auto-start is allowed, enqueues the existing ``start_aks``
task (forwarding the same ``auto_warmup`` / ``auto_openapi`` payloads as
``POST /api/aks/start`` so the restarted cluster re-warms and re-deploys
``elb-openapi``).
Edit boundaries: HTTP validation, auth, the start side effect, and response
shaping only. The phase decision lives in the service; the start task lives in
`api.tasks.azure.start_aks`. Do not call kubectl / azure.mgmt here.
Key entry points: `aks_openapi_ensure_running`.
Risky contracts: Every non-health `/api/*` route enforces `require_caller`. The
``status`` field is the polled external contract (see `ENSURE_RUNNING_STATUSES`).
A start is enqueued ONLY for a ``stopped`` cluster with ``start_recommended`` and
auto-start enabled, so the route cannot race an in-flight stop/start LRO or
silently rack up cluster-start cost on every poll.
Validation: `uv run pytest -q api/tests/test_aks_ensure_running.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Body, Depends, Response

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay
from api.routes.aks.common import _invalidate_aks_monitor_cache

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# Opt-out for the auto-start side effect. Default ON (the whole point of
# "ensure-running" is to bring the cluster up); an operator who wants the route
# to report state without ever spending start cost sets this to a falsey value.
_AUTO_START_ENV = "ENSURE_RUNNING_AUTO_START"


def _auto_start_enabled() -> bool:
    value = (os.environ.get(_AUTO_START_ENV, "") or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _build_auto_openapi(body: dict[str, Any], caller: CallerIdentity) -> dict[str, Any] | None:
    """Mirror `POST /api/aks/start`'s auto_openapi resolution.

    Lets the restarted cluster re-deploy ``elb-openapi`` without a separate
    click. Returns ``None`` when no ACR can be resolved (the start task then
    skips the OpenAPI redeploy, same as the lifecycle route).
    """
    auto_warmup = body.get("auto_warmup") if isinstance(body.get("auto_warmup"), dict) else None
    auto_openapi = body.get("auto_openapi") if isinstance(body.get("auto_openapi"), dict) else None
    if auto_openapi is None:
        source = auto_warmup if isinstance(auto_warmup, dict) else body
        if source.get("acr_name"):
            auto_openapi = {
                "acr_name": source.get("acr_name", ""),
                "acr_resource_group": source.get("acr_resource_group", ""),
                "storage_account": source.get("storage_account", ""),
                "storage_resource_group": source.get("storage_resource_group", ""),
            }
    if isinstance(auto_openapi, dict):
        resolved = {
            "acr_name": auto_openapi.get("acr_name", ""),
            "acr_resource_group": auto_openapi.get("acr_resource_group", ""),
            "storage_account": auto_openapi.get("storage_account", ""),
            "storage_resource_group": auto_openapi.get("storage_resource_group", ""),
            "tenant_id": auto_openapi.get("tenant_id") or caller.tenant_id,
            "caller_oid": auto_openapi.get("caller_oid") or caller.object_id,
        }
        return resolved if resolved["acr_name"] else None
    return None


@router.post("/openapi/ensure-running")
def aks_openapi_ensure_running(
    response: Response,
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Report the cluster's serving phase and (when stopped) start it.

    Body: ``resource_group`` and ``cluster_name`` are required;
    ``subscription_id`` defaults to ``AZURE_SUBSCRIPTION_ID``. Pass
    ``start=false`` to observe without triggering a start. Optional
    ``auto_warmup`` / ``auto_openapi`` mirror ``POST /api/aks/start`` and are
    only used when a start is actually enqueued.

    Returns HTTP 200 with a ``status`` of ``not_found`` / ``stopped`` /
    ``starting`` / ``warming`` / ``ready`` / ``unknown`` so a caller can poll the
    same endpoint until ``ready``. Sets ``Retry-After`` while not ready.
    """
    from api.services import get_credential
    from api.services.aks.ensure_running import evaluate_ensure_running

    subscription_id = (
        str(body.get("subscription_id") or "").strip()
        or os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    )
    resource_group = str(body.get("resource_group") or "").strip()
    cluster_name = str(body.get("cluster_name") or "").strip()
    if not (subscription_id and resource_group and cluster_name):
        response.status_code = 400
        return {
            "status": "error",
            "code": "missing_parameters",
            "message": (
                "subscription_id (or AZURE_SUBSCRIPTION_ID env), resource_group "
                "and cluster_name are required."
            ),
        }

    want_start = bool(body.get("start", True))
    credential = get_credential()
    result = evaluate_ensure_running(
        credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )

    start_triggered = False
    start_task_id: str | None = None
    if (
        result["status"] == "stopped"
        and result["start_recommended"]
        and want_start
        and _auto_start_enabled()
    ):
        from api.tasks.azure import start_aks

        auto_warmup = (
            body.get("auto_warmup") if isinstance(body.get("auto_warmup"), dict) else None
        )
        auto_openapi = _build_auto_openapi(body, caller)
        async_result = _safe_delay(
            start_aks,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            auto_warmup=auto_warmup,
            auto_openapi=auto_openapi,
        )
        start_triggered = True
        start_task_id = getattr(async_result, "id", None)
        _invalidate_aks_monitor_cache(subscription_id, resource_group)
        LOGGER.info(
            "ensure-running enqueued start for cluster=%s rg=%s task=%s",
            cluster_name,
            resource_group,
            start_task_id,
        )

    retry_after = result["retry_after_seconds"]
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)

    return {
        "status": result["status"],
        "cluster": {
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "name": cluster_name,
            "power_state": result["power_state"],
            "provisioning_state": result["provisioning_state"],
            "exists": result["exists"],
        },
        "start_triggered": start_triggered,
        "start_task_id": start_task_id,
        "warmup": result["warmup"],
        "retry_after_seconds": retry_after,
        "message": result["reason"],
    }

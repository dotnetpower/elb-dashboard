"""AKS-hosted OpenAPI deployment, spec, and proxy routes.

Responsibility: AKS-hosted OpenAPI deployment, spec, and proxy routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `aks_openapi_deploy`, `aks_openapi_deploy_status`,
`aks_openapi_deployment`, `aks_openapi_token`, `aks_openapi_token_generate`,
`aks_openapi_spec`, `_reject_dashboard_uuid_job_path`, `aks_openapi_proxy`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py
api/tests/test_openapi_proxy_route.py api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _OPENAPI_PROXY_ALLOWED_HEADERS, _safe_delay

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_DASHBOARD_UUID_JOB_PATH_RE = re.compile(
    r"^/v1/jobs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:/|\?|$)",
    re.IGNORECASE,
)


class OpenApiTokenRequest(BaseModel):
    subscription_id: str = ""
    resource_group: str
    cluster_name: str
    regenerate: bool = False


def _raise_openapi_route_error(exc: Exception) -> None:
    from api.services.openapi_deployment import OpenApiDeploymentError
    from api.services.openapi_token import OpenApiTokenError

    if isinstance(exc, (OpenApiTokenError, OpenApiDeploymentError)):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    raise HTTPException(
        status_code=502,
        detail={
            "code": "openapi_token_unavailable",
            "message": "The OpenAPI API token could not be read or updated.",
        },
    ) from exc


def _reject_dashboard_uuid_job_path(target_path: str) -> None:
    if not _DASHBOARD_UUID_JOB_PATH_RE.match(target_path):
        return
    raise HTTPException(
        status_code=400,
        detail={
            "code": "dashboard_job_id_not_openapi_job_id",
            "message": (
                "OpenAPI job endpoints expect the short job_id returned by POST /v1/jobs. "
                "Dashboard job UUIDs are local control-plane IDs; use "
                "/api/blast/jobs/{job_id} or the Jobs page for those IDs."
            ),
        },
    )


@router.post("/openapi/deploy")
def aks_openapi_deploy(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Re-deploy ``elb-openapi`` to an existing AKS cluster.

    Translates the SPA's ``OpenApiDeployPanel`` body into a Celery task
    enqueue. The returned ``id`` is the Celery task id so the SPA can poll
    ``GET /aks/openapi/deploy/{id}/status`` directly.
    """

    from api.tasks.openapi import deploy_openapi_service

    rg = body.get("resource_group", "") or ""
    cluster_name = body.get("cluster_name", "") or ""
    acr_name = body.get("acr_name", "") or ""
    if not (rg and cluster_name and acr_name):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_parameters",
                "message": (
                    "resource_group, cluster_name and acr_name are required "
                    "to deploy the OpenAPI service."
                ),
            },
        )

    result = _safe_delay(
        deploy_openapi_service,
        subscription_id=body.get("subscription_id", "") or "",
        resource_group=rg,
        cluster_name=cluster_name,
        acr_name=acr_name,
        storage_account=body.get("storage_account", "") or "",
        storage_resource_group=body.get("storage_resource_group", "") or "",
        tenant_id=caller.tenant_id or "",
        caller_oid=caller.object_id or "",
    )
    return {
        "id": result.id,
        "instance_id": result.id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/aks/openapi/deploy/{result.id}/status",
        "status": "queued",
    }


@router.get("/openapi/deploy/{instance_id}/status")
def aks_openapi_deploy_status(
    instance_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Translate the Celery ``AsyncResult`` for a deploy_openapi task into
    the orchestrator-style envelope (``runtime_status`` + ``custom_status``
    + ``output``) the SPA's ``OpenApiDeployPanel`` was originally written
    against.
    """

    from celery.result import AsyncResult

    from api.celery_app import celery_app

    result = AsyncResult(instance_id, app=celery_app)
    status = (result.status or "PENDING").upper()
    runtime_status = {
        "PENDING": "Pending",
        "RECEIVED": "Pending",
        "STARTED": "Running",
        "RETRY": "Running",
        "PROGRESS": "Running",
        "SUCCESS": "Completed",
        "FAILURE": "Failed",
        "REVOKED": "Terminated",
    }.get(status, "Running")

    custom_status: dict[str, Any] = {"phase": status.lower()}
    output: dict[str, Any] | None = None

    if not result.ready():
        info = result.info if isinstance(result.info, dict) else None
        if info:
            custom_status.update({k: v for k, v in info.items() if k != "exc_type"})
    elif result.successful():
        payload = result.result if isinstance(result.result, dict) else {}
        succeeded = str(payload.get("status", "")).lower() == "succeeded"
        custom_status.update({"phase": "completed"})
        # The SPA reads ``output.openapi_deploy.error`` and
        # ``output.workload_identity.error`` to surface failures, so pass
        # the whole task payload through as ``output``.
        output = dict(payload)
        if not succeeded:
            output.setdefault("status", "failed")
    else:
        # FAILURE / REVOKED
        err = ""
        try:
            err = str(result.result or result.info or "")[:500]
        except Exception:
            err = "task failed"
        custom_status.update({"phase": "failed"})
        output = {
            "status": "failed",
            "openapi_deploy": {"error": err},
        }

    return {
        "instance_id": instance_id,
        "runtime_status": runtime_status,
        "custom_status": custom_status,
        "output": output,
    }


@router.get("/openapi/deployment")
def aks_openapi_deployment(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the deployed ``elb-openapi`` image tag from the AKS deployment."""

    from api.services import get_credential
    from api.services.openapi_deployment import get_openapi_deployment_status

    del caller
    try:
        return get_openapi_deployment_status(
            get_credential(),
            subscription_id=subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    except Exception as exc:
        _raise_openapi_route_error(exc)
        raise


@router.get("/openapi/token")
def aks_openapi_token(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the current API token configured on the ``elb-openapi`` deployment."""

    from api.services import get_credential
    from api.services.openapi_token import get_openapi_api_token_status

    del caller
    try:
        return get_openapi_api_token_status(
            get_credential(),
            subscription_id=subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    except Exception as exc:
        _raise_openapi_route_error(exc)
        raise


@router.post("/openapi/token")
def aks_openapi_token_generate(
    body: OpenApiTokenRequest,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Generate or rotate the API token on the ``elb-openapi`` deployment."""

    from api.services import get_credential
    from api.services.openapi_token import ensure_openapi_api_token

    LOGGER.info(
        "openapi token update requested cluster=%s caller_oid=%s regenerate=%s",
        body.cluster_name,
        caller.object_id,
        body.regenerate,
    )
    try:
        return ensure_openapi_api_token(
            get_credential(),
            subscription_id=body.subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
            resource_group=body.resource_group,
            cluster_name=body.cluster_name,
            regenerate=body.regenerate,
        )
    except Exception as exc:
        _raise_openapi_route_error(exc)
        raise


@router.get("/openapi/spec")
def aks_openapi_spec(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Best-effort proxy for the deployed OpenAPI service's ``/openapi.json``.

    Resolves the LoadBalancer IP via the K8s API, then fetches the spec.
    Returns a degraded ``openapi:"3.0.0"`` placeholder when the service is
    not yet reachable so the SPA's docs page does not crash.
    """

    import httpx

    from api.services import get_credential
    from api.services.k8s_monitoring import k8s_get_service_ip

    sub = subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", "")
    cred = get_credential()
    try:
        ip = k8s_get_service_ip(cred, sub, resource_group, cluster_name, "elb-openapi")
    except Exception as exc:
        ip = None
        LOGGER.warning("openapi/spec: k8s_get_service_ip failed: %s", exc)

    if not ip:
        return {
            "openapi": "3.0.0",
            "info": {"title": "elb-openapi (not yet deployed)", "version": "0.0.0"},
            "paths": {},
            "degraded": True,
            "degraded_reason": "openapi_service_not_reachable",
        }

    try:
        with httpx.Client(timeout=10.0) as client:
            for path in ("/openapi.json", "/docs/openapi.json"):
                resp = client.get(f"http://{ip}{path}")
                if resp.status_code == 200:
                    return cast(dict[str, Any], resp.json())
    except Exception as exc:
        LOGGER.warning("openapi/spec: fetch failed for %s: %s", ip, exc)

    return {
        "openapi": "3.0.0",
        "info": {"title": "elb-openapi (spec not available)", "version": "0.0.0"},
        "paths": {},
        "degraded": True,
        "degraded_reason": "openapi_endpoint_unreachable",
    }


@router.api_route(
    "/openapi/proxy",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def aks_openapi_proxy(
    request: Request,
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    target_path: str = Query(..., alias="path"),
    caller: CallerIdentity = Depends(require_caller),
) -> Response:
    """Proxy API Reference "Try it" calls to the deployed ``elb-openapi`` pod."""

    import httpx

    from api.services import get_credential
    from api.services.k8s_monitoring import k8s_get_service_ip

    if not target_path.startswith("/") or target_path.startswith("//"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_openapi_path",
                "message": "path must be an absolute OpenAPI service path",
            },
        )
    if "\r" in target_path or "\n" in target_path:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_openapi_path", "message": "path contains invalid characters"},
        )
    _reject_dashboard_uuid_job_path(target_path)

    sub = subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", "")
    cred = get_credential()
    try:
        ip = k8s_get_service_ip(cred, sub, resource_group, cluster_name, "elb-openapi")
    except Exception as exc:
        ip = None
        LOGGER.warning("openapi/proxy: k8s_get_service_ip failed: %s", exc)

    if not ip:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "openapi_service_not_reachable",
                "message": "The elb-openapi service is not reachable yet.",
                "retryable": True,
            },
        )

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _OPENAPI_PROXY_ALLOWED_HEADERS
    }
    api_token = os.environ.get("ELB_OPENAPI_API_TOKEN", "").strip()
    if not api_token:
        from api.services.openapi_runtime import get_openapi_api_token

        api_token = get_openapi_api_token()
    if not api_token:
        try:
            from api.services.openapi_token import get_openapi_api_token_status

            token_status = get_openapi_api_token_status(
                cred,
                subscription_id=sub,
                resource_group=resource_group,
                cluster_name=cluster_name,
            )
            api_token = str(token_status.get("token") or "").strip()
        except Exception as exc:
            LOGGER.debug("openapi/proxy: API token lookup skipped: %s", type(exc).__name__)
    if api_token:
        headers["X-ELB-API-Token"] = api_token
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                f"http://{ip}{target_path}",
                headers=headers,
                content=body if body else None,
            )
    except httpx.RequestError as exc:
        LOGGER.warning("openapi/proxy: upstream request failed for %s: %s", ip, exc)
        raise HTTPException(
            status_code=502,
            detail={
                "code": "openapi_upstream_unreachable",
                "message": "The elb-openapi endpoint did not respond.",
                "retryable": True,
            },
        ) from exc

    response_headers: dict[str, str] = {}
    content_type = upstream.headers.get("content-type")
    if content_type:
        response_headers["content-type"] = content_type
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )

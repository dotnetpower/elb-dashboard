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
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _OPENAPI_PROXY_ALLOWED_HEADERS, _safe_delay

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_DASHBOARD_UUID_JOB_PATH_RE = re.compile(
    r"^/v1/jobs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:/|\?|$)",
    re.IGNORECASE,
)

# Allowlist of path prefixes the dashboard's API Reference "Try it" surface
# legitimately calls on the deployed elb-openapi service. The proxy
# auto-injects the admin X-ELB-API-Token (see aks_openapi_proxy below),
# so without this allowlist any authenticated tenant member can ride that
# admin token into /admin/*, /internal/*, /debug/* on the upstream service.
# Match rules: an entry ending in '/' is a prefix; an entry without '/' is
# an exact path. Comparison is case-insensitive so an ingress that lower-
# cases paths cannot launder a denied path through the gate. Keep this
# minimal — add new entries only when a new SPA Try-It call surface is
# genuinely needed.
_OPENAPI_PROXY_ALLOWED_PATH_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/openapi.json",
    "/docs/",
    "/v1/",
)

# Substrings that are always denied even if they appear inside an
# allowlisted prefix. Defence-in-depth against an upstream that exposes
# admin routes under /v1/admin/ or similar — the elb-openapi service
# should not, but this layer must not assume that.
_OPENAPI_PROXY_DENIED_PATH_TOKENS: tuple[str, ...] = (
    "/admin/",
    "/admin?",
    "/internal/",
    "/internal?",
    "/debug/",
    "/debug?",
)


def _is_private_ipv4(value: str) -> bool:
    """Return True if ``value`` parses as a private / loopback / link-local IPv4.

    Used by the OpenAPI proxy to decide whether it is safe to send the
    admin ``X-ELB-API-Token`` over plain HTTP. The api sidecar and the
    elb-openapi pod normally live in the same AKS VNet, so the resolved
    Service IP is RFC1918. A public IP (operator wired the Service as
    ``LoadBalancer`` with a public LB and no TLS) would expose the admin
    token to the path between the api sidecar and the LB; refuse to
    inject the token in that case.

    Limited to IPv4 today because the existing upstream URL construction
    in the proxy / spec routes (``f"http://{ip}..."``) does not bracket
    IPv6 addresses and httpx therefore cannot parse them. Adding IPv6
    support requires both this private-range check AND fixing every
    ``f"http://{ip}..."`` call site to bracket IPv6 literals. Tracked as
    a follow-up; meanwhile this function returns False for IPv6, which
    causes the proxy to refuse — the conservative default.
    """
    import ipaddress

    try:
        ip = ipaddress.IPv4Address(value)
    except (ValueError, ipaddress.AddressValueError):
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _public_lb_allowed() -> bool:
    """Return True when the operator has explicitly opted in to forwarding
    the admin ``X-ELB-API-Token`` to a non-private (public) LB IP.

    Set ``OPENAPI_ALLOW_PUBLIC_LB=true`` on the api sidecar to enable.
    Accepted truthy values: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    The default is False — the conservative posture from security audit #12
    (2026-05-22).
    """
    return os.getenv("OPENAPI_ALLOW_PUBLIC_LB", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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


def _enforce_openapi_proxy_target_path(target_path: str) -> None:
    """Reject paths outside the public OpenAPI Try-It surface.

    The proxy auto-injects the admin ``X-ELB-API-Token`` on every call,
    so without this gate an authenticated tenant member could pass
    ``?path=/admin/...`` and ride that token into admin-only endpoints
    on the elb-openapi service. The allowlist mirrors what the SPA's
    API Reference "Try it" feature legitimately calls. Path traversal
    segments (``..``; URL-encoded variants such as ``%2e%2e`` and
    double-encoded ``%252e%252e``) are rejected regardless of prefix,
    and a small deny-list of admin / internal / debug substrings is
    enforced as defence-in-depth against an upstream that exposes
    sensitive routes inside an allowlisted prefix.
    """
    import urllib.parse

    path_only = target_path.split("?", 1)[0]
    # Iteratively percent-decode until the value stops changing so a
    # double-encoded segment (``%252e%252e`` → ``%2e%2e`` → ``..``) cannot
    # slip past a single-pass decode. Bounded loop guards against an
    # adversarial input that would otherwise stretch the decode chain
    # indefinitely.
    decoded = path_only
    for _ in range(5):
        next_decoded = urllib.parse.unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    # NUL bytes and other C0 control characters can truncate the path at
    # the upstream (C-string handling) so the allowlist check would see
    # a different value than the upstream router. Reject them outright.
    if any(ord(ch) < 0x20 for ch in decoded):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_openapi_path",
                "message": "path contains control characters",
            },
        )
    if ".." in decoded:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "openapi_path_traversal_denied",
                "message": "path contains '..' which is not allowed",
            },
        )
    # Case-insensitive comparison so an ingress that lower-cases the
    # path cannot launder a denied path through this gate. The allowlist
    # entries are already lowercase by construction.
    lowered = decoded.lower()
    # Re-include any query string for the deny-token check so
    # ``/v1/admin?x=1`` is caught alongside ``/v1/admin/x``.
    lowered_full = lowered + target_path[len(path_only) :].lower()
    for denied in _OPENAPI_PROXY_DENIED_PATH_TOKENS:
        if denied in lowered_full:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "openapi_path_not_allowlisted",
                    "message": f"path contains denied segment '{denied.rstrip('?/')}'",
                },
            )
    for prefix in _OPENAPI_PROXY_ALLOWED_PATH_PREFIXES:
        prefix_root = prefix.rstrip("/")
        if lowered == prefix or lowered == prefix_root or lowered.startswith(prefix):
            return
    raise HTTPException(
        status_code=400,
        detail={
            "code": "openapi_path_not_allowlisted",
            "message": (
                "path must start with one of: "
                + ", ".join(_OPENAPI_PROXY_ALLOWED_PATH_PREFIXES)
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
        from api.services.httpx_pool import get_pooled_client

        client = get_pooled_client("aks-openapi-spec", timeout=10.0)
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
    _enforce_openapi_proxy_target_path(target_path)
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

    # Refuse to forward when the resolved Service IP is *not* private:
    # the api sidecar would otherwise send the admin ``X-ELB-API-Token``
    # over plain HTTP to a public LoadBalancer, exposing the token to
    # any MITM between the sidecar and the LB. The legitimate deployment
    # exposes elb-openapi inside the AKS VNet (Service type LoadBalancer
    # with internal annotation, or ClusterIP), so the IP is RFC1918.
    # Operators that intentionally run elb-openapi behind a public LB
    # (and accept the plain-HTTP-to-public-IP exposure) can opt in by
    # setting ``OPENAPI_ALLOW_PUBLIC_LB=true`` on the api sidecar.
    if not _is_private_ipv4(ip) and not _public_lb_allowed():
        LOGGER.warning(
            "openapi/proxy: refusing to forward admin token to non-private IP %s "
            "(use an internal LoadBalancer, terminate TLS in front of the service, "
            "or set OPENAPI_ALLOW_PUBLIC_LB=true to opt in)",
            ip,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "openapi_unsafe_transport",
                "message": (
                    "Refusing to send the admin token to a non-private IP. "
                    "Expose elb-openapi via an internal LoadBalancer (RFC1918), "
                    "terminate TLS in front of the public endpoint, "
                    "or set OPENAPI_ALLOW_PUBLIC_LB=true to opt in."
                ),
            },
        )
    if not _is_private_ipv4(ip):
        LOGGER.warning(
            "openapi/proxy: forwarding admin token to non-private IP %s "
            "because OPENAPI_ALLOW_PUBLIC_LB is set; this exposes the token "
            "over plain HTTP between the api sidecar and the public LB",
            ip,
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
    # Streaming proxy: open the AsyncClient OUTSIDE the request scope so
    # we can attach its lifecycle to the streaming response generator.
    # Buffering the upstream response (the previous ``upstream.content``)
    # would force a full BLAST result file into the api sidecar's RAM
    # before the browser sees the first byte; large XML / gzip responses
    # multiplied through the api sidecar's memory under concurrent loads.
    client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        upstream_req = client.build_request(
            request.method,
            f"http://{ip}{target_path}",
            headers=headers,
            content=body if body else None,
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
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
    for header_name in ("content-type", "content-disposition"):
        value = upstream.headers.get(header_name)
        if value:
            response_headers[header_name] = value

    async def _body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _body_iter(),
        status_code=upstream.status_code,
        headers=response_headers,
    )

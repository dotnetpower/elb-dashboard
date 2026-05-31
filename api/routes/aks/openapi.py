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
from api.services.sanitise import redact_oid

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
# admin / internal / debug routes under /v1/admin/ or similar — the
# elb-openapi service should not, but this layer must not assume that.
# Variants with a trailing `-` catch dasherised siblings (``/admin-api/``,
# ``/internal-tools/``, ``/debug-info/``, ``/private-keys/``,
# ``/sudo-mode/``); `/private/` and `/sudo/` cover the common
# privileged-route naming conventions used by adjacent FastAPI services.
# Each privileged family carries the same three-way coverage (``/x/`` for
# segment, ``/x?`` for query-stripped exact, ``/x-`` for dashed sibling)
# so adding a new family is a single block edit.
_OPENAPI_PROXY_DENIED_PATH_TOKENS: tuple[str, ...] = (
    "/admin/",
    "/admin?",
    "/admin-",
    "/internal/",
    "/internal?",
    "/internal-",
    "/debug/",
    "/debug?",
    "/debug-",
    "/private/",
    "/private?",
    "/private-",
    "/sudo/",
    "/sudo?",
    "/sudo-",
)

_OPENAPI_K8S_NAMESPACE = "default"
_OPENAPI_SERVICE_NAME = "elb-openapi"
_OPENAPI_SERVICE_PORT = 80


def _peering_recovery_hint() -> dict[str, str]:
    """Generic recovery affordance for `elb-openapi` unreachable errors.

    The api sidecar lives in the dashboard platform VNet; the
    ``elb-openapi`` Service IP lives in the AKS auto-VNet. Without a
    bidirectional VNet peering the proxy / spec routes time out. The
    SPA reads ``recovery_action`` to render a "Repair VNet peering"
    button that POSTs to ``/api/aks/peer-with-platform`` (the same
    idempotent helper the AKS provision task runs at the end of cluster
    create). Returned fields are additive and stable.
    """
    return {
        "recovery_action": "peer_with_platform",
        "recovery_hint": (
            "The api sidecar cannot reach the elb-openapi Service IP. "
            "This is usually a missing VNet peering between the dashboard "
            "platform VNet and the AKS-auto VNet. Click 'Repair VNet peering' "
            "to run the idempotent recovery, or run "
            "`scripts/dev/peer-cluster-network.sh`."
        ),
    }


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


def _k8s_service_proxy_url(server: str, path: str) -> str:
    target = path.lstrip("/")
    return (
        f"{server}/api/v1/namespaces/{_OPENAPI_K8S_NAMESPACE}/services/"
        f"http:{_OPENAPI_SERVICE_NAME}:{_OPENAPI_SERVICE_PORT}/proxy/{target}"
    )


def _fetch_openapi_spec_via_k8s_proxy(
    cred: Any,
    sub: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any] | None:
    """Fetch the OpenAPI spec through the Kubernetes service proxy.

    This fallback keeps local development usable after `elb-openapi` moves to
    an internal LoadBalancer that only the deployed Container App VNet can
    route to directly.
    """

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(
        cred,
        sub,
        resource_group,
        cluster_name,
        admin=True,
    )
    try:
        for path in ("/openapi.json", "/docs/openapi.json"):
            resp = session.get(_k8s_service_proxy_url(server, path), timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, dict) else None
    finally:
        session.close()
    return None


class OpenApiTokenRequest(BaseModel):
    subscription_id: str = ""
    resource_group: str
    cluster_name: str
    regenerate: bool = False


def _raise_openapi_route_error(exc: Exception) -> None:
    from api.services.openapi.deployment import OpenApiDeploymentError
    from api.services.openapi.token import OpenApiTokenError

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
        acr_resource_group=body.get("acr_resource_group", "") or "",
        storage_account=body.get("storage_account", "") or "",
        storage_resource_group=body.get("storage_resource_group", "") or "",
        tenant_id=caller.tenant_id or "",
        caller_oid=caller.object_id or "",
        confirm_recreate=bool(body.get("confirm_recreate", False)),
    )
    return {
        "id": result.id,
        "instance_id": result.id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/aks/openapi/deploy/{result.id}/status",
        "status": "queued",
    }


def _deploy_failure_is_upstream_reach(output: dict[str, Any] | None) -> bool:
    """Return True when a failed deploy payload looks like an upstream-reach
    (likely VNet peering) issue rather than image / scheduling / identity.

    Signals checked, in order of confidence:

    1. ``openapi_deploy.status == 'no_ready_replica'`` AND
       ``external_ip`` is empty — the LoadBalancer never got an IP, which
       on AKS-auto VNets is almost always a peering / NSG / outbound
       routing problem the ``/api/aks/peer-with-platform`` helper can fix.
    2. Diagnostic events mention "no endpoints available" — the Service
       exists but Kubernetes can't reach the pod's endpoints (the same
       symptom the proxy sees when peering breaks mid-flight).
    3. The error string contains canonical upstream-reach phrases
       (``unreachable``, ``timed out``, ``no route to host``, ``i/o timeout``)
       that the SPA otherwise would have to parse from free-form text.

    Returning True only injects an additive ``recovery_action`` /
    ``recovery_hint`` pair into the envelope — never changes the
    runtime_status, output, or other existing fields — so legacy SPA
    builds keep working.
    """
    if not isinstance(output, dict):
        return False
    deploy = output.get("openapi_deploy")
    if not isinstance(deploy, dict):
        return False
    if deploy.get("status") == "no_ready_replica":
        external_ip = str(deploy.get("external_ip") or "").strip()
        if not external_ip:
            return True
        diagnostics = deploy.get("diagnostics")
        if isinstance(diagnostics, dict):
            events = diagnostics.get("events") or []
            for event in events:
                if not isinstance(event, dict):
                    continue
                message = str(event.get("message") or "").lower()
                if "no endpoints available" in message:
                    return True
    error_text = str(deploy.get("error") or "").lower()
    if any(
        token in error_text
        for token in (
            "unreachable",
            "timed out",
            "no route to host",
            "i/o timeout",
            "connection refused",
        )
    ):
        return True
    return False


@router.get("/openapi/deploy/{instance_id}/status")
def aks_openapi_deploy_status(
    instance_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Translate the Celery ``AsyncResult`` for a deploy_openapi task into
    the orchestrator-style envelope (``runtime_status`` + ``custom_status``
    + ``output``) the SPA's ``OpenApiDeployPanel`` was originally written
    against.

    When a failed task looks like an upstream-reach (VNet peering)
    problem, the envelope additionally carries top-level
    ``recovery_action`` / ``recovery_hint`` keys so the SPA can render
    the "Repair VNet peering" affordance without parsing free-form
    error strings. See ``_deploy_failure_is_upstream_reach`` and the
    sibling ``_peering_recovery_hint``.
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

    envelope: dict[str, Any] = {
        "instance_id": instance_id,
        "runtime_status": runtime_status,
        "custom_status": custom_status,
        "output": output,
    }
    if (
        runtime_status in ("Failed", "Terminated", "Completed")
        and _deploy_failure_is_upstream_reach(output)
    ):
        envelope.update(_peering_recovery_hint())
    return envelope


@router.post("/openapi/deploy/{task_id}/cancel")
def aks_openapi_deploy_cancel(
    task_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Revoke a running ``deploy_openapi_service`` Celery task.

    Mirrors the contract of ``POST /api/aks/cancel-provision/{task_id}``
    so the SPA can share its toast / banner code: the response shape is
    identical (``task_id``, ``job_id``, ``previous_status``,
    ``was_running``, ``cancelled``, ``settle_after_seconds``) and the
    route is idempotent — calling on an already-terminal task returns
    200 with ``was_running=False`` and never re-invokes ``revoke()``.

    OpenAPI deploy does not currently write a ``JobState`` row (the task
    only emits Celery ``PROGRESS`` updates), so ``job_id`` is typically
    ``None`` and the ownership gate falls through to a no-op. That matches
    the upstream cancel-provision behaviour for orphan tasks
    (``test_cancel_passes_through_when_no_state_row``) and remains
    behind ``require_caller`` so anonymous browsers cannot reach it.
    """
    # Sibling import — `_enforce_task_ownership` is intentionally
    # duplicated across routes/tasks.py, routes/operations.py and
    # routes/aks/cancel.py; reusing the cancel.py copy avoids a fourth
    # near-identical block here while keeping the cancel surface honest.
    from api.routes.aks.cancel import _enforce_task_ownership

    _enforce_task_ownership(task_id, caller)

    from celery.result import AsyncResult

    from api.celery_app import celery_app
    from api.services.state_repo import JobStateRepository
    from api.tasks.azure.helpers import update_state

    result = AsyncResult(task_id, app=celery_app)
    status = str(result.status or "PENDING").upper()
    was_running = status in {"PENDING", "RECEIVED", "STARTED", "RETRY"}

    if was_running:
        try:
            # ``terminate=True`` with SIGTERM matches the cancel-provision
            # route. The deploy task spends most of its time inside K8s
            # API polling loops, so the worker honors the signal at the
            # next yield (typically <= 10 s — the ready-replica probe
            # interval is 5 s, the LB IP probe interval is 10 s).
            celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
        except Exception as exc:
            LOGGER.warning(
                "openapi deploy revoke failed task_id=%s err=%s",
                task_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=502,
                detail={"code": "revoke_failed", "retryable": True},
            ) from exc

    job_id: str | None = None
    try:
        state = JobStateRepository().find_by_task_id(task_id)
        if state is not None:
            job_id = state.job_id
            update_state(
                job_id,
                "cancelled_by_user",
                status="cancelled",
                error_code="cancelled_by_user",
            )
    except Exception as exc:
        LOGGER.debug("openapi deploy state update on cancel failed: %s", type(exc).__name__)

    return {
        "task_id": task_id,
        "job_id": job_id,
        "previous_status": status,
        "was_running": was_running,
        "cancelled": True,
        # The deploy task probe loop yields every ~5-10 s, so the worker
        # honors SIGTERM well within the upstream cancel-provision's
        # 20 s ARM-poll budget. Match that budget for SPA consistency.
        "settle_after_seconds": 10 if was_running else 0,
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
    from api.services.openapi.deployment import get_openapi_deployment_status

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


@router.get("/openapi/pls")
def aks_openapi_pls(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return live Private Link Service (PLS) annotation state for ``elb-openapi``.

    Pure-read endpoint. The SPA renders this card to show:
      * whether PLS is enabled in the deploy environment
        (``OPENAPI_PLS_ENABLED`` / ``OPENAPI_PLS_NAME`` / ``OPENAPI_PLS_LB_SUBNET``),
      * whether the live Kubernetes Service already carries the
        ``service.beta.kubernetes.io/azure-pls-create`` annotation set, and
      * whether a transition is pending (env says PLS, Service says no) plus
        whether the deploy task will require ``OPENAPI_PLS_CONFIRM_RECREATE=1``
        to proceed.

    Failure-by-design: cluster unreachable / RBAC missing / k8s session error
    all degrade to ``available=False`` with ``reason=<short_code>`` so the SPA
    can render an "unknown" cell instead of a hard error. The endpoint never
    mutates state.
    """
    from api.services import get_credential
    from api.services.openapi.pls_status import get_pls_status

    del caller
    try:
        status = get_pls_status(
            get_credential(),
            subscription_id=subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
        return status.to_dict()
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
    from api.services.openapi.token import get_openapi_api_token_status

    try:
        return get_openapi_api_token_status(
            get_credential(),
            subscription_id=subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
            resource_group=resource_group,
            cluster_name=cluster_name,
            caller_oid=caller.object_id or "",
            tenant_id=caller.tenant_id or "",
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
    from api.services.openapi.token import ensure_openapi_api_token

    LOGGER.info(
        "openapi token update requested cluster=%s caller_oid=%s regenerate=%s",
        body.cluster_name,
        redact_oid(caller.object_id),
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


class OpenApiPublicHttpsRequest(BaseModel):
    subscription_id: str = ""
    resource_group: str
    cluster_name: str
    operator_email: str = ""


# IANA reserved + commonly-private TLDs that Let's Encrypt rejects at
# ACME account registration time with
# `urn:ietf:params:acme:error:invalidContact` ("Domain name does not end
# with a valid public suffix (TLD)"). Mirrored in
# `web/src/components/SettingsPanel.tsx::PRIVATE_USE_TLDS` so the SPA
# disables the Enable button before the request even leaves the browser.
_PRIVATE_USE_TLDS: frozenset[str] = frozenset(
    {
        "local",
        "localhost",
        "internal",
        "test",
        "example",
        "invalid",
        "lan",
        "home",
        "corp",
        "private",
    }
)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+$")


def _validate_operator_email(value: str) -> str:
    """Reject empty / private-TLD emails before enqueuing the Celery task.

    Defence-in-depth for the SPA gate — a stale browser tab or a
    hand-crafted POST must not be able to enqueue
    `setup_openapi_public_https` with `noreply@elb-dashboard.local` and
    silently fail half-way through the install (regression on
    elb-cluster-01, 2026-05-27).
    """
    text = (value or "").strip()
    if not text or len(text) > 254 or not _EMAIL_RE.match(text):
        raise HTTPException(
            status_code=400,
            detail="operator_email is required and must be a valid RFC 5322 address",
        )
    domain = text.split("@", 1)[1].lower()
    if ".." in domain or domain.endswith("."):
        raise HTTPException(
            status_code=400, detail="operator_email domain is malformed"
        )
    labels = domain.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        raise HTTPException(
            status_code=400, detail="operator_email domain must include a public TLD"
        )
    tld = labels[-1]
    if not tld.isalpha() or len(tld) < 2:
        raise HTTPException(
            status_code=400, detail="operator_email TLD must be alphabetic"
        )
    if tld in _PRIVATE_USE_TLDS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Let's Encrypt rejects private-use TLDs "
                f"(.{tld}). Use a public TLD email such as ops@example.com."
            ),
        )
    return text


@router.get("/openapi/public-https")
def aks_openapi_public_https_status(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the cached public HTTPS endpoint state for the SPA panel.

    Reads from ops Redis only — no kubectl round trip — so polling is
    cheap. ``{enabled: false}`` means the operator has never run the
    setup task (or ran the disable task afterwards).
    """

    from api.tasks.openapi import get_openapi_public_https_status

    del caller
    return get_openapi_public_https_status()


@router.get("/openapi/public-https/operator-email-rules")
def aks_openapi_operator_email_rules(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Expose the validator rules so the SPA can sync its client gate.

    Single source of truth: the backend's `_validate_operator_email`
    rejects empty / private-TLD emails (otherwise Let's Encrypt rejects
    ACME account registration with `urn:ietf:params:acme:error:invalidContact`).
    The SPA mirrors the rule client-side so the Enable button can be
    disabled without a round trip, but it fetches this list on mount so
    the two sides cannot drift if we later add a new private-use TLD to
    the backend without touching the SPA.
    """
    del caller
    return {
        "private_use_tlds": sorted(_PRIVATE_USE_TLDS),
        "email_regex": _EMAIL_RE.pattern,
        "max_length": 254,
    }


@router.post("/openapi/public-https")
def aks_openapi_public_https_enable(
    body: OpenApiPublicHttpsRequest,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Enqueue ``setup_openapi_public_https`` for the given AKS cluster.

    The task is idempotent; re-running it on a cluster that already has
    the public HTTPS path applied refreshes the Ingress + ClusterIssuer
    without burning a Let's Encrypt rate-limit slot (cert-manager reuses
    the existing Certificate Secret when present).
    """

    from api.tasks.openapi import setup_openapi_public_https

    email = _validate_operator_email(body.operator_email)
    LOGGER.info(
        "openapi public-https enable requested cluster=%s caller_oid=%s",
        body.cluster_name,
        redact_oid(caller.object_id),
    )
    result = _safe_delay(
        setup_openapi_public_https,
        subscription_id=body.subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
        resource_group=body.resource_group,
        cluster_name=body.cluster_name,
        operator_email=email,
        caller_oid=caller.object_id or "",
    )
    return {
        "id": result.id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/aks/openapi/public-https/{result.id}/status",
        "status": "queued",
    }


@router.delete("/openapi/public-https")
def aks_openapi_public_https_disable(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Enqueue ``disable_openapi_public_https`` — deletes Ingress + cert.

    ingress-nginx and cert-manager remain installed (cheap, useful for
    other apps + a future re-enable). The cached public base URL is
    cleared so the SPA flips its baseUrl back to the internal LB IP.

    Uses query params (not a JSON body) because some browser / proxy
    combinations strip the body from a `DELETE` request, and the
    SPA's authenticated fetch wrapper only routes JSON bodies through
    POST / PUT.
    """

    from api.tasks.openapi import disable_openapi_public_https

    LOGGER.info(
        "openapi public-https disable requested cluster=%s caller_oid=%s",
        cluster_name,
        redact_oid(caller.object_id),
    )
    result = _safe_delay(
        disable_openapi_public_https,
        subscription_id=subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
        resource_group=resource_group,
        cluster_name=cluster_name,
        caller_oid=caller.object_id or "",
    )
    return {
        "id": result.id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/aks/openapi/public-https/{result.id}/status",
        "status": "queued",
    }


@router.get("/openapi/public-https/{task_id}/status")
def aks_openapi_public_https_task_status(
    task_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Translate the Celery ``AsyncResult`` into a SPA-friendly envelope.

    Mirrors ``aks_openapi_deploy_status`` shape so the SPA can reuse the
    same polling helper for both flows.
    """

    from celery.result import AsyncResult

    from api.celery_app import celery_app

    del caller
    result = AsyncResult(task_id, app=celery_app)
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
        custom_status.update({"phase": "completed"})
        output = dict(payload)
    else:
        err = ""
        try:
            err = str(result.result or result.info or "")[:500]
        except Exception:
            err = "task failed"
        custom_status.update({"phase": "failed"})
        output = {"status": "failed", "error": err}

    return {
        "task_id": task_id,
        "runtime_status": runtime_status,
        "custom_status": custom_status,
        "output": output,
    }


@router.get("/openapi/spec")
def aks_openapi_spec(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Best-effort proxy for the deployed OpenAPI service's ``/openapi.json``.

    Resolves the LoadBalancer IP via the K8s API, then fetches the spec.
    Prefers the operator-configured ``OPENAPI_PUBLIC_BASE_URL`` (HTTPS
    endpoint) over the cluster IP when set; that hook is no-op when the
    env is empty, preserving the legacy IP-based fetch path 1:1.
    Returns a degraded ``openapi:"3.0.0"`` placeholder when the service is
    not yet reachable so the SPA's docs page does not crash.
    """

    from api.services import get_credential
    from api.services.k8s.monitoring import k8s_get_service_ip
    from api.services.openapi.runtime import get_public_tls_base_url

    sub = subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", "")
    cred = get_credential()
    public_base = get_public_tls_base_url()

    base_url: str = ""
    if public_base:
        base_url = public_base
    else:
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
                **_peering_recovery_hint(),
            }
        base_url = f"http://{ip}"

    try:
        from api.services.httpx_pool import get_pooled_client

        client = get_pooled_client("aks-openapi-spec", timeout=10.0)
        for path in ("/openapi.json", "/docs/openapi.json"):
            resp = client.get(f"{base_url}{path}")
            if resp.status_code == 200:
                return cast(dict[str, Any], resp.json())
    except Exception as exc:
        LOGGER.warning("openapi/spec: fetch failed for %s: %s", base_url, exc)

    if not public_base:
        try:
            proxied = _fetch_openapi_spec_via_k8s_proxy(cred, sub, resource_group, cluster_name)
            if proxied is not None:
                return proxied
        except Exception as exc:
            LOGGER.warning("openapi/spec: k8s service proxy fetch failed: %s", exc)

    return {
        "openapi": "3.0.0",
        "info": {"title": "elb-openapi (spec not available)", "version": "0.0.0"},
        "paths": {},
        "degraded": True,
        "degraded_reason": "openapi_endpoint_unreachable",
        **_peering_recovery_hint(),
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
    from api.services.k8s.monitoring import k8s_get_service_ip
    from api.services.openapi.exec_gate import evaluate_openapi_exec_gate
    from api.services.openapi.proxy_audit import (
        is_state_changing_method,
        record_openapi_proxy_exec,
    )
    from api.services.openapi.runtime import get_public_tls_base_url

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

    # Opt-in RBAC enforcement (charter §12a Rule 4, default-OFF behind
    # ENFORCE_OPENAPI_EXEC_RBAC). When enabled, a state-changing call is
    # only forwarded if the caller actually holds a write role
    # (Contributor / Owner / AKS write) on the target resource group;
    # otherwise we deny BEFORE resolving the upstream or injecting the
    # admin token. Read-only GETs and the dev-bypass identity are never
    # gated, and with the env unset the legacy "any tenant member" path is
    # preserved exactly. The RBAC lookup is synchronous ARM IO, so run it
    # off the event loop.
    import asyncio

    exec_decision = await asyncio.to_thread(
        evaluate_openapi_exec_gate,
        cred,
        caller=caller,
        method=request.method,
        subscription_id=sub,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    if not exec_decision.allowed:
        raise HTTPException(
            status_code=exec_decision.status_code,
            detail=exec_decision.detail,
        )

    # Pick the upstream base URL. The TLS-terminated public endpoint, when
    # configured, is always safe for admin-token injection because the
    # transit is encrypted end-to-end. When the env is unset we fall back
    # to the historical IP path with all its safety gates intact.
    public_base = get_public_tls_base_url()
    use_public_tls = bool(public_base and public_base.lower().startswith("https://"))

    if use_public_tls:
        upstream_base = public_base
    else:
        try:
            ip = k8s_get_service_ip(cred, sub, resource_group, cluster_name, "elb-openapi")
        except Exception as exc:
            ip = None
            LOGGER.warning("openapi/proxy: k8s_get_service_ip failed: %s", exc)

        if not ip:
            # Include a Retry-After hint so the SPA's SwaggerTryIt /
            # OpenApiPanel can back off instead of retrying every render
            # cycle while the Service IP is still being provisioned
            # (typical “cluster just started” window).
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "openapi_service_not_reachable",
                    "message": "The elb-openapi service is not reachable yet.",
                    "retryable": True,
                    "retry_after_seconds": 15,
                    **_peering_recovery_hint(),
                },
                headers={"Retry-After": "15"},
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
        upstream_base = f"http://{ip}"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _OPENAPI_PROXY_ALLOWED_HEADERS
    }
    api_token = os.environ.get("ELB_OPENAPI_API_TOKEN", "").strip()
    if not api_token:
        from api.services.openapi.runtime import get_openapi_api_token

        api_token = get_openapi_api_token()
    if not api_token:
        try:
            from api.services.openapi.token import get_openapi_api_token_status

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

    # Forensic audit trail: the proxy injects the admin token. When the
    # opt-in RBAC gate above is OFF (the default), any authenticated tenant
    # member — even a subscription Reader — can drive state-changing calls
    # through the admin token, so record WHO drove each mutating call
    # (POST/PUT/PATCH/DELETE) for traceability. When the gate is ON the call
    # has already passed the write-role check, and the row still captures
    # who executed. Read-only GETs are intentionally not audited (polling
    # noise). Best-effort and run off the event loop; never blocks the proxy.
    if is_state_changing_method(request.method):
        await asyncio.to_thread(
            record_openapi_proxy_exec,
            method=request.method,
            target_path=target_path,
            subscription_id=sub,
            resource_group=resource_group,
            cluster_name=cluster_name,
            caller_oid=caller.object_id or "",
            tenant_id=caller.tenant_id or "",
        )

    body = await request.body()
    # Streaming proxy: open the AsyncClient OUTSIDE the request scope so
    # we can attach its lifecycle to the streaming response generator.
    # Buffering the upstream response (the previous ``upstream.content``)
    # would force a full BLAST result file into the api sidecar's RAM
    # before the browser sees the first byte; large XML / gzip responses
    # multiplied through the api sidecar's memory under concurrent loads.
    #
    # Per-phase timeouts: a 5 s connect cap turns the "VNet peering not
    # yet set up" case into a fast `openapi_upstream_unreachable` 502
    # (with the SPA recovery affordance) instead of a 30 s "Sending…"
    # spinner — the same gap that the new `/api/aks/peer-with-platform`
    # endpoint exists to close. Read/write/pool stay at 30 s so a large
    # BLAST result that takes a while to stream is not interrupted.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
        follow_redirects=False,
    )
    try:
        upstream_req = client.build_request(
            request.method,
            f"{upstream_base}{target_path}",
            headers=headers,
            content=body if body else None,
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        LOGGER.warning(
            "openapi/proxy: upstream request failed for %s: %s", upstream_base, exc
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "openapi_upstream_unreachable",
                "message": "The elb-openapi endpoint did not respond.",
                "retryable": True,
                **_peering_recovery_hint(),
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

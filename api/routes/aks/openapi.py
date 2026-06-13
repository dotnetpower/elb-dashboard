"""AKS-hosted OpenAPI deployment + spec routes.

Responsibility: AKS-hosted OpenAPI deployment / status / token / public-HTTPS /
spec routes (the API Reference "Try it" reverse proxy lives in the sibling
`openapi_proxy.py` module).
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `aks_openapi_deploy`, `aks_openapi_deploy_status`,
`aks_openapi_deployment`, `aks_openapi_token`, `aks_openapi_token_generate`,
`aks_openapi_spec`
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

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay
from api.services.sanitise import redact_oid

LOGGER = logging.getLogger(__name__)

router = APIRouter()

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


def _lb_pending_recovery_hint(
    cred: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, str]:
    """Pick the most specific recovery hint when the LB IP is missing.

    When the ``elb-openapi`` internal LoadBalancer has no IP, the cause is
    usually one of two BYO-network gaps: (1) the cluster identity lacks Network
    Contributor on the node subnet (the LB controller's ARM call 403s, GitHub
    #33), or (2) a missing VNet peering. Case 1 has a distinct, one-click fix
    (`/api/aks/openapi/lb-subnet-rbac`), so probe the Service events and return
    the `grant_lb_subnet_rbac` hint when that signature is present; otherwise
    fall back to the generic peering hint. Best-effort and additive — a probe
    failure degrades to the peering hint, never raises.
    """
    try:
        from api.services.aks.openapi_lb_rbac import (
            detect_lb_subnet_rbac_missing,
            lb_subnet_rbac_recovery_hint,
        )

        if detect_lb_subnet_rbac_missing(
            cred, subscription_id, resource_group, cluster_name
        ):
            return lb_subnet_rbac_recovery_hint()
    except Exception:
        LOGGER.debug("lb-pending hint: rbac detection failed", exc_info=True)
    return _peering_recovery_hint()


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


def _classify_openapi_startup(
    cred: Any,
    sub: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any] | None:
    """Best-effort probe of the ``elb-openapi`` rollout startup state.

    Wraps ``get_openapi_pod_startup_state`` so the spec route can tell a
    still-starting pod (benign, self-resolving image cold-pull) from a
    genuinely unreachable endpoint (VNet peering). Returns ``None`` on any
    failure so the caller keeps its existing peering-repair fallback.
    """

    try:
        from api.services.openapi.pod_phase import get_openapi_pod_startup_state

        return get_openapi_pod_startup_state(
            cred,
            subscription_id=sub,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    except Exception as exc:
        LOGGER.warning("openapi/spec: startup-state probe failed: %s", exc)
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

    # Resolve the ACR resource group with the same precedence the auto-deploy
    # path uses (api/tasks/openapi/auto_deploy.py): explicit body value wins,
    # then the platform env. The deploy task hard-fails when ``acr_name`` is
    # set but ``acr_resource_group`` is empty, so a SPA build that saved a
    # config without the ACR RG would otherwise queue a task that is
    # guaranteed to raise. Falling back to the platform env keeps the
    # single-cluster default (ACR in the platform RG) working without forcing
    # the operator to re-enter it.
    acr_resource_group = (
        (body.get("acr_resource_group", "") or "").strip()
        or os.environ.get("PLATFORM_ACR_RESOURCE_GROUP", "").strip()
        or os.environ.get("AZURE_RESOURCE_GROUP", "").strip()
    )

    result = _safe_delay(
        deploy_openapi_service,
        subscription_id=body.get("subscription_id", "") or "",
        resource_group=rg,
        cluster_name=cluster_name,
        acr_name=acr_name,
        acr_resource_group=acr_resource_group,
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


@router.post("/openapi/lb-subnet-rbac")
def aks_openapi_lb_subnet_rbac(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Grant the cluster identity Network Contributor on its BYO node subnet.

    Recovery for clusters created out-of-band (manual ``az aks create`` /
    delete+recreate) whose ``elb-openapi`` internal LoadBalancer stays
    ``<pending>`` with a ``subnets/<snet> ... 403 AuthorizationFailed`` event.
    Idempotent — mirrors the grant ``provision_aks`` performs at create time
    (see GitHub issue #33). Synchronous like ``/api/aks/peer-with-platform``.

    Note: granting on an already-running cluster does not take effect
    immediately (the cloud-controller caches its ARM token); the response
    ``note`` explains the token-cache caveat and the stop/start workaround.
    """

    cluster_name = (body.get("cluster_name") or "").strip()
    resource_group = (body.get("resource_group") or "").strip()
    if not (cluster_name and resource_group):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_parameters",
                "message": "resource_group and cluster_name are required.",
            },
        )
    subscription_id = (
        body.get("subscription_id") or os.getenv("AZURE_SUBSCRIPTION_ID", "") or ""
    ).strip()
    if not subscription_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_parameters",
                "message": (
                    "subscription_id is required (env AZURE_SUBSCRIPTION_ID "
                    "is not set in this sidecar)."
                ),
            },
        )

    LOGGER.info(
        "aks/openapi/lb-subnet-rbac requested cluster=%s rg=%s caller_oid=%s",
        cluster_name,
        resource_group,
        redact_oid(caller.object_id),
    )

    from api.services import get_credential
    from api.services.aks.openapi_lb_rbac import ensure_openapi_lb_subnet_rbac

    try:
        return ensure_openapi_lb_subnet_rbac(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
        )
    except Exception as exc:
        # The grant helper raises only on a non-recoverable ARM error (e.g. the
        # dashboard MI lacks roleAssignments/write at the subnet scope). Surface
        # 502 with a recovery hint rather than a raw 500.
        LOGGER.exception("aks/openapi/lb-subnet-rbac: grant failed")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "lb_subnet_rbac_grant_failed",
                "message": (
                    "Could not grant Network Contributor on the node subnet: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                ),
            },
        ) from exc


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
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    cluster_name: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the cached public HTTPS endpoint state for the SPA panel.

    Reads from ops Redis only — no kubectl round trip — so polling is
    cheap. ``{enabled: false}`` means the operator has never run the
    setup task (or ran the disable task afterwards).

    The cluster context (``subscription_id`` / ``resource_group`` /
    ``cluster_name``) scopes the lookup to that cluster's per-cluster key
    so a previously-enabled cluster's public FQDN never leaks onto a
    different cluster's API page. The params are optional for backward
    compatibility; when any is missing the legacy global key is read.
    """

    from api.tasks.openapi import get_openapi_public_https_status

    del caller
    return get_openapi_public_https_status(
        subscription_id=subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
        resource_group=resource_group,
        cluster_name=cluster_name,
    )


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
    public_base = get_public_tls_base_url(
        subscription_id=sub,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )

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
                **_lb_pending_recovery_hint(cred, sub, resource_group, cluster_name),
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

    # The spec fetch failed. Before blaming VNet peering (the historical
    # default), check whether the elb-openapi pod is simply still starting —
    # e.g. a fresh blastpool node cold-pulling the ~370 MB image takes ~90 s,
    # during which the LB has an IP but no Ready endpoint. Showing the
    # "Repair VNet peering" affordance in that window reads as an error for a
    # benign, self-resolving state. Only fall through to the peering hint when
    # the pod is genuinely Ready-but-unreachable (real peering break) or the
    # probe itself could not determine the state.
    startup = _classify_openapi_startup(cred, sub, resource_group, cluster_name)
    if startup is not None and startup["state"] in ("starting", "failed"):
        return {
            "openapi": "3.0.0",
            "info": {"title": "elb-openapi (starting)", "version": "0.0.0"},
            "paths": {},
            "degraded": True,
            "degraded_reason": (
                "openapi_pod_starting"
                if startup["state"] == "starting"
                else "openapi_pod_not_ready"
            ),
            "pod_state": startup["state"],
            "pod_reason": startup["reason"],
            "pod_message": startup["message"],
            "ready_replicas": startup["ready_replicas"],
            "desired_replicas": startup["desired_replicas"],
        }

    return {
        "openapi": "3.0.0",
        "info": {"title": "elb-openapi (spec not available)", "version": "0.0.0"},
        "paths": {},
        "degraded": True,
        "degraded_reason": "openapi_endpoint_unreachable",
        **_peering_recovery_hint(),
    }

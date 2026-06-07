"""AKS-hosted OpenAPI reverse-proxy route.

The API Reference "Try it" reverse proxy that forwards browser calls to the
deployed ``elb-openapi`` pod (injecting the admin ``X-ELB-API-Token``), split
out of `api/routes/aks/openapi.py` so the deployment / spec concerns and the
proxy concern each own a single-responsibility route module under the shared
`aks_router`.

Responsibility: Serve `/openapi/proxy`, enforce the Try-It path allowlist +
    private-IP token-safety gate + opt-in RBAC exec gate, then stream the
    upstream response back to the browser.
Edit boundaries: HTTP validation + path/transport safety + streaming only; the
    RBAC decision lives in `api/services/openapi/exec_gate.py`, the audit row in
    `api/services/openapi/proxy_audit.py`, and Service-IP/TLS resolution in
    `api/services/openapi/runtime.py` / `api/services/k8s/monitoring.py`.
Key entry points: `aks_openapi_proxy`, `_enforce_openapi_proxy_target_path`,
    `_reject_dashboard_uuid_job_path`.
Risky contracts: The admin token MUST NOT be forwarded to a non-private IP
    unless `OPENAPI_ALLOW_PUBLIC_LB=true`; the path allowlist + deny-token list
    MUST stay so an authenticated caller cannot ride the admin token into
    `/admin/*` on the upstream. Every non-health `/api/*` route enforces
    `require_caller`.
Validation: `uv run pytest -q api/tests/test_openapi_proxy_route.py
    api/tests/test_openapi_rate_limit.py api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _OPENAPI_PROXY_ALLOWED_HEADERS
from api.routes.aks.openapi import _peering_recovery_hint

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
        if prefix.endswith("/"):
            # Prefix entry (e.g. "/v1/", "/docs/"): the bare root or anything
            # beneath it is allowed.
            if lowered == prefix.rstrip("/") or lowered.startswith(prefix):
                return
            continue
        # Exact-path entry (e.g. "/healthz", "/openapi.json"): ONLY the exact
        # path is allowed. A bare ``startswith`` here would let
        # ``/healthzXXX`` / ``/openapi.jsonXXX`` ride the admin token into any
        # upstream route that happens to share that prefix, contradicting the
        # documented "entry without '/' is an exact path" contract.
        if lowered == prefix:
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

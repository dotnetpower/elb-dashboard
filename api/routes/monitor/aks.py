"""AKS monitor routes.

Responsibility: AKS monitor routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `list_aks`, `aks_nodes`, `aks_pods`, `aks_top_nodes`, `aks_pod_logs`,
`aks_pod_describe`, `aks_pod_delete`, `aks_service_ip`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py
api/tests/test_monitor_cache.py`.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller
from api.routes.monitor.common import _cache_key, _graceful, _sub_default
from api.services import monitoring as monitoring_svc
from api.services.cluster_health import cached_snapshot_with_cluster_gate
from api.services.monitor_cache import cached_snapshot
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/aks")
def list_aks(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    include_unmanaged: bool = Query(default=False),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """List AKS clusters. When `resource_group` is empty, returns every
    ElasticBLAST-managed cluster in the subscription (filtered by ARM tags
    `managedBy=elb-dashboard` / `app=elastic-blast`, or the legacy `blastpool`
    agent-pool fingerprint). Pass `include_unmanaged=true` to disable the
    filter — diagnostics only; the dashboard never sets this in normal use
    because foreign clusters would surface BLAST controls on unrelated
    workloads. When `resource_group` is provided, returns the RG-scoped list
    unchanged (every cluster in that RG, no tag filter)."""
    sub = subscription_id or _sub_default()
    if not sub:
        raise HTTPException(400, "subscription_id required")
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    if resource_group:
        try:
            return cached_snapshot(
                _cache_key("monitor", "aks", sub, resource_group),
                lambda: {"clusters": monitoring_svc.list_aks_clusters(cred, sub, resource_group)},
            )
        except Exception as exc:
            return cast(dict[str, Any], _graceful("aks_list", exc, empty={"clusters": []}))
    # Subscription-wide path.
    try:
        return cached_snapshot(
            _cache_key("monitor", "aks", sub, "sub", str(include_unmanaged)),
            lambda: {
                "clusters": monitoring_svc.list_aks_clusters_in_subscription(
                    cred, sub, include_unmanaged=include_unmanaged
                ),
                "scope": "subscription",
            },
        )
    except Exception as exc:
        return cast(dict[str, Any], _graceful("aks_list", exc, empty={"clusters": []}))


@router.get("/aks/nodes")
def aks_nodes(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        return cached_snapshot_with_cluster_gate(
            _cache_key("monitor", "aks", "nodes", sub, resource_group, cluster_name),
            lambda: {
                "nodes": monitoring_svc.k8s_get_nodes(cred, sub, resource_group, cluster_name)
            },
            credential=cred,
            subscription_id=sub,
            resource_group=resource_group,
            cluster_name=cluster_name,
            empty={"nodes": []},
        )
    except Exception as exc:
        return cast(dict[str, Any], _graceful("aks_nodes", exc, empty={"nodes": []}))


@router.get("/aks/pods")
def aks_pods(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        return cached_snapshot_with_cluster_gate(
            _cache_key("monitor", "aks", "pods", sub, resource_group, cluster_name),
            lambda: {"pods": monitoring_svc.k8s_get_pods(cred, sub, resource_group, cluster_name)},
            credential=cred,
            subscription_id=sub,
            resource_group=resource_group,
            cluster_name=cluster_name,
            empty={"pods": []},
        )
    except Exception as exc:
        return cast(dict[str, Any], _graceful("aks_pods", exc, empty={"pods": []}))


@router.get("/aks/top-nodes")
def aks_top_nodes(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        return cached_snapshot_with_cluster_gate(
            _cache_key("monitor", "aks", "top-nodes", sub, resource_group, cluster_name),
            lambda: {
                "nodes": monitoring_svc.k8s_top_nodes(cred, sub, resource_group, cluster_name)
            },
            credential=cred,
            subscription_id=sub,
            resource_group=resource_group,
            cluster_name=cluster_name,
            empty={"nodes": []},
        )
    except Exception as exc:
        return cast(dict[str, Any], _graceful("aks_top_nodes", exc, empty={"nodes": []}))


@router.get("/aks/node-pressure")
def aks_node_pressure(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Per-pool CPU/memory request pressure with a 90% warning threshold.

    Designed for the dashboard's "systempool is 99% requested" early
    warning that the 2026-05-28 ingress-nginx Pending regression needed
    an operator to discover by hand. The SPA renders pool name + CPU/MEM
    percentage + a warning badge when either >=90%.
    """
    sub = subscription_id or _sub_default()
    from api.routes import monitor as monitor_package
    from api.services.k8s.node_pressure import k8s_node_request_pressure

    del caller
    cred = monitor_package.get_credential()
    try:
        return cached_snapshot_with_cluster_gate(
            _cache_key("monitor", "aks", "node-pressure", sub, resource_group, cluster_name),
            lambda: k8s_node_request_pressure(cred, sub, resource_group, cluster_name),
            credential=cred,
            subscription_id=sub,
            resource_group=resource_group,
            cluster_name=cluster_name,
            empty={"reachable": False, "pools": {}},
        )
    except Exception as exc:
        return cast(
            dict[str, Any],
            _graceful(
                "aks_node_pressure",
                exc,
                empty={"reachable": False, "pools": {}},
            ),
        )


@router.get("/aks/pod-logs")
def aks_pod_logs(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    namespace: str = Query(...),
    pod_name: str = Query(...),
    tail: int = Query(default=200, ge=1, le=10000),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        logs = monitoring_svc.k8s_pod_logs(
            cred, sub, resource_group, cluster_name, namespace, pod_name, tail
        )
        return {"logs": logs}
    except Exception as exc:
        return cast(dict[str, Any], _graceful("aks_pod_logs", exc, empty={"logs": ""}))


@router.get("/aks/pod-describe")
def aks_pod_describe(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    namespace: str = Query(...),
    pod_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        describe = monitoring_svc.k8s_pod_describe(
            cred, sub, resource_group, cluster_name, namespace, pod_name
        )
        return {"describe": describe}
    except Exception as exc:
        return cast(
            dict[str, Any], _graceful("aks_pod_describe", exc, empty={"describe": ""})
        )


@router.delete("/aks/pod")
def aks_pod_delete(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    namespace: str = Query(...),
    pod_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Delete a single pod. Refuses system-managed namespaces server-side.

    The SPA hides the action for system namespaces, but this route also
    enforces it — frontend-only authorization is an OWASP A01 violation."""
    sub = subscription_id or _sub_default()
    from api.routes import monitor as monitor_package
    from api.services.k8s.observability import SYSTEM_NAMESPACES

    if namespace in SYSTEM_NAMESPACES:
        raise HTTPException(
            status_code=403,
            detail=f"namespace {namespace!r} is system-managed; refusing to delete",
        )

    cred = monitor_package.get_credential()
    try:
        result = monitoring_svc.k8s_pod_delete(
            cred, sub, resource_group, cluster_name, namespace, pod_name
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        LOGGER.warning("aks_pod_delete failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"delete failed: {type(exc).__name__}") from exc

    # Invalidate the pods list cache so the dashboard reflects the change on
    # the next poll without waiting for the snapshot TTL.
    from api.services.monitor_cache import invalidate_monitor_snapshot_prefix

    invalidate_monitor_snapshot_prefix(
        _cache_key("monitor", "aks", "pods", sub, resource_group, cluster_name)
    )
    return result


@router.get("/aks/service-ip")
def aks_service_ip(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    service_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the LoadBalancer external IP for a service in the AKS cluster.

    Response shape (matches the SPA's `monitoringApi.serviceIp` typing):
        ``{"service_name": "<name>", "external_ip": "<addr>|null", "available": bool}``

    Missing service / pending LoadBalancer ingress is an expected discovery
    state for the OpenAPI page, not an HTTP error. Keep that as a 200 so the
    request inspector and request metrics do not surface routine deploy-panel
    discovery as a failing API call.
    """

    sub = subscription_id or _sub_default()
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        ip = monitoring_svc.k8s_get_service_ip(
            cred, sub, resource_group, cluster_name, service_name
        )
    except Exception as exc:
        # k8s_get_service_ip should already swallow not-found and return
        # None. If a lower layer still raises, keep discovery non-fatal for
        # the API Reference page; the deploy panel can guide the user through
        # reconciliation without polluting the inspector with expected 404s.
        LOGGER.warning("aks_service_ip: lookup failed: %s", exc)
        ip = None
    if not ip:
        return {
            "service_name": service_name,
            "external_ip": None,
            "available": False,
            "status": "missing_or_pending",
        }
    return {"service_name": service_name, "external_ip": ip, "available": True, "status": "ready"}


@router.get("/aks/warmup-status")
def aks_warmup_status(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        return cached_snapshot_with_cluster_gate(
            _cache_key("monitor", "aks", "warmup-status", sub, resource_group, cluster_name),
            lambda: monitoring_svc.k8s_warmup_status(cred, sub, resource_group, cluster_name),
            credential=cred,
            subscription_id=sub,
            resource_group=resource_group,
            cluster_name=cluster_name,
            empty={"databases": []},
        )
    except Exception as exc:
        return cast(dict[str, Any], _graceful("aks_warmup_status", exc, empty={"databases": []}))


# ---------------------------------------------------------------------------
# AKS events (lightweight k8s events feed for the cluster bento Live activity)
# ---------------------------------------------------------------------------
_EVENTS_NAMESPACE_RE = __import__("re").compile(r"^[a-z0-9-]{1,64}$")
# Azure resource group name rules (≤90, alphanumeric + . _ - ( ), no
# trailing dot).  AKS cluster name is even tighter (≤63, alphanumeric +
# - and _) but we keep them on the same regex for simplicity since the
# Azure SDK already enforces the strict form server-side.
_AZ_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9._\-()]{1,90}$")


@router.get("/aks/events")
def aks_events(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    namespace: str = Query(default=""),
    limit: int = Query(default=30, ge=1, le=200),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Recent k8s events, sorted newest-first.

    `namespace=""` means "all namespaces".  Requests through the kubelet
    API; if the cluster is stopped or RBAC denies access, returns the
    standard degraded payload instead of 500.

    Output is sanitised — pod/container ids and node names are kept,
    but message content runs through `sanitise()` so a misbehaved
    container that prints a SAS or bearer token cannot leak it via
    the dashboard.
    """
    if namespace and not _EVENTS_NAMESPACE_RE.match(namespace):
        raise HTTPException(400, "invalid namespace")
    if not _AZ_NAME_RE.match(resource_group):
        raise HTTPException(400, "invalid resource_group")
    if not _AZ_NAME_RE.match(cluster_name):
        raise HTTPException(400, "invalid cluster_name")
    sub = subscription_id or _sub_default()
    if not sub:
        raise HTTPException(400, "subscription_id required")
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        from api.services.k8s.monitoring import k8s_list_events

        def load_events() -> dict[str, Any]:
            events = k8s_list_events(
                cred,
                sub,
                resource_group,
                cluster_name,
                namespace=namespace or None,
                limit=limit,
            )
            # Defence in depth: redact every message before it leaves the
            # api sidecar.  The k8s_list_events helper already drops fields
            # that have no business in the SPA, but messages are free-form.
            for ev in events:
                if isinstance(ev.get("message"), str):
                    ev["message"] = sanitise(ev["message"])[:512]
            return {"events": events}

        return cached_snapshot(
            _cache_key(
                "monitor", "aks", "events", sub, resource_group, cluster_name, namespace, limit
            ),
            load_events,
        )
    except Exception as exc:
        return cast(dict[str, Any], _graceful("aks_events", exc, empty={"events": []}))


# ---------------------------------------------------------------------------
# Request metrics — process-local ring buffer (latency p50/p95/p99 + errors)
# ---------------------------------------------------------------------------


@router.post("/aks/run-command")
def aks_run_command(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Run a kubectl command on the AKS cluster via the Kubernetes API.

    Uses the k8s direct API helpers from monitoring service, NOT Azure Run Command
    (which is ~30s slow and ARM-rate-limited per copilot-instructions.md §11).
    """
    sub = body.get("subscription_id", "") or _sub_default()
    rg = body.get("resource_group", "")
    cluster_name = body.get("cluster_name", "")
    command = body.get("command", "")

    if not command or not rg or not cluster_name:
        return {
            "exit_code": 1,
            "output": "Missing required fields: resource_group, cluster_name, command",
        }

    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        normalized = command.strip()
        # Strip optional leading "kubectl " so the matcher only has to deal
        # with the verb + resource form.
        if normalized.startswith("kubectl "):
            normalized = normalized[len("kubectl ") :].lstrip()

        # Accept both plural and singular resource forms — `kubectl` itself
        # treats them interchangeably (`get pod` ≡ `get pods`).
        if normalized.startswith("get pods") or normalized.startswith("get pod"):
            namespace = body.get("namespace")
            pods = monitoring_svc.k8s_get_pods(cred, sub, rg, cluster_name, namespace=namespace)
            import json

            return {"exit_code": 0, "output": json.dumps(pods, indent=2, default=str)}
        elif normalized.startswith("get nodes") or normalized.startswith("get node"):
            nodes = monitoring_svc.k8s_get_nodes(cred, sub, rg, cluster_name)
            import json

            return {"exit_code": 0, "output": json.dumps(nodes, indent=2, default=str)}
        elif normalized.startswith("top nodes") or normalized.startswith("top node"):
            metrics = monitoring_svc.k8s_top_nodes(cred, sub, rg, cluster_name)
            import json

            return {"exit_code": 0, "output": json.dumps(metrics, indent=2, default=str)}
        else:
            return {
                "exit_code": 1,
                "output": (
                    "Command not supported via API proxy. Supported: get pod(s), get node(s), "
                    "top node(s). Use the Browser Terminal for arbitrary kubectl commands."
                ),
            }
    except Exception as exc:
        return {"exit_code": 1, "output": f"Error: {type(exc).__name__}: {str(exc)[:500]}"}


# ---------------------------------------------------------------------------
# ACR
# ---------------------------------------------------------------------------

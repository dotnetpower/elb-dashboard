"""AKS monitor routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller
from api.routes.monitor.common import _cache_key, _graceful, _sub_default
from api.services import monitoring as monitoring_svc
from api.services.monitor_cache import cached_snapshot
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/aks")
def list_aks(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    if not sub:
        raise HTTPException(400, "subscription_id required")
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        return cached_snapshot(
            _cache_key("monitor", "aks", sub, resource_group),
            lambda: {"clusters": monitoring_svc.list_aks_clusters(cred, sub, resource_group)},
        )
    except Exception as exc:
        return _graceful("aks_list", exc, empty={"clusters": []})


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
        return cached_snapshot(
            _cache_key("monitor", "aks", "nodes", sub, resource_group, cluster_name),
            lambda: {
                "nodes": monitoring_svc.k8s_get_nodes(cred, sub, resource_group, cluster_name)
            },
        )
    except Exception as exc:
        return _graceful("aks_nodes", exc, empty={"nodes": []})


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
        return cached_snapshot(
            _cache_key("monitor", "aks", "pods", sub, resource_group, cluster_name),
            lambda: {"pods": monitoring_svc.k8s_get_pods(cred, sub, resource_group, cluster_name)},
        )
    except Exception as exc:
        return _graceful("aks_pods", exc, empty={"pods": []})


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
        return cached_snapshot(
            _cache_key("monitor", "aks", "top-nodes", sub, resource_group, cluster_name),
            lambda: {
                "nodes": monitoring_svc.k8s_top_nodes(cred, sub, resource_group, cluster_name)
            },
        )
    except Exception as exc:
        return _graceful("aks_top_nodes", exc, empty={"nodes": []})


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
        return _graceful("aks_pod_logs", exc, empty={"logs": ""})


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
        ``{"service_name": "<name>", "external_ip": "<addr>"}``

    Raises HTTP 404 when the service does not exist or has no LoadBalancer
    ingress yet. The SPA relies on the error state to render the
    OpenApiDeployPanel — never collapse "no IP yet" into a 200 response or
    the page hangs in the "Discovering OpenAPI service…" state.
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
        # None, but if it raises something else (auth, network), surface a
        # 404 so the SPA shows the deploy panel.
        LOGGER.warning("aks_service_ip: lookup failed: %s", exc)
        raise HTTPException(
            status_code=404,
            detail={"code": "service_not_found", "service_name": service_name},
        ) from exc
    if not ip:
        raise HTTPException(
            status_code=404,
            detail={"code": "service_no_external_ip", "service_name": service_name},
        )
    return {"service_name": service_name, "external_ip": ip}


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
        return cached_snapshot(
            _cache_key("monitor", "aks", "warmup-status", sub, resource_group, cluster_name),
            lambda: monitoring_svc.k8s_warmup_status(cred, sub, resource_group, cluster_name),
        )
    except Exception as exc:
        return _graceful("aks_warmup_status", exc, empty={"databases": []})


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
        from api.services.k8s_monitoring import k8s_list_events

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
        return _graceful("aks_events", exc, empty={"events": []})


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
        # Use k8s_get_pods as a proxy for basic kubectl commands
        if command.strip().startswith("get pods") or command.strip().startswith("kubectl get pods"):
            namespace = body.get("namespace")
            pods = monitoring_svc.k8s_get_pods(cred, sub, rg, cluster_name, namespace=namespace)
            import json

            return {"exit_code": 0, "output": json.dumps(pods, indent=2, default=str)}
        elif command.strip().startswith("get nodes") or command.strip().startswith(
            "kubectl get nodes"
        ):
            nodes = monitoring_svc.k8s_get_nodes(cred, sub, rg, cluster_name)
            import json

            return {"exit_code": 0, "output": json.dumps(nodes, indent=2, default=str)}
        elif command.strip().startswith("top nodes") or command.strip().startswith(
            "kubectl top nodes"
        ):
            metrics = monitoring_svc.k8s_top_nodes(cred, sub, rg, cluster_name)
            import json

            return {"exit_code": 0, "output": json.dumps(metrics, indent=2, default=str)}
        else:
            return {
                "exit_code": 1,
                "output": (
                    "Command not supported via API proxy. Supported: get pods, get nodes, "
                    "top nodes. Use the Browser Terminal for arbitrary kubectl commands."
                ),
            }
    except Exception as exc:
        return {"exit_code": 1, "output": f"Error: {type(exc).__name__}: {str(exc)[:500]}"}


# ---------------------------------------------------------------------------
# ACR
# ---------------------------------------------------------------------------

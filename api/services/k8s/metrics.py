"""Kubernetes metrics API helpers.

Responsibility: Kubernetes metrics API helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `k8s_top_nodes`, `k8s_top_pods`, `_node_capacity`, `_node_capacity_with_meta`
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from azure.core.credentials import TokenCredential

LOGGER = logging.getLogger(__name__)

# Status codes the metrics.k8s.io aggregated API returns when metrics-server
# is not ready or not installed: 503 ServiceUnavailable (the APIService points
# at a metrics-server that is still starting / unhealthy) and 404 (the
# aggregated API is not registered at all). Both are transient or
# configuration states, not request errors, so the top-nodes / top-pods
# helpers degrade to an empty result instead of raising an HTTPError whose
# traceback floods the api sidecar logs (App Insights / container-log audit
# 2026-06-13 found 58 such 503 tracebacks).
_METRICS_UNAVAILABLE_STATUS = frozenset({404, 503})


def k8s_top_nodes(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> list[dict[str, Any]]:
    """Return node resource usage from the Kubernetes metrics API."""

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        capacity = _node_capacity_with_meta(session, server)
        response = session.get(f"{server}/apis/metrics.k8s.io/v1beta1/nodes", timeout=10)
        if getattr(response, "status_code", None) in _METRICS_UNAVAILABLE_STATUS:
            LOGGER.debug(
                "metrics.k8s.io nodes unavailable (status=%s); returning empty",
                response.status_code,
            )
            return []
        response.raise_for_status()
        nodes: list[dict[str, Any]] = []
        for item in response.json().get("items", []):
            name = item["metadata"]["name"]
            usage = item.get("usage", {})
            cpu_m = _parse_cpu_millicores(usage.get("cpu", "0"))
            mem_ki = _parse_memory_ki(usage.get("memory", "0"))
            meta = capacity.get(
                name,
                {
                    "cpu_m": 1,
                    "mem_ki": 1,
                    "pool": "",
                    "ready": True,
                    "conditions": {},
                },
            )
            cpu_cap = meta.get("cpu_m") or 1
            mem_cap = meta.get("mem_ki") or 1
            nodes.append(
                {
                    "name": name,
                    "cpu": f"{cpu_m}m",
                    "cpu_pct": round(cpu_m / cpu_cap * 100) if cpu_cap else 0,
                    "memory": f"{mem_ki // 1024}Mi",
                    "memory_pct": round(mem_ki / mem_cap * 100) if mem_cap else 0,
                    "memory_total": f"{mem_cap // 1024}Mi",
                    "cpu_m": cpu_m,
                    "mem_ki": mem_ki,
                    "cpu_capacity_m": cpu_cap,
                    "mem_capacity_ki": mem_cap,
                    "pool": meta.get("pool", ""),
                    "ready": bool(meta.get("ready", True)),
                    "conditions": meta.get("conditions", {}),
                }
            )
        _enrich_with_page_cache(session, server, nodes)
        return nodes
    finally:
        session.close()


def _enrich_with_page_cache(
    session: Any, server: str, nodes: list[dict[str, Any]]
) -> None:
    """Add ``cache_ki`` / ``cache_pct`` to each node when the kubelet exposes it.

    Best-effort: the metrics.k8s.io ``usage`` only carries working set, which
    excludes the reclaimable file (page) cache where a warmed BLAST DB actually
    lives. We sample the kubelet ``/stats/summary`` proxy to surface that cache
    as a distinct bar segment. Any failure (e.g. ``nodes/proxy`` denied) leaves
    the nodes untouched so the panel renders working-set-only, exactly as before.
    """
    if not nodes:
        return
    try:
        from api.services.k8s.node_cache import fetch_node_cache_ki

        cache_by_node = fetch_node_cache_ki(session, server, [n["name"] for n in nodes])
    except Exception:
        return
    for node in nodes:
        cache_ki = cache_by_node.get(node["name"])
        if cache_ki is None:
            continue
        mem_cap = node.get("mem_capacity_ki") or 0
        node["cache_ki"] = cache_ki
        node["cache_pct"] = round(cache_ki / mem_cap * 100) if mem_cap else 0


def _node_capacity(session: Any, server: str) -> dict[str, dict[str, int]]:
    response = session.get(f"{server}/api/v1/nodes", timeout=10)
    response.raise_for_status()
    capacity: dict[str, dict[str, int]] = {}
    for item in response.json().get("items", []):
        name = item["metadata"]["name"]
        cap = item.get("status", {}).get("capacity", {})
        capacity[name] = {
            "cpu_m": _parse_cpu_millicores(cap.get("cpu", "0")),
            "mem_ki": _parse_memory_ki(cap.get("memory", "0")),
        }
    return capacity


def _node_capacity_with_meta(session: Any, server: str) -> dict[str, dict[str, Any]]:
    response = session.get(f"{server}/api/v1/nodes", timeout=10)
    response.raise_for_status()
    out: dict[str, dict[str, Any]] = {}
    for item in response.json().get("items", []):
        meta = item.get("metadata", {})
        labels = meta.get("labels", {}) or {}
        status = item.get("status", {})
        cap = status.get("capacity", {})
        pool = labels.get("agentpool") or labels.get("kubernetes.azure.com/agentpool") or ""
        ready = False
        conditions: dict[str, str] = {}
        for cond in status.get("conditions", []) or []:
            condition_type = cond.get("type", "")
            condition_status = cond.get("status", "")
            if not condition_type:
                continue
            conditions[condition_type] = condition_status
            if condition_type == "Ready":
                ready = condition_status == "True"
        out[meta.get("name", "")] = {
            "cpu_m": _parse_cpu_millicores(cap.get("cpu", "0")),
            "mem_ki": _parse_memory_ki(cap.get("memory", "0")),
            "pool": pool,
            "ready": ready,
            "conditions": conditions,
        }
    return out


def _parse_cpu_millicores(raw: str) -> int:
    """Parse a Kubernetes CPU quantity into millicores (best-effort).

    The metrics API reports node/pod CPU usage with a unit suffix that depends
    on the kubelet's precision: ``n`` (nanocores), ``u`` (microcores), or ``m``
    (millicores); capacity is usually a bare core count (``"8"``) or millicores.
    Unrecognised shapes return ``0`` instead of raising so one odd value can't
    crash the whole AKS top-nodes snapshot refresh.
    """
    value = str(raw).strip()
    if not value:
        return 0
    try:
        if value.endswith("n"):
            return int(value[:-1]) // 1_000_000
        if value.endswith("u"):
            return int(value[:-1]) // 1_000
        if value.endswith("m"):
            return int(value[:-1])
        return int(float(value) * 1000)
    except (ValueError, TypeError):
        return 0


def _parse_memory_ki(raw: str) -> int:
    """Parse a Kubernetes memory quantity into KiB (best-effort).

    Handles the IEC suffixes the metrics/capacity API emit (``Ki``/``Mi``/``Gi``/
    ``Ti``) plus a bare byte count. Unrecognised shapes return ``0`` rather than
    raising so a single odd value can't crash the node snapshot refresh.
    """
    value = str(raw).strip()
    if not value:
        return 0
    try:
        if value.endswith("Ki"):
            return int(value[:-2])
        if value.endswith("Mi"):
            return int(value[:-2]) * 1024
        if value.endswith("Gi"):
            return int(value[:-2]) * 1024 * 1024
        if value.endswith("Ti"):
            return int(value[:-2]) * 1024 * 1024 * 1024
        return int(value) // 1024
    except (ValueError, TypeError):
        return 0


def k8s_top_pods(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str | None = None,
    label_selector: str | None = None,
) -> list[dict[str, Any]]:
    """Return per-pod / per-container resource usage from the Kubernetes metrics API.

    Mirrors ``kubectl top pod --containers`` output but as structured data.
    Used for BLAST workload right-sizing (memory peak detection) and for the
    admission-control slot manager. Filters by namespace and/or labelSelector
    when provided so callers can scope to BLAST job pods only.
    """

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        if namespace:
            url = f"{server}/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods"
        else:
            url = f"{server}/apis/metrics.k8s.io/v1beta1/pods"
        params: dict[str, str] = {}
        if label_selector:
            params["labelSelector"] = label_selector
        response = session.get(url, params=params or None, timeout=10)
        if getattr(response, "status_code", None) in _METRICS_UNAVAILABLE_STATUS:
            LOGGER.debug(
                "metrics.k8s.io pods unavailable (status=%s); returning empty",
                response.status_code,
            )
            return []
        response.raise_for_status()
        pods: list[dict[str, Any]] = []
        for item in response.json().get("items", []):
            meta = item.get("metadata", {}) or {}
            containers_out: list[dict[str, Any]] = []
            pod_cpu_m = 0
            pod_mem_ki = 0
            for container in item.get("containers", []) or []:
                usage = container.get("usage", {}) or {}
                cpu_m = _parse_cpu_millicores(usage.get("cpu", "0"))
                mem_ki = _parse_memory_ki(usage.get("memory", "0"))
                pod_cpu_m += cpu_m
                pod_mem_ki += mem_ki
                containers_out.append(
                    {
                        "name": container.get("name", ""),
                        "cpu_m": cpu_m,
                        "mem_ki": mem_ki,
                    }
                )
            pods.append(
                {
                    "namespace": meta.get("namespace", ""),
                    "name": meta.get("name", ""),
                    "window": item.get("window", ""),
                    "timestamp": item.get("timestamp", ""),
                    "cpu_m": pod_cpu_m,
                    "mem_ki": pod_mem_ki,
                    "mem_mi": pod_mem_ki // 1024,
                    "containers": containers_out,
                }
            )
        return pods
    finally:
        session.close()


__all__ = ["k8s_top_nodes", "k8s_top_pods"]

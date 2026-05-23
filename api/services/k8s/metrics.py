"""Kubernetes metrics API helpers.

Responsibility: Kubernetes metrics API helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `k8s_top_nodes`, `_node_capacity`, `_node_capacity_with_meta`
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py`.
"""

from __future__ import annotations

from typing import Any

from azure.core.credentials import TokenCredential


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
        return nodes
    finally:
        session.close()


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
    value = str(raw)
    if value.endswith("n"):
        return int(value[:-1]) // 1_000_000
    if value.endswith("m"):
        return int(value[:-1])
    return int(value) * 1000


def _parse_memory_ki(raw: str) -> int:
    value = str(raw)
    if value.endswith("Ki"):
        return int(value[:-2])
    if value.endswith("Mi"):
        return int(value[:-2]) * 1024
    if value.endswith("Gi"):
        return int(value[:-2]) * 1024 * 1024
    return int(value) // 1024


__all__ = ["k8s_top_nodes"]

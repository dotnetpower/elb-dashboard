"""Kubernetes node listing and warmup-node selection helpers.

Responsibility: Kubernetes node listing and warmup-node selection helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `k8s_get_nodes`, `k8s_ready_warmup_node_names`, `_candidate_warmup_node_names`
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py`.
"""

from __future__ import annotations

from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.client import _get_k8s_session


def k8s_get_nodes(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> list[dict[str, Any]]:
    """Return cluster nodes from the Kubernetes API."""

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(f"{server}/api/v1/nodes", timeout=10)
        response.raise_for_status()
        nodes: list[dict[str, Any]] = []
        for item in response.json().get("items", []):
            meta = item.get("metadata", {})
            status = item.get("status", {})
            conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
            info = status.get("nodeInfo", {})
            addresses = {a["type"]: a["address"] for a in status.get("addresses", [])}
            roles = (
                ",".join(
                    key.replace("node-role.kubernetes.io/", "")
                    for key in meta.get("labels", {})
                    if key.startswith("node-role.kubernetes.io/")
                )
                or "<none>"
            )
            nodes.append(
                {
                    "name": meta.get("name", ""),
                    "status": "Ready" if conditions.get("Ready") == "True" else "NotReady",
                    "roles": roles,
                    "age": meta.get("creationTimestamp", ""),
                    "version": info.get("kubeletVersion", ""),
                    "internal_ip": addresses.get("InternalIP", ""),
                    "os_image": info.get("osImage", ""),
                    "kernel": info.get("kernelVersion", ""),
                    "runtime": info.get("containerRuntimeVersion", ""),
                    # Additive operational signals for the diagnostics engine.
                    # Pressure conditions report "True" when the node is under
                    # disk / memory / PID pressure; `unschedulable` is True when
                    # the node is cordoned. Kept additive so existing consumers
                    # (monitor AKS card) ignore the extra keys.
                    "disk_pressure": conditions.get("DiskPressure") == "True",
                    "memory_pressure": conditions.get("MemoryPressure") == "True",
                    "pid_pressure": conditions.get("PIDPressure") == "True",
                    "unschedulable": bool(item.get("spec", {}).get("unschedulable", False)),
                }
            )
        return nodes
    finally:
        session.close()


def k8s_ready_warmup_node_names(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    preferred_pool: str = "blastpool",
) -> list[str]:
    """Return Ready node names suitable for node-local DB warmup Jobs."""

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(f"{server}/api/v1/nodes", timeout=10)
        response.raise_for_status()
        return _candidate_warmup_node_names(
            response.json().get("items", []), preferred_pool=preferred_pool
        )
    finally:
        session.close()


def _candidate_warmup_node_names(
    nodes: list[dict[str, Any]], *, preferred_pool: str = "blastpool"
) -> list[str]:
    candidates: list[tuple[str, str, str]] = []
    for node in nodes:
        metadata = node.get("metadata", {}) or {}
        spec = node.get("spec", {}) or {}
        status = node.get("status", {}) or {}
        name = str(metadata.get("name") or "")
        if not name or spec.get("unschedulable") is True:
            continue
        conditions = {
            item.get("type"): item.get("status")
            for item in status.get("conditions", []) or []
            if isinstance(item, dict)
        }
        if conditions.get("Ready") != "True":
            continue
        labels = metadata.get("labels", {}) or {}
        pool = str(labels.get("agentpool") or labels.get("kubernetes.azure.com/agentpool") or "")
        mode = str(labels.get("kubernetes.azure.com/mode") or "")
        candidates.append((name, pool, mode))

    preferred = [name for name, pool, _mode in candidates if pool == preferred_pool]
    if preferred:
        return sorted(preferred)

    user_nodes = [
        name
        for name, pool, mode in candidates
        if mode.lower() != "system" and pool.lower() not in {"system", "systempool"}
    ]
    # Never fall back to system-pool nodes: AKS system nodes carry the
    # `CriticalAddonsOnly` taint, so a warmup Job pinned to one stays Pending
    # forever and the wait loop times the whole warmup out. A cluster with only
    # system nodes has no valid warmup target — return an empty list so the
    # caller defers with "no Ready warmup nodes" instead of placing doomed Jobs.
    return sorted(user_nodes)

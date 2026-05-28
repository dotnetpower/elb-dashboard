"""Compute the systempool / blastpool CPU+memory request pressure.

Module docstring (natural):
Surfaces the "systempool is 99% requested" early warning that the
ingress-nginx scheduling regression of 2026-05-28 needed an
operator to discover by hand (`kubectl describe node …`). The
pressure number is the sum of every Pod's `containers[].resources.requests`
divided by the node's `status.allocatable`, reported per node and as
the per-pool max. Anything above 90% means the next add-on you install
on that pool will almost certainly land in `Pending`.

Responsibility: Pure read-only Kubernetes-API helper. Returns per-pool
    pressure dicts; never raises (every Azure / k8s exception is caught
    and surfaces as ``{"reachable": False, "reason": ...}``).
Edit boundaries: New helper only. Existing `k8s_get_nodes` semantics
    stay unchanged.
Key entry points: ``k8s_node_request_pressure``.
Risky contracts: CPU/memory parsing reuses the suffix conventions
    from `api.services.k8s.ingress::_parse_cpu_to_millicores` /
    `_parse_memory_to_bytes`. Re-export those if the parser ever moves.
Validation: ``uv run pytest -q api/tests/test_k8s_node_pressure.py``.
"""

from __future__ import annotations

from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.client import _get_k8s_session
from api.services.k8s.ingress import (
    _parse_cpu_to_millicores,
    _parse_memory_to_bytes,
)

_HIGH_PRESSURE_PCT = 90


def _node_pool_label(node: dict[str, Any]) -> str:
    labels = (node.get("metadata") or {}).get("labels") or {}
    return (
        labels.get("agentpool")
        or labels.get("kubernetes.azure.com/agentpool")
        or ""
    )


def _allocatable(node: dict[str, Any]) -> tuple[int, int]:
    alloc = (node.get("status") or {}).get("allocatable") or {}
    cpu_m = _parse_cpu_to_millicores(alloc.get("cpu")) or 0
    mem_b = _parse_memory_to_bytes(alloc.get("memory")) or 0
    return cpu_m, mem_b


def _pod_requests(pod: dict[str, Any]) -> tuple[int, int]:
    cpu_m = 0
    mem_b = 0
    for container in (pod.get("spec") or {}).get("containers") or []:
        requests = ((container.get("resources") or {}).get("requests") or {})
        cpu_m += _parse_cpu_to_millicores(requests.get("cpu")) or 0
        mem_b += _parse_memory_to_bytes(requests.get("memory")) or 0
    # Init containers count too (their requests count against the node at
    # scheduling time as `max(initContainer, sum(regularContainers))`,
    # but the simple sum is close enough for an alarm).
    return cpu_m, mem_b


def _pct(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return min(100, max(0, round(numerator * 100 / denominator)))


def k8s_node_request_pressure(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Return per-pool CPU/memory request pressure. Never raises.

    Response shape::

        {
          "reachable": True,
          "pools": {
            "systempool": {
              "nodes": 1,
              "cpu_request_pct": 99,
              "memory_request_pct": 60,
              "warning": True,           # any metric >= 90
              "max_node": "aks-systempool-…000001"
            },
            "blastpool": { ... }
          },
          "high_pressure_threshold_pct": 90
        }
    """
    try:
        session, server = _get_k8s_session(
            credential, subscription_id, resource_group, cluster_name
        )
    except Exception as exc:
        return {
            "reachable": False,
            "reason": f"k8s_session_failed: {type(exc).__name__}",
        }
    try:
        nodes_resp = session.get(f"{server}/api/v1/nodes", timeout=10)
        nodes_resp.raise_for_status()
        pods_resp = session.get(
            f"{server}/api/v1/pods?fieldSelector=status.phase!=Succeeded,status.phase!=Failed",
            timeout=15,
        )
        pods_resp.raise_for_status()
    except Exception as exc:
        return {
            "reachable": False,
            "reason": f"k8s_api_failed: {type(exc).__name__}",
        }
    finally:
        try:
            session.close()
        except Exception as close_exc:
            # Closing the SDK session twice is harmless; log at DEBUG so
            # the close path stays observable without spamming the worker.
            import logging

            logging.getLogger(__name__).debug(
                "node_pressure session close ignored: %s", close_exc
            )

    nodes = nodes_resp.json().get("items", []) or []
    pods = pods_resp.json().get("items", []) or []

    per_node: dict[str, dict[str, Any]] = {}
    for node in nodes:
        name = (node.get("metadata") or {}).get("name") or ""
        if not name:
            continue
        cpu_alloc, mem_alloc = _allocatable(node)
        per_node[name] = {
            "pool": _node_pool_label(node),
            "cpu_alloc_m": cpu_alloc,
            "mem_alloc_b": mem_alloc,
            "cpu_req_m": 0,
            "mem_req_b": 0,
        }
    for pod in pods:
        node_name = (pod.get("spec") or {}).get("nodeName") or ""
        if not node_name or node_name not in per_node:
            continue
        cpu_m, mem_b = _pod_requests(pod)
        per_node[node_name]["cpu_req_m"] += cpu_m
        per_node[node_name]["mem_req_b"] += mem_b

    pools: dict[str, dict[str, Any]] = {}
    for name, stats in per_node.items():
        pool = stats["pool"] or "<unknown>"
        agg = pools.setdefault(
            pool,
            {
                "nodes": 0,
                "cpu_request_pct": 0,
                "memory_request_pct": 0,
                "warning": False,
                "max_node": "",
            },
        )
        agg["nodes"] += 1
        cpu_pct = _pct(stats["cpu_req_m"], stats["cpu_alloc_m"])
        mem_pct = _pct(stats["mem_req_b"], stats["mem_alloc_b"])
        if cpu_pct > agg["cpu_request_pct"]:
            agg["cpu_request_pct"] = cpu_pct
            agg["max_node"] = name
        if mem_pct > agg["memory_request_pct"]:
            agg["memory_request_pct"] = mem_pct
        if cpu_pct >= _HIGH_PRESSURE_PCT or mem_pct >= _HIGH_PRESSURE_PCT:
            agg["warning"] = True

    return {
        "reachable": True,
        "pools": pools,
        "high_pressure_threshold_pct": _HIGH_PRESSURE_PCT,
    }


__all__ = ["k8s_node_request_pressure"]

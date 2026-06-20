"""Resolve AKS signals consumed by the BLAST capacity gate (Stage 3, issue #23).

The pure decision tree in
[api/services/blast/capacity_gate.py](./capacity_gate.py) takes already-resolved
inputs (per-pool request pressure, per-node usage, pending-pod count). This
module owns the side-effectful part: call the existing
``k8s_node_request_pressure`` / ``k8s_top_nodes`` / ``k8s_get_pods`` helpers
on the operator's behalf, fold the result into a single ``CapacitySignals``
snapshot, and short-circuit when the AKS cluster is Stopped / deleted so a
stale gate doesn't burn ARM/K8s API budget.

Responsibility: Turn ``(credential, subscription, rg, cluster)`` into a
``CapacitySignals`` snapshot the gate can consume, cached at the same TTL as
the dashboard's other K8s tiles.
Edit boundaries: Lives behind ``cached_snapshot_with_cluster_gate`` — never
import Celery here, never write Redis from this module. The slot hash is the
capacity gate's responsibility. K8s API calls go through
``api.services.k8s.*`` wrappers; do not poke ``requests`` directly.
Key entry points: ``CapacitySignals``, ``resolve_capacity_signals``,
``signal_cache_ttl_s``, ``signal_cache_stale_s``.
Risky contracts: ``resolve_capacity_signals`` MUST NEVER raise. A K8s API
hiccup must degrade to ``CapacitySignals(pressure=None, top_nodes=None,
pending_pods=0)``; the gate then maps that to the ``aks_unreachable`` deny
branch. Raising would break the submit_task call site behind
``BLAST_GATE_ENABLED=true``.
Validation: ``uv run pytest -q api/tests/test_blast_capacity_signals.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.blast.capacity_gate import GATE_DEFAULT_POOL_NAME
from api.services.cluster_health import cached_snapshot_with_cluster_gate
from api.services.env import env_int as _env_int

LOGGER = logging.getLogger(__name__)

GATE_DEFAULT_SIGNAL_CACHE_S = 30
GATE_DEFAULT_SIGNAL_STALE_S = 120


def signal_cache_ttl_s() -> int:
    return _env_int(
        "BLAST_GATE_SIGNAL_CACHE_S",
        GATE_DEFAULT_SIGNAL_CACHE_S,
        minimum=5,
        maximum=300,
    )


def signal_cache_stale_s() -> int:
    return _env_int(
        "BLAST_GATE_SIGNAL_STALE_S", GATE_DEFAULT_SIGNAL_STALE_S, minimum=10, maximum=600
    )


@dataclass(frozen=True)
class CapacitySignals:
    """Snapshot of the three AKS signals the gate consumes.

    All fields are tolerant: ``pressure=None`` or ``top_nodes=None`` flows
    naturally into the gate's ``aks_unreachable`` branch, and
    ``pending_pods=0`` is the safe default when the pods API is unreachable.
    """

    pressure: dict[str, Any] | None
    top_nodes: list[dict[str, Any]] | None
    pending_pods: int


_CACHE_PREFIX = "blast:capacity:signals"


def _cache_key(subscription_id: str, resource_group: str, cluster_name: str) -> str:
    return f"{_CACHE_PREFIX}:{subscription_id}:{resource_group}:{cluster_name}"


def _safe_node_request_pressure(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any] | None:
    from api.services.k8s.node_pressure import k8s_node_request_pressure

    try:
        return k8s_node_request_pressure(
            credential, subscription_id, resource_group, cluster_name
        )
    except Exception as exc:
        LOGGER.info(
            "capacity_signals.pressure failed cluster=%s: %s",
            cluster_name,
            type(exc).__name__,
        )
        return None


def _safe_top_nodes(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> list[dict[str, Any]] | None:
    from api.services.k8s.metrics import k8s_top_nodes

    try:
        return k8s_top_nodes(credential, subscription_id, resource_group, cluster_name)
    except Exception as exc:
        LOGGER.info(
            "capacity_signals.top_nodes failed cluster=%s: %s",
            cluster_name,
            type(exc).__name__,
        )
        return None


def _safe_pending_pods_count(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    pool_name: str,
) -> int:
    from api.services.k8s.monitoring import k8s_get_pods

    del pool_name  # pending-pod gate is namespace-agnostic; pool not needed
    try:
        pods = k8s_get_pods(credential, subscription_id, resource_group, cluster_name)
    except Exception as exc:
        LOGGER.info(
            "capacity_signals.pending_pods failed cluster=%s: %s",
            cluster_name,
            type(exc).__name__,
        )
        return 0
    pending = 0
    for pod in pods or []:
        if not isinstance(pod, dict):
            continue
        status = str(pod.get("status") or "").strip().lower()
        if status == "pending":
            pending += 1
    return pending


def resolve_capacity_signals(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    pool_name: str = GATE_DEFAULT_POOL_NAME,
) -> CapacitySignals:
    """Fetch + cache the three K8s signals the gate consumes.

    Never raises. Cached behind the standard cluster-health gate so a
    Stopped / deleted cluster doesn't burn ARM budget.
    """

    cache_key = _cache_key(subscription_id, resource_group, cluster_name)
    empty: dict[str, Any] = {"pressure": None, "top_nodes": None, "pending_pods": 0}

    def _loader() -> dict[str, Any]:
        pressure = _safe_node_request_pressure(
            credential, subscription_id, resource_group, cluster_name
        )
        top_nodes = _safe_top_nodes(
            credential, subscription_id, resource_group, cluster_name
        )
        pending = _safe_pending_pods_count(
            credential, subscription_id, resource_group, cluster_name, pool_name
        )
        return {
            "pressure": pressure,
            "top_nodes": top_nodes,
            "pending_pods": int(pending),
        }

    payload = cached_snapshot_with_cluster_gate(
        cache_key,
        _loader,
        credential=credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        empty=empty,
        ttl_seconds=signal_cache_ttl_s(),
        stale_seconds=signal_cache_stale_s(),
    )
    return CapacitySignals(
        pressure=payload.get("pressure"),
        top_nodes=payload.get("top_nodes"),
        pending_pods=int(payload.get("pending_pods") or 0),
    )


__all__ = (
    "GATE_DEFAULT_SIGNAL_CACHE_S",
    "GATE_DEFAULT_SIGNAL_STALE_S",
    "CapacitySignals",
    "resolve_capacity_signals",
    "signal_cache_stale_s",
    "signal_cache_ttl_s",
)

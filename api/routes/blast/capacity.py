"""`GET /api/blast/capacity` — snapshot of the BLAST capacity gate.

Responsibility: Render a single snapshot of the cluster-aware capacity gate
so the dashboard can show "BLAST queue: 1 of 1 slots in use; CPU 42% / mem
38%; gate enabled" without forcing the operator to read the worker logs.
Edit boundaries: HTTP validation + JSON shaping only. All Azure / K8s / Redis
calls go through ``api.services.blast.{capacity_gate,capacity_signals}``.
Key entry points: ``blast_capacity_snapshot``.
Risky contracts: Must enforce ``require_caller``. The response is read-only —
even when ``BLAST_GATE_ENABLED=false`` this endpoint returns the *would-have-
been* decision preview so operators can validate the gate before flipping it
to enforce.
Validation: ``uv run pytest -q api/tests/test_blast_capacity_route.py``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from api.auth import CallerIdentity, require_caller
from api.services import get_credential
from api.services.blast import capacity_gate, capacity_signals
from api.services.response_contracts import build_meta, request_id_from_scope

router = APIRouter()
LOGGER = logging.getLogger(__name__)


def _gate_enabled() -> bool:
    raw = os.environ.get("BLAST_GATE_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@router.get("/capacity")
def blast_capacity_snapshot(
    request: Request,
    subscription_id: str = Query(..., min_length=1),
    resource_group: str = Query(..., min_length=1),
    cluster_name: str = Query(..., min_length=1),
    program: str = Query("blastn", min_length=1),
    database: str = Query("nt", min_length=1),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the live capacity gate snapshot for one cluster.

    The endpoint never raises — every Azure / K8s degradation is folded into
    the response. Operators can call this even when ``BLAST_GATE_ENABLED`` is
    OFF to preview what the gate *would* decide.
    """

    enabled = _gate_enabled()
    pool_name = capacity_gate.GATE_DEFAULT_POOL_NAME
    snap = capacity_signals.CapacitySignals(pressure=None, top_nodes=None, pending_pods=0)
    signals_error: str | None = None
    try:
        credential = get_credential()
        snap = capacity_signals.resolve_capacity_signals(
            credential, subscription_id, resource_group, cluster_name, pool_name=pool_name
        )
    except Exception as exc:  # pragma: no cover - safety net
        signals_error = type(exc).__name__
        LOGGER.warning(
            "blast_capacity signals resolve failed cluster=%s rg=%s sub=%s: %s",
            cluster_name,
            resource_group,
            subscription_id,
            signals_error,
        )

    try:
        active = capacity_gate.list_active_reservations(cluster_name)
    except Exception as exc:  # pragma: no cover - safety net
        LOGGER.warning(
            "blast_capacity reservations failed cluster=%s: %s",
            cluster_name,
            type(exc).__name__,
        )
        active = []

    try:
        demand = capacity_gate.predict_demand(program=program, database=database)
    except Exception:  # pragma: no cover - safety net
        demand = capacity_gate.ResourceDemand(cpu_m=0, mem_mib=0)

    try:
        decision = capacity_gate.evaluate_capacity_gate(
            pressure=snap.pressure,
            top_nodes=snap.top_nodes,
            pending_pods_count=snap.pending_pods,
            predicted_demand=demand,
            active_reservations=active,
            pool_name=pool_name,
        )
    except Exception as exc:  # pragma: no cover - safety net
        LOGGER.warning(
            "blast_capacity gate evaluate failed cluster=%s: %s",
            cluster_name,
            type(exc).__name__,
        )
        decision = capacity_gate.GateDecision(
            admit=False, reason="evaluate_error", retryable=True
        )

    # Extract pool-level pressure for the cleaner UI summary. The full payload
    # is intentionally hidden — operators read the cluster card for that.
    pool_pressure: dict[str, Any] | None = None
    if isinstance(snap.pressure, dict) and isinstance(snap.pressure.get("pools"), dict):
        pool_pressure = snap.pressure["pools"].get(pool_name)

    cpu_request_pct = (
        int(pool_pressure.get("cpu_request_pct", 0) or 0) if isinstance(pool_pressure, dict) else 0
    )
    memory_request_pct = (
        int(pool_pressure.get("memory_request_pct", 0) or 0)
        if isinstance(pool_pressure, dict)
        else 0
    )

    response_payload: dict[str, Any] = {
        "enabled": enabled,
        "pool": pool_name,
        "slots": {
            "in_use": len(active),
            "max": capacity_gate.max_slots_per_cluster(),
        },
        "cpu_request_pct": cpu_request_pct,
        "memory_request_pct": memory_request_pct,
        "watermark_cpu_pct": capacity_gate.cpu_watermark_pct(),
        "watermark_memory_pct": capacity_gate.mem_watermark_pct(),
        "pending_pods": snap.pending_pods,
        "decision_preview": "admit" if decision.admit else "deny",
        "decision_reason": decision.reason,
        "decision_retryable": decision.retryable,
        "predicted_demand": {
            "cpu_m": demand.cpu_m,
            "mem_mib": demand.mem_mib,
        },
        "active_reservations": [asdict(r) for r in active],
        "signals_degraded": snap.pressure is None or snap.top_nodes is None,
        "signals_error": signals_error,
        "counters": capacity_gate.gate_counters_snapshot(cluster_name),
    }

    meta = build_meta(request_id=request_id_from_scope(request))
    return {"data": response_payload, "meta": meta}

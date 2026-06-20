"""AKS capacity gate — admission control for parallel BLAST submits (Stage 1, pure module).

Implements the decision + slot-accounting helpers described in
[docs/research/aks-capacity-gate.md](../../../docs/research/aks-capacity-gate.md).
This module is intentionally NOT wired into the live submit task in Stage 1.
Stage 3 will add the call site behind the ``BLAST_GATE_ENABLED`` env flag.

Responsibility: Turn already-resolved AKS signals (request pressure, node
usage, pending-pod count) plus the set of currently active reservations into
a single ``GateDecision``, and atomically reserve / release per-cluster
slots in a Redis hash.
Edit boundaries: Only stdlib + ``api.services.redis_clients`` for the pool
helper. No FastAPI, Celery, Azure SDK, or Kubernetes-API imports here —
callers resolve those signals and pass them in so this module stays trivially
testable. The default constants ship the gate in a behaviour-equivalent shape
to the existing per-cluster Redis lock (``max_slots=1``).
Key entry points: ``evaluate_capacity_gate``, ``reserve_slot``,
``release_slot``, ``list_active_reservations``, ``predict_demand``,
``slot_hash_key``, ``GateDecision``, ``Reservation``, ``ResourceDemand``.
Risky contracts: Reservation is atomic via a Lua ``HLEN < max ? HSET : 0``
script — DO NOT replace with a check-then-act sequence in Python or two
workers will exceed ``max_slots`` under contention. The Redis client returned
by ``get_broker_redis_client`` is a process-shared singleton; callers MUST
NOT call ``.close()`` on it. Slot hash carries a safety TTL so a worker that
dies mid-submit cannot pin a slot forever — but releasing in a ``finally``
block is the primary correctness mechanism.
Validation: ``uv run pytest -q api/tests/test_blast_capacity_gate.py``.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from api.services.env import env_int as _env_int

LOGGER = logging.getLogger(__name__)

# Slot hash prefix — one Redis hash per cluster, fields are job_id, values
# are JSON ``Reservation`` snapshots. Per-pool gating is forward-compatible:
# the helper takes the pool name explicitly, but today there is only one
# ``blastpool`` per cluster so a single hash key is sufficient.
GATE_SLOT_HASH_PREFIX = "elb:blast:slots"

# Defaults — all env-overridable. Charter §12a Rule 4: new guards ship in a
# state that is behaviour-equivalent to today, so ``max_slots`` defaults to 1
# (the existing per-cluster lock depth). Watermarks sit safely below the
# operator-facing 90% warning the existing ``k8s_node_request_pressure``
# helper already surfaces.
GATE_DEFAULT_MAX_SLOTS_PER_CLUSTER = 1
GATE_DEFAULT_CPU_WATERMARK_PCT = 75
GATE_DEFAULT_MEM_WATERMARK_PCT = 75
GATE_DEFAULT_DEMAND_CPU_M = 1000
GATE_DEFAULT_DEMAND_MEM_MIB = 4096
GATE_DEFAULT_SLOT_TTL_S = 1800
GATE_DEFAULT_POOL_NAME = "blastpool"

# Atomic reservation script:
#   if HLEN(hash) >= max AND not HEXISTS(hash, job_id):
#       return 0
#   HSET(hash, job_id, payload)
#   EXPIRE(hash, ttl)
#   return 1
# Re-reserving for the same job_id is intentionally idempotent — a retry
# loop that already owns its slot must not be rejected.
_RESERVE_LUA = """
local count = redis.call('HLEN', KEYS[1])
local has = redis.call('HEXISTS', KEYS[1], ARGV[2])
if tonumber(count) >= tonumber(ARGV[1]) and has == 0 then
  return 0
end
redis.call('HSET', KEYS[1], ARGV[2], ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceDemand:
    """Predicted CPU (millicores) + memory (MiB) for a single submit."""

    cpu_m: int
    mem_mib: int

    @property
    def mem_b(self) -> int:
        return self.mem_mib * 1024 * 1024


@dataclass(frozen=True)
class Reservation:
    """One slot held by a job in flight. Persisted as JSON in the slot hash."""

    job_id: str
    reserved_at: str  # ISO8601 UTC
    cpu_m: int
    mem_mib: int

    @property
    def mem_b(self) -> int:
        return self.mem_mib * 1024 * 1024

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> Reservation | None:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                job_id=str(data["job_id"]),
                reserved_at=str(data["reserved_at"]),
                cpu_m=int(data.get("cpu_m", 0)),
                mem_mib=int(data.get("mem_mib", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class GateDecision:
    """Result of ``evaluate_capacity_gate``.

    ``admit=True`` means the caller MAY proceed to ``reserve_slot``.
    ``admit=False`` with ``retryable=True`` is the requeue path (the
    existing 30s ``waiting_for_submit_slot`` flow renamed to
    ``waiting_for_capacity``). ``retryable=False`` is a hard reject —
    the caller should surface a ``Retry-After`` and not re-enqueue.
    """

    admit: bool
    reason: str | None = None
    retryable: bool = True
    slots_in_use: int = 0
    headroom_cpu_m: int = 0
    headroom_mem_mib: int = 0
    measured_pct: int | None = None
    predicted_cpu_m: int = 0
    predicted_mem_mib: int = 0


# ---------------------------------------------------------------------------
# Config — pulled from env on every call so tests can flip without reloading
# ---------------------------------------------------------------------------


def max_slots_per_cluster() -> int:
    """Slot ceiling for one cluster. Default 1 = behaviour-equivalent to today."""
    return _env_int(
        "BLAST_GATE_MAX_SLOTS_PER_CLUSTER",
        GATE_DEFAULT_MAX_SLOTS_PER_CLUSTER,
        minimum=1,
        maximum=64,
    )


def cpu_watermark_pct() -> int:
    return _env_int(
        "BLAST_GATE_CPU_WATERMARK_PCT",
        GATE_DEFAULT_CPU_WATERMARK_PCT,
        minimum=1,
        maximum=100,
    )


def mem_watermark_pct() -> int:
    return _env_int(
        "BLAST_GATE_MEM_WATERMARK_PCT",
        GATE_DEFAULT_MEM_WATERMARK_PCT,
        minimum=1,
        maximum=100,
    )


def default_demand_cpu_m() -> int:
    return _env_int(
        "BLAST_GATE_DEFAULT_DEMAND_CPU_M",
        GATE_DEFAULT_DEMAND_CPU_M,
        minimum=1,
    )


def default_demand_mem_mib() -> int:
    return _env_int(
        "BLAST_GATE_DEFAULT_DEMAND_MEM_MIB",
        GATE_DEFAULT_DEMAND_MEM_MIB,
        minimum=1,
    )


def slot_ttl_s() -> int:
    return _env_int(
        "BLAST_GATE_SLOT_TTL_S",
        GATE_DEFAULT_SLOT_TTL_S,
        minimum=60,
        maximum=24 * 3600,
    )


# ---------------------------------------------------------------------------
# Pure helpers — no Redis, no AKS calls
# ---------------------------------------------------------------------------


def slot_hash_key(cluster_name: str) -> str:
    """Per-cluster Redis hash key. Mirrors the ``submit_lock_key`` style."""
    cluster = (cluster_name or "_unknown").strip() or "_unknown"
    return f"{GATE_SLOT_HASH_PREFIX}:{cluster}"


def _pool_headroom(top_nodes: list[dict[str, Any]] | None, pool_name: str) -> tuple[int, int]:
    """Return ``(allocatable_cpu_m, allocatable_mem_mib)`` summed across nodes in ``pool_name``.

    Reads the shape returned by ``api.services.k8s.metrics.k8s_top_nodes``::

        {"name": "...", "pool": "blastpool",
         "cpu_m": 1200, "cpu_capacity_m": 4000,
         "mem_ki": 2_000_000, "mem_capacity_ki": 16_000_000, "ready": true, ...}

    Headroom = ``capacity - currently used``. Returns ``(0, 0)`` if the pool
    is missing or every entry is malformed — callers treat that as "no
    capacity known, cannot admit".
    """
    if not top_nodes:
        return (0, 0)
    cpu_free_m = 0
    mem_free_mib = 0
    target = (pool_name or "").strip().lower()
    for node in top_nodes:
        if not isinstance(node, dict):
            continue
        if (str(node.get("pool", "")).strip().lower()) != target:
            continue
        if node.get("ready") is False:
            continue
        try:
            cap_cpu = int(node.get("cpu_capacity_m") or 0)
            used_cpu = int(node.get("cpu_m") or 0)
            cap_mem_ki = int(node.get("mem_capacity_ki") or 0)
            used_mem_ki = int(node.get("mem_ki") or 0)
        except (TypeError, ValueError):
            continue
        cpu_free_m += max(0, cap_cpu - used_cpu)
        mem_free_mib += max(0, (cap_mem_ki - used_mem_ki) // 1024)
    return (cpu_free_m, mem_free_mib)


def _blastpool_pressure(
    pressure: dict[str, Any] | None, pool_name: str
) -> dict[str, Any] | None:
    """Pull the named pool out of the ``k8s_node_request_pressure`` payload.

    Returns ``None`` when the payload is missing, unreachable, or doesn't
    contain ``pool_name``. Tolerant of both lowercase / capitalised pool names
    because the upstream label depends on the AKS provisioning flavour.
    """
    if not isinstance(pressure, dict) or not pressure.get("reachable"):
        return None
    pools = pressure.get("pools") or {}
    if not isinstance(pools, dict):
        return None
    target = (pool_name or "").strip().lower()
    for name, entry in pools.items():
        if str(name).strip().lower() == target and isinstance(entry, dict):
            return entry
    return None


def _reservation_demand(reservations: list[Reservation]) -> tuple[int, int]:
    cpu = sum(r.cpu_m for r in reservations)
    mem = sum(r.mem_mib for r in reservations)
    return (cpu, mem)


# ---------------------------------------------------------------------------
# Demand prediction — Stage 1 ships Tier 3 (env fallback) only.
# Tier 1 (per-(program, db) history) + Tier 2 (per-program defaults) are
# follow-ups; this signature is forward-compatible.
# ---------------------------------------------------------------------------


def predict_demand(
    program: str | None = None,  # used by Tier 1/2 in later stages
    database: str | None = None,
) -> ResourceDemand:
    """Predicted resource demand for one submit.

    Stage 1: returns the env-configurable conservative default. Stages 2-3
    will add (program, database) history and per-program presets ahead of
    this fallback. The signature is stable so call sites don't need to
    change when those tiers land.
    """
    return ResourceDemand(cpu_m=default_demand_cpu_m(), mem_mib=default_demand_mem_mib())


# ---------------------------------------------------------------------------
# The decision function
# ---------------------------------------------------------------------------


def evaluate_capacity_gate(
    *,
    pressure: dict[str, Any] | None,
    top_nodes: list[dict[str, Any]] | None,
    pending_pods_count: int,
    predicted_demand: ResourceDemand,
    active_reservations: list[Reservation],
    pool_name: str = GATE_DEFAULT_POOL_NAME,
    max_slots: int | None = None,
    cpu_watermark: int | None = None,
    mem_watermark: int | None = None,
) -> GateDecision:
    """Evaluate the gate. Pure — every input is already-resolved data.

    Caller is expected to resolve the AKS signals via
    ``k8s_node_request_pressure`` + ``k8s_top_nodes`` + a pending-pod count
    (cached for ``BLAST_GATE_SIGNAL_CACHE_S`` seconds) and pass them in.
    Active reservations are read from the slot hash via
    ``list_active_reservations``.

    Decision tree (first match wins):

    1. ``aks_unreachable`` — pressure payload missing / unreachable.
    2. ``pods_pending`` — ``pending_pods_count > 0`` (something already
       can't be scheduled; do not add to the queue).
    3. ``cpu_watermark`` / ``memory_watermark`` — per-pool **request**
       pressure exceeds the watermark (default 75%).
    4. ``slot_cap_reached`` — ``len(active_reservations) >= max_slots``
       and the caller doesn't already own a slot.
    5. ``reserved_cpu_exhausted`` / ``reserved_memory_exhausted`` — even
       with current headroom, the sum of in-flight reservations + this
       prediction would exceed what the pool can host.
    6. otherwise ``admit=True``.

    All denies in this Stage-1 module return ``retryable=True``. A
    ``retryable=False`` reject path (hard cap exceeded, e.g. node pool
    autoscaler maxed out for >N minutes) is reserved for Stage 3 wiring.
    """
    max_slots_value = max_slots if max_slots is not None else max_slots_per_cluster()
    cpu_wm = cpu_watermark if cpu_watermark is not None else cpu_watermark_pct()
    mem_wm = mem_watermark if mem_watermark is not None else mem_watermark_pct()
    slots_in_use = len(active_reservations)
    pred_cpu = predicted_demand.cpu_m
    pred_mem = predicted_demand.mem_mib

    base_meta = {
        "slots_in_use": slots_in_use,
        "predicted_cpu_m": pred_cpu,
        "predicted_mem_mib": pred_mem,
    }

    pool_pressure = _blastpool_pressure(pressure, pool_name)
    if pool_pressure is None:
        reason = (
            "aks_unreachable"
            if not isinstance(pressure, dict) or not pressure.get("reachable")
            else "pool_not_found"
        )
        return GateDecision(admit=False, reason=reason, retryable=True, **base_meta)

    if pending_pods_count > 0:
        return GateDecision(
            admit=False,
            reason="pods_pending",
            retryable=True,
            measured_pct=int(pending_pods_count),
            **base_meta,
        )

    cpu_pct = int(pool_pressure.get("cpu_request_pct", 0) or 0)
    if cpu_pct >= cpu_wm:
        return GateDecision(
            admit=False,
            reason="cpu_watermark",
            retryable=True,
            measured_pct=cpu_pct,
            **base_meta,
        )
    mem_pct = int(pool_pressure.get("memory_request_pct", 0) or 0)
    if mem_pct >= mem_wm:
        return GateDecision(
            admit=False,
            reason="memory_watermark",
            retryable=True,
            measured_pct=mem_pct,
            **base_meta,
        )

    if slots_in_use >= max_slots_value:
        return GateDecision(
            admit=False,
            reason="slot_cap_reached",
            retryable=True,
            measured_pct=max_slots_value,
            **base_meta,
        )

    avail_cpu_m, avail_mem_mib = _pool_headroom(top_nodes, pool_name)
    reserved_cpu_m, reserved_mem_mib = _reservation_demand(active_reservations)
    head_cpu = max(0, avail_cpu_m - reserved_cpu_m)
    head_mem = max(0, avail_mem_mib - reserved_mem_mib)

    if pred_cpu > head_cpu:
        return GateDecision(
            admit=False,
            reason="reserved_cpu_exhausted",
            retryable=True,
            headroom_cpu_m=head_cpu,
            headroom_mem_mib=head_mem,
            **base_meta,
        )
    if pred_mem > head_mem:
        return GateDecision(
            admit=False,
            reason="reserved_memory_exhausted",
            retryable=True,
            headroom_cpu_m=head_cpu,
            headroom_mem_mib=head_mem,
            **base_meta,
        )

    return GateDecision(
        admit=True,
        reason=None,
        retryable=True,
        headroom_cpu_m=head_cpu,
        headroom_mem_mib=head_mem,
        **base_meta,
    )


# ---------------------------------------------------------------------------
# Reservation primitives — touch Redis via the shared broker pool
# ---------------------------------------------------------------------------


def reserve_slot(
    cluster_name: str,
    job_id: str,
    demand: ResourceDemand,
    *,
    max_slots: int | None = None,
    ttl_s: int | None = None,
    now: datetime | None = None,
) -> Reservation | None:
    """Atomically reserve one slot in the per-cluster hash.

    Returns the persisted ``Reservation`` on success, ``None`` on
    contention (``HLEN >= max_slots`` and ``job_id`` doesn't already
    own a field). Re-reserving an existing ``job_id`` is treated as
    success and refreshes the JSON payload — safe for retry loops.

    ``ttl_s`` is the **hash TTL**, refreshed on every successful
    reservation, so a worker that dies mid-submit cannot pin a slot
    forever. Releasing in a ``finally`` block is the primary
    correctness mechanism; the TTL is the safety net.
    """
    from api.services.redis_clients import get_broker_redis_client

    cap = max_slots if max_slots is not None else max_slots_per_cluster()
    ttl = ttl_s if ttl_s is not None else slot_ttl_s()
    reserved_at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    reservation = Reservation(
        job_id=job_id,
        reserved_at=reserved_at,
        cpu_m=demand.cpu_m,
        mem_mib=demand.mem_mib,
    )
    client = get_broker_redis_client()
    key = slot_hash_key(cluster_name)
    try:
        result = client.eval(_RESERVE_LUA, 1, key, cap, job_id, reservation.to_json(), ttl)
    except Exception as exc:  # pragma: no cover - Redis failure path
        LOGGER.warning("capacity_gate.reserve_slot redis error: %s", type(exc).__name__)
        return None
    try:
        success = int(result) == 1
    except (TypeError, ValueError):
        success = False
    if not success:
        return None
    return reservation


def release_slot(cluster_name: str, job_id: str) -> bool:
    """Drop ``job_id`` from the cluster's slot hash. Idempotent.

    Returns True if a field was actually deleted, False otherwise
    (already released, or Redis unreachable). Never raises — release
    failures must not abort the submit task's success path.
    """
    from api.services.redis_clients import get_broker_redis_client

    client = get_broker_redis_client()
    key = slot_hash_key(cluster_name)
    try:
        removed = client.hdel(key, job_id)
    except Exception as exc:
        LOGGER.info("capacity_gate.release_slot skipped: %s", type(exc).__name__)
        return False
    try:
        return int(removed) > 0
    except (TypeError, ValueError):
        return False


def list_active_reservations(cluster_name: str) -> list[Reservation]:
    """Return every reservation currently held against this cluster.

    Decodes the JSON payloads back into ``Reservation`` instances and
    silently drops any entry that fails to parse — a malformed value
    is treated as "this slot is still held but accounting is lost",
    which is the conservative choice for an admission decision.
    Returns ``[]`` when the hash is empty or Redis is unreachable.
    """
    from api.services.redis_clients import get_broker_redis_client

    client = get_broker_redis_client()
    key = slot_hash_key(cluster_name)
    try:
        values = client.hvals(key)
    except Exception as exc:
        LOGGER.info("capacity_gate.list_active_reservations skipped: %s", type(exc).__name__)
        return []
    out: list[Reservation] = []
    for raw in values or []:
        res = Reservation.from_json(raw)
        if res is not None:
            out.append(res)
    return out


# ---------------------------------------------------------------------------
# In-process gate counters (Stage 5 telemetry)
# ---------------------------------------------------------------------------
#
# Per-cluster, per-event counters maintained inside the worker / api
# processes. Cheap (a dict update under a lock), zero external infra, and
# scoped to the lifetime of a Container Apps revision — which is exactly
# the operator-visible window. Surfaced through ``gate_counters_snapshot``
# so the /api/blast/capacity route can include them; the worker hook
# functions (``bump_admit``, ``bump_deny``, ``bump_release``,
# ``bump_reserve_lost``) are called from ``api.tasks.blast.submit_task``.
#
# Counters are intentionally NOT persisted: on revision restart the
# absolute numbers reset to 0 and operators read deltas from the
# dashboard's polling. Persistence would tie the gate to a backing
# store we already chose not to introduce.
_COUNTERS_LOCK = threading.Lock()
_COUNTERS: dict[str, dict[str, Any]] = {}


def _bucket(cluster: str) -> dict[str, Any]:
    bucket = _COUNTERS.get(cluster)
    if bucket is None:
        bucket = {
            "admit_total": 0,
            "deny_total": 0,
            "release_total": 0,
            "reserve_lost_total": 0,
            "deny_by_reason": {},
            "last_event_at": None,
        }
        _COUNTERS[cluster] = bucket
    return bucket


def bump_admit(cluster_name: str) -> None:
    with _COUNTERS_LOCK:
        bucket = _bucket(cluster_name or "_unknown")
        bucket["admit_total"] = int(bucket["admit_total"]) + 1
        bucket["last_event_at"] = datetime.now(UTC).isoformat(timespec="seconds")


def bump_deny(cluster_name: str, reason: str | None) -> None:
    label = (reason or "unknown").strip().lower() or "unknown"
    with _COUNTERS_LOCK:
        bucket = _bucket(cluster_name or "_unknown")
        bucket["deny_total"] = int(bucket["deny_total"]) + 1
        reasons = bucket["deny_by_reason"]
        reasons[label] = int(reasons.get(label, 0)) + 1
        bucket["last_event_at"] = datetime.now(UTC).isoformat(timespec="seconds")


def bump_release(cluster_name: str) -> None:
    with _COUNTERS_LOCK:
        bucket = _bucket(cluster_name or "_unknown")
        bucket["release_total"] = int(bucket["release_total"]) + 1
        bucket["last_event_at"] = datetime.now(UTC).isoformat(timespec="seconds")


def bump_reserve_lost(cluster_name: str) -> None:
    with _COUNTERS_LOCK:
        bucket = _bucket(cluster_name or "_unknown")
        bucket["reserve_lost_total"] = int(bucket["reserve_lost_total"]) + 1
        bucket["last_event_at"] = datetime.now(UTC).isoformat(timespec="seconds")


def gate_counters_snapshot(cluster_name: str) -> dict[str, Any]:
    """Read-only snapshot of the counters for one cluster.

    Returns the zero-filled defaults when nothing has happened yet so the
    SPA can render the cell without a degraded branch.
    """
    with _COUNTERS_LOCK:
        bucket = _COUNTERS.get(cluster_name)
        if bucket is None:
            return {
                "admit_total": 0,
                "deny_total": 0,
                "release_total": 0,
                "reserve_lost_total": 0,
                "deny_by_reason": {},
                "last_event_at": None,
            }
        # Defensive copy — the SPA receives JSON, the inner dict must not
        # leak the live mutable reference.
        return {
            "admit_total": int(bucket["admit_total"]),
            "deny_total": int(bucket["deny_total"]),
            "release_total": int(bucket["release_total"]),
            "reserve_lost_total": int(bucket["reserve_lost_total"]),
            "deny_by_reason": dict(bucket["deny_by_reason"]),
            "last_event_at": bucket["last_event_at"],
        }


def _reset_counters_for_tests() -> None:
    """Test helper — not exported."""
    with _COUNTERS_LOCK:
        _COUNTERS.clear()


__all__ = (
    "GATE_DEFAULT_CPU_WATERMARK_PCT",
    "GATE_DEFAULT_DEMAND_CPU_M",
    "GATE_DEFAULT_DEMAND_MEM_MIB",
    "GATE_DEFAULT_MAX_SLOTS_PER_CLUSTER",
    "GATE_DEFAULT_MEM_WATERMARK_PCT",
    "GATE_DEFAULT_POOL_NAME",
    "GATE_DEFAULT_SLOT_TTL_S",
    "GATE_SLOT_HASH_PREFIX",
    "GateDecision",
    "Reservation",
    "ResourceDemand",
    "bump_admit",
    "bump_deny",
    "bump_release",
    "bump_reserve_lost",
    "cpu_watermark_pct",
    "default_demand_cpu_m",
    "default_demand_mem_mib",
    "evaluate_capacity_gate",
    "gate_counters_snapshot",
    "list_active_reservations",
    "max_slots_per_cluster",
    "mem_watermark_pct",
    "predict_demand",
    "release_slot",
    "reserve_slot",
    "slot_hash_key",
    "slot_ttl_s",
)

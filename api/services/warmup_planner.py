"""Warmup feasibility planner - pure-python, side-effect free.

Responsibility: Warmup feasibility planner - pure-python, side-effect free
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `WarmupPlan`, `compute_warmup_feasibility`, `_refusal`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from typing import Any, Literal

from api.services.aks_skus import DEFAULT_SKU, SKU_BY_NAME
from api.services.db_sharding import (
    PRESET_SHARD_SETS,
    SAFE_SHARD_FRACTION_OF_NODE_RAM,
    select_partitions_for_submit,
)

LOGGER = logging.getLogger(__name__)


# Honest verdict codes consumed by the SPA. Keep the set closed and
# documented so the UI can map each code to a coloured banner without
# guessing at the backend's intent.
WarmupStatus = Literal[
    "ok",  # warmup is safe to start as-is
    "ok_unknown_sku",  # warmup is safe but SKU lookup fell back to a default RAM
    "no_db_size",  # planner refused: we don't know how big the DB is
    "no_nodes",  # planner refused: cluster has 0 BLAST nodes
    "node_sku_too_small",  # per-pod budget exceeded even after clamping to MAX preset
    "cluster_too_small",  # per-node budget exceeded; user must add nodes
]

# Fallback per-node RAM when the SKU is not in our catalog. Conservative
# value (matches Standard_E16s_v5) so unknown SKUs do not get an
# over-optimistic feasibility verdict.
_FALLBACK_NODE_RAM_GIB: int = 64

# Largest shard count the layout layer can produce. Mirrors
# ``PRESET_SHARD_SETS[-1]`` so a future bump there propagates here.
_MAX_PRESET_SHARDS: int = max(PRESET_SHARD_SETS)


@dataclass(frozen=True, slots=True)
class WarmupPlan:
    """Result of ``compute_warmup_feasibility`` — fully serialisable.

    ``feasible`` is the headline boolean for the UI. ``status`` carries
    the precise reason; ``message`` and ``recommendations`` are the
    human-readable text the SPA can render verbatim. All numeric fields
    are rounded to a single decimal place to keep the JSON payload
    stable across runs and avoid spurious ``-0.0`` / FP drift.
    """

    feasible: bool
    status: WarmupStatus
    message: str

    # Cluster topology echoed back so the SPA can render a self-contained
    # banner without re-querying ``/api/monitor/aks``.
    num_nodes: int
    machine_type: str
    node_ram_gib: float
    safe_node_budget_gib: float

    # Database stats.
    db_total_bytes: int
    db_gib: float

    # Sharding decision (what would be selected at submit time).
    chosen_shards: int  # what ``select_partitions_for_submit`` returns now
    target_shards: int  # ideal N before clamping (may exceed presets)
    per_shard_gib: float
    per_node_gib: float  # db_gib / num_nodes — independent of N
    shards_per_node: int  # ceil(chosen_shards / num_nodes)

    # Ordered list of remediation strings; the first entry is the cheapest.
    recommendations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation for inclusion in API responses."""
        d = asdict(self)
        # asdict turns the recommendations tuple into a tuple, which json
        # can serialise but list[str] is the contract on the wire.
        d["recommendations"] = list(self.recommendations)
        return d


def compute_warmup_feasibility(
    *,
    db_total_bytes: int,
    num_nodes: int,
    machine_type: str = DEFAULT_SKU,
) -> WarmupPlan:
    """Plan a DaemonSet warmup for ``db_total_bytes`` on ``num_nodes`` × ``machine_type``.

    Returns a :class:`WarmupPlan` with ``feasible`` set and a precise
    ``status`` code. **Never raises** for caller-supplied inputs that
    are merely insufficient for warmup — those become a non-feasible
    plan with a status code and remediation steps. ``ValueError`` is
    reserved for the degenerate ``db_total_bytes < 0`` case (programmer
    error).

    Side effects: emits one DEBUG log line. Does not touch storage or
    Kubernetes.
    """
    if db_total_bytes < 0:
        raise ValueError(f"db_total_bytes must be non-negative, got {db_total_bytes}")
    if num_nodes < 0:
        raise ValueError(f"num_nodes must be non-negative, got {num_nodes}")

    sku = SKU_BY_NAME.get(machine_type)
    sku_known = sku is not None
    node_ram_gib_raw = sku.memory_gib if sku is not None else _FALLBACK_NODE_RAM_GIB
    node_ram_gib = float(node_ram_gib_raw)
    safe_node_budget_gib = round(node_ram_gib * SAFE_SHARD_FRACTION_OF_NODE_RAM, 1)

    db_gib = round(db_total_bytes / float(1024**3), 2)

    # ------------------------------------------------------------------
    # Degenerate inputs first — bail with an honest status code rather
    # than a generic "feasible=true with chosen_shards=1" answer.
    # ------------------------------------------------------------------
    if db_total_bytes == 0:
        return _refusal(
            status="no_db_size",
            message="Database size is unknown or zero — cannot plan warmup yet.",
            recommendations=(
                "Wait for the download step to finish so size metadata is available.",
            ),
            db_total_bytes=db_total_bytes,
            db_gib=db_gib,
            num_nodes=num_nodes,
            machine_type=machine_type,
            node_ram_gib=node_ram_gib,
            safe_node_budget_gib=safe_node_budget_gib,
        )

    if num_nodes == 0:
        return _refusal(
            status="no_nodes",
            message="Cluster has no BLAST nodes — provision a blastpool before warmup.",
            recommendations=(
                "Provision an AKS cluster with at least one blastpool node "
                f"(default SKU: {DEFAULT_SKU}).",
            ),
            db_total_bytes=db_total_bytes,
            db_gib=db_gib,
            num_nodes=num_nodes,
            machine_type=machine_type,
            node_ram_gib=node_ram_gib,
            safe_node_budget_gib=safe_node_budget_gib,
        )

    # ------------------------------------------------------------------
    # Sharding decision — delegate to the existing v3-validated picker
    # so warmup-time and submit-time stay in lockstep.
    # ------------------------------------------------------------------
    chosen_shards = select_partitions_for_submit(
        db_total_bytes=db_total_bytes,
        num_nodes=num_nodes,
        machine_type=machine_type,
    )
    # Re-derive the *un-clamped* target so the UI can say "you'd ideally
    # need 12 shards but presets only go up to 10".
    target_shards_by_memory = max(
        1,
        math.ceil(db_gib / max(1.0, safe_node_budget_gib)),
    )
    target_shards = max(num_nodes, target_shards_by_memory)

    per_shard_gib = round(db_gib / chosen_shards, 2)
    per_node_gib = round(db_gib / num_nodes, 2)
    shards_per_node = max(1, math.ceil(chosen_shards / num_nodes))

    # ------------------------------------------------------------------
    # Constraint checks. The order matters: report the most fundamental
    # blocker first so the user fixes the right thing.
    # ------------------------------------------------------------------
    per_pod_overflow = per_shard_gib > safe_node_budget_gib
    per_node_overflow = per_node_gib > safe_node_budget_gib

    if per_pod_overflow and chosen_shards >= _MAX_PRESET_SHARDS:
        # We picked the largest preset and a single pod still wouldn't
        # fit. Adding nodes will not help — only a bigger SKU.
        recommendations = _sku_upgrade_recommendations(
            required_per_node_gib=db_gib / _MAX_PRESET_SHARDS,
            current_machine=machine_type,
        )
        return WarmupPlan(
            feasible=False,
            status="node_sku_too_small",
            message=(
                f"DB shard size {per_shard_gib:.1f} GiB exceeds the safe per-node "
                f"budget {safe_node_budget_gib:.1f} GiB even after splitting into "
                f"the maximum {_MAX_PRESET_SHARDS} shards. Adding nodes will not help "
                f"— upgrade the blastpool SKU."
            ),
            num_nodes=num_nodes,
            machine_type=machine_type,
            node_ram_gib=node_ram_gib,
            safe_node_budget_gib=safe_node_budget_gib,
            db_total_bytes=db_total_bytes,
            db_gib=db_gib,
            chosen_shards=chosen_shards,
            target_shards=target_shards,
            per_shard_gib=per_shard_gib,
            per_node_gib=per_node_gib,
            shards_per_node=shards_per_node,
            recommendations=recommendations,
        )

    if per_node_overflow:
        # Shard size itself fits, but with the current node count one
        # node would page-cache more than the safe budget allows.
        # Adding nodes lowers per-node pressure linearly.
        required_nodes = max(1, math.ceil(db_gib / max(1.0, safe_node_budget_gib)))
        # SKU upgrade path keeps node count fixed: a node must hold
        # ``db_gib / num_nodes`` of data in its safe budget.
        sku_suggestions = _sku_upgrade_recommendations(
            required_per_node_gib=db_gib / num_nodes,
            current_machine=machine_type,
        )
        recommendations = (
            f"Increase blastpool node count from {num_nodes} to at least "
            f"{required_nodes} (each node would then host ≈ "
            f"{db_gib / required_nodes:.1f} GiB of {machine_type}'s "
            f"{node_ram_gib:.0f} GiB RAM).",
            *sku_suggestions,
        )
        return WarmupPlan(
            feasible=False,
            status="cluster_too_small",
            message=(
                f"Per-node memory pressure {per_node_gib:.1f} GiB exceeds the safe "
                f"budget {safe_node_budget_gib:.1f} GiB on {num_nodes} × {machine_type}. "
                f"Add nodes (cheapest) or upgrade the SKU."
            ),
            num_nodes=num_nodes,
            machine_type=machine_type,
            node_ram_gib=node_ram_gib,
            safe_node_budget_gib=safe_node_budget_gib,
            db_total_bytes=db_total_bytes,
            db_gib=db_gib,
            chosen_shards=chosen_shards,
            target_shards=target_shards,
            per_shard_gib=per_shard_gib,
            per_node_gib=per_node_gib,
            shards_per_node=shards_per_node,
            recommendations=recommendations,
        )

    # ------------------------------------------------------------------
    # Feasible.
    # ------------------------------------------------------------------
    status: WarmupStatus = "ok_unknown_sku" if not sku_known else "ok"
    message = (
        f"Warmup feasible: {chosen_shards}-shard layout, ≈ {per_shard_gib:.1f} GiB "
        f"per pod and ≈ {per_node_gib:.1f} GiB per node "
        f"(safe budget {safe_node_budget_gib:.1f} GiB on {machine_type})."
    )
    if not sku_known:
        message += (
            f" SKU {machine_type!r} is not in the catalog; assumed "
            f"{int(node_ram_gib)} GiB per node."
        )

    LOGGER.debug(
        "warmup_plan db_gib=%.2f nodes=%d sku=%s -> chosen=%d feasible=%s",
        db_gib,
        num_nodes,
        machine_type,
        chosen_shards,
        True,
    )
    return WarmupPlan(
        feasible=True,
        status=status,
        message=message,
        num_nodes=num_nodes,
        machine_type=machine_type,
        node_ram_gib=node_ram_gib,
        safe_node_budget_gib=safe_node_budget_gib,
        db_total_bytes=db_total_bytes,
        db_gib=db_gib,
        chosen_shards=chosen_shards,
        target_shards=target_shards,
        per_shard_gib=per_shard_gib,
        per_node_gib=per_node_gib,
        shards_per_node=shards_per_node,
        recommendations=(),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _refusal(
    *,
    status: WarmupStatus,
    message: str,
    recommendations: tuple[str, ...],
    db_total_bytes: int,
    db_gib: float,
    num_nodes: int,
    machine_type: str,
    node_ram_gib: float,
    safe_node_budget_gib: float,
) -> WarmupPlan:
    """Build a non-feasible plan for degenerate inputs (no nodes, no size)."""
    return WarmupPlan(
        feasible=False,
        status=status,
        message=message,
        num_nodes=num_nodes,
        machine_type=machine_type,
        node_ram_gib=node_ram_gib,
        safe_node_budget_gib=safe_node_budget_gib,
        db_total_bytes=db_total_bytes,
        db_gib=db_gib,
        chosen_shards=0,
        target_shards=0,
        per_shard_gib=0.0,
        per_node_gib=0.0,
        shards_per_node=0,
        recommendations=recommendations,
    )


def _sku_upgrade_recommendations(
    *, required_per_node_gib: float, current_machine: str
) -> tuple[str, ...]:
    """Suggest concrete SKU upgrades that satisfy a target per-node budget.

    ``required_per_node_gib`` is how much DB data one *node* (not pod)
    would have to page-cache after the upgrade — the caller picks the
    semantic (``db / max_preset`` for ``node_sku_too_small`` failures,
    ``db / num_nodes`` for ``cluster_too_small`` failures).

    Walks the catalog looking for blast-pool eligible SKUs whose safe
    per-node budget is at least ``required_per_node_gib`` AND whose RAM
    is strictly larger than the current SKU (so we never recommend a
    *downgrade* — the calling failure mode is "out of RAM"). Returns up
    to two cheapest options. Empty tuple if nothing in the catalog is
    large enough.
    """
    current = SKU_BY_NAME.get(current_machine)
    current_ram = current.memory_gib if current is not None else 0
    candidates: list[tuple[float, str, int]] = []  # (hourly, name, ram)
    for entry in SKU_BY_NAME.values():
        if entry.role not in ("blast", "both"):
            continue
        if entry.name == current_machine:
            continue
        if entry.memory_gib <= current_ram:
            # Never recommend a downgrade or sidegrade.
            continue
        safe_budget = entry.memory_gib * SAFE_SHARD_FRACTION_OF_NODE_RAM
        if safe_budget < required_per_node_gib:
            continue
        candidates.append((entry.hourly_usd, entry.name, entry.memory_gib))

    candidates.sort(key=lambda t: (t[0], t[1]))
    suggestions: list[str] = []
    for _hourly, name, ram in candidates[:2]:
        suggestions.append(f"Upgrade blastpool SKU to {name} ({ram} GiB RAM per node).")
    return tuple(suggestions)

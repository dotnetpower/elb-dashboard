"""Planning helpers used by Storage warmup Celery tasks.

Responsibility: Planning helpers used by Storage warmup Celery tasks
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `program_to_mol_type`, `available_shard_sets`, `select_warmup_shard_count`,
`build_elb_image`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

from typing import Any


def program_to_mol_type(program: str, database_name: str) -> str:
    lowered_program = program.lower()
    lowered_db = database_name.lower()
    if lowered_program in {"blastp", "blastx"}:
        return "prot"
    if lowered_db in {"nr", "swissprot", "pdbaa", "refseq_protein"}:
        return "prot"
    return "nucl"


def available_shard_sets(database: dict[str, Any]) -> list[int]:
    shard_sets = database.get("shard_sets") or []
    out: list[int] = []
    if isinstance(shard_sets, list):
        for value in shard_sets:
            try:
                shard_count = int(value)
            except (TypeError, ValueError):
                continue
            if shard_count > 0:
                out.append(shard_count)
    return sorted(set(out))


def select_warmup_shard_count(
    *,
    database: dict[str, Any],
    node_count: int,
    machine_type: str,
) -> int:
    shard_sets = [value for value in available_shard_sets(database) if value <= node_count]
    if not shard_sets:
        raise RuntimeError(
            "database has no shard set that fits the current Ready warmup node count"
        )

    total_bytes = int(database.get("total_bytes") or database.get("bytes_total") or 0)
    if total_bytes > 0:
        from api.services.warmup.planner import compute_warmup_feasibility

        plan = compute_warmup_feasibility(
            db_total_bytes=total_bytes,
            num_nodes=node_count,
            machine_type=machine_type,
        )
        if not plan.feasible:
            raise RuntimeError(f"warmup is not feasible: {plan.message}")
        if plan.chosen_shards in shard_sets:
            return plan.chosen_shards
        smaller_or_equal = [value for value in shard_sets if value <= plan.chosen_shards]
        if smaller_or_equal:
            return max(smaller_or_equal)
    return max(shard_sets)


def build_elb_image(acr_name: str) -> str:
    from api.services.image_tags import IMAGE_TAGS

    clean_acr = acr_name.strip().lower()
    if not clean_acr:
        raise RuntimeError("acr_name is required for node-local warmup Jobs")
    return f"{clean_acr}.azurecr.io/ncbi/elb:{IMAGE_TAGS['ncbi/elb']}"

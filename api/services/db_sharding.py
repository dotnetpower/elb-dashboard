"""Compatibility wrapper for `api.services.db.sharding`.

Responsibility: Re-export `api.services.db.sharding` at the legacy flat path.
Edit boundaries: Implementation lives in `api.services.db.sharding`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_db_sharding.py`.
"""

from api.services.db.sharding import (
    AKS_LOCAL_DB_DIR,
    DEFAULT_CONTAINER,
    MAX_SHARDS,
    PRESET_SHARD_SETS,
    SAFE_SHARD_FRACTION_OF_NODE_RAM,
    ShardLayout,
    ShardUploadResult,
    derive_volumes_from_keys,
    ensure_shard_sets,
    list_db_volumes,
    partition_prefix_for,
    plan_shard_layout,
    read_blastdb_stats,
    render_manifest,
    render_nal,
    select_partitions_for_submit,
    shard_sets_present,
    upload_shard_set,
)

__all__ = [
    "AKS_LOCAL_DB_DIR",
    "DEFAULT_CONTAINER",
    "MAX_SHARDS",
    "PRESET_SHARD_SETS",
    "SAFE_SHARD_FRACTION_OF_NODE_RAM",
    "ShardLayout",
    "ShardUploadResult",
    "derive_volumes_from_keys",
    "ensure_shard_sets",
    "list_db_volumes",
    "partition_prefix_for",
    "plan_shard_layout",
    "read_blastdb_stats",
    "render_manifest",
    "render_nal",
    "select_partitions_for_submit",
    "shard_sets_present",
    "upload_shard_set",
]

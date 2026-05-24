"""Compatibility wrapper for `api.services.blast.task_config`.

Responsibility: Compatibility wrapper for `api.services.blast.task_config`
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: Module import side effects and constants.
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from api.services.blast.task_config import (
    BlastDatabaseAvailabilityError,
    WarmupNotReadyError,
    build_config_content,
    disable_sharding_options,
    ensure_node_warmup_ready_for_submit,
    metadata_has_prepared_shard_layout,
    normalise_database_url,
    normalise_query_url,
    option_enabled,
    query_blob_path_from_query_file,
    relative_blob_path,
    results_job_url,
    snippet,
    storage_url,
    submit_requires_node_warmup,
    suppress_sharding_for_unsharded_database,
    validate_blast_database_available,
    validate_storage_blob_reference,
)

__all__ = [
    "BlastDatabaseAvailabilityError",
    "WarmupNotReadyError",
    "build_config_content",
    "disable_sharding_options",
    "ensure_node_warmup_ready_for_submit",
    "metadata_has_prepared_shard_layout",
    "normalise_database_url",
    "normalise_query_url",
    "option_enabled",
    "query_blob_path_from_query_file",
    "relative_blob_path",
    "results_job_url",
    "snippet",
    "storage_url",
    "submit_requires_node_warmup",
    "suppress_sharding_for_unsharded_database",
    "validate_blast_database_available",
    "validate_storage_blob_reference",
]

"""Compatibility wrapper for `api.services.blast.task_config`."""

from api.services.blast.task_config import (
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
)

__all__ = [
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
]

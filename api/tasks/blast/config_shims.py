"""Test-monkeypatch-friendly aliases over ``api.services.blast.task_config``.

Responsibility: Thin alias layer over ``blast.task_config`` (URL builders, INI config
content, option normalisers, sharding suppression, node-warmup gate) so tests can
monkeypatch ``blast._X`` and so cross-submodule callers can stage shared logic without
reimporting ``_blast_task_config`` everywhere.
Edit boundaries: Each public function is one or two lines that delegate to
``api.services.blast.task_config`` (or ``api.services.blast.db_metadata.resolve_db_metadata``
for the warmup / config builders). New domain logic belongs in those service modules,
not here.
Key entry points: ``_storage_url``, ``_relative_blob_path``, ``_normalise_query_url``,
``_query_blob_path_from_query_file``, ``_normalise_database_url``, ``_results_job_url``,
``_build_config_content``, ``_option_enabled``, ``_metadata_has_prepared_shard_layout``,
``_disable_sharding_options``, ``_expand_strict_tie_order_candidate_pool``,
``_suppress_sharding_for_unsharded_database``, ``_submit_requires_node_warmup``,
``_ensure_node_warmup_ready_for_submit``.
Risky contracts: ``_expand_strict_tie_order_candidate_pool`` rewrites
``max_target_seqs`` when a strict tie-order oracle is requested; the pool size is set by
``STRICT_TIE_ORDER_MIN_TARGET_SEQS`` (re-exported from ``split_constants``).
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from api.services.blast import task_config as _blast_task_config
from api.services.blast.task_config import BlastDatabaseAvailabilityError
from api.tasks import blast as _blast
from api.tasks.blast.split_constants import STRICT_TIE_ORDER_MIN_TARGET_SEQS


def _storage_url(storage_account: str, container: str, path: str = "") -> str:
    return _blast_task_config.storage_url(storage_account, container, path)


def _relative_blob_path(value: str, label: str) -> str:
    return _blast_task_config.relative_blob_path(value, label)


def _normalise_query_url(storage_account: str, query_file: str) -> str:
    return _blast_task_config.normalise_query_url(storage_account, query_file)


def _query_blob_path_from_query_file(*, storage_account: str, query_file: str) -> str:
    """Return a safe blob path in the queries container for an original query file."""
    return _blast_task_config.query_blob_path_from_query_file(
        storage_account=storage_account,
        query_file=query_file,
    )


def _normalise_database_url(storage_account: str, database: str) -> str:
    return _blast_task_config.normalise_database_url(storage_account, database)


def _validate_blast_database_available(
    *, storage_account: str, database: str
) -> dict[str, str]:
    return _blast_task_config.validate_blast_database_available(
        storage_account=storage_account,
        database=database,
    )


def _results_job_url(storage_account: str, job_id: str) -> str:
    return _blast_task_config.results_job_url(storage_account, job_id)


def _build_config_content(
    *,
    job_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
    program: str = "blastn",
    database: str = "",
    query_file: str = "",
    options: Mapping[str, Any] | None = None,
) -> str:
    return _blast_task_config.build_config_content(
        job_id=job_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        storage_account=storage_account,
        program=program,
        database=database,
        query_file=query_file,
        options=options,
        metadata_resolver=_blast.resolve_db_metadata,
    )


def _option_enabled(options: Mapping[str, Any], key: str) -> bool:
    return _blast_task_config.option_enabled(options, key)


def _metadata_has_prepared_shard_layout(db_name: str, meta: Mapping[str, Any]) -> bool:
    return _blast_task_config.metadata_has_prepared_shard_layout(db_name, meta)


def _disable_sharding_options(options: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return _blast_task_config.disable_sharding_options(options)


def _expand_strict_tie_order_candidate_pool(
    options: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(options, Mapping):
        return None if options is None else dict(options)
    has_oracle = bool(
        options.get("tie_order_oracle_accessions") or options.get("tie_order_oracle_text")
    )
    if not has_oracle or not _option_enabled(options, "tie_order_oracle_strict"):
        return cast(dict[str, Any], options)
    current_raw = options.get("max_target_seqs")
    try:
        current = int(current_raw) if current_raw not in (None, "") else 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        current = 0
    if current >= STRICT_TIE_ORDER_MIN_TARGET_SEQS:
        return dict(options)
    expanded = dict(options)
    expanded["requested_max_target_seqs"] = current_raw
    expanded["max_target_seqs"] = STRICT_TIE_ORDER_MIN_TARGET_SEQS
    return expanded


def _suppress_sharding_for_unsharded_database(
    *,
    storage_account: str,
    database: str,
    options: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    return _blast_task_config.suppress_sharding_for_unsharded_database(
        storage_account=storage_account,
        database=database,
        options=options,
        metadata_resolver=_blast.resolve_db_metadata,
    )


def _submit_requires_node_warmup(options: Mapping[str, Any] | None) -> bool:
    return _blast_task_config.submit_requires_node_warmup(options)


def _ensure_node_warmup_ready_for_submit(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    database: str,
    storage_account: str = "",
    options: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    return _blast_task_config.ensure_node_warmup_ready_for_submit(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        database=database,
        storage_account=storage_account,
        options=options,
        metadata_resolver=_blast.resolve_db_metadata,
    )


__all__ = (
    "BlastDatabaseAvailabilityError",
    "_build_config_content",
    "_disable_sharding_options",
    "_ensure_node_warmup_ready_for_submit",
    "_expand_strict_tie_order_candidate_pool",
    "_metadata_has_prepared_shard_layout",
    "_normalise_database_url",
    "_normalise_query_url",
    "_option_enabled",
    "_query_blob_path_from_query_file",
    "_relative_blob_path",
    "_results_job_url",
    "_storage_url",
    "_submit_requires_node_warmup",
    "_suppress_sharding_for_unsharded_database",
    "_validate_blast_database_available",
)

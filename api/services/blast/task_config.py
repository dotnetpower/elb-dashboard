"""BLAST task config building and submit readiness helpers.

Responsibility: BLAST task config building and submit readiness helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `WarmupNotReadyError`, `snippet`, `storage_url`, `relative_blob_path`,
`validate_storage_blob_reference`, `normalise_query_url`, `query_blob_path_from_query_file`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from api.services.blast.db_metadata import extract_db_name, resolve_db_metadata
from api.services.storage.url_validation import (
    validate_storage_blob_reference as validate_storage_blob_reference,
)

ERROR_SNIPPET_CHARS = 500


class WarmupNotReadyError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class BlastDatabaseAvailabilityError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def snippet(value: object, limit: int = ERROR_SNIPPET_CHARS) -> str:
    return str(value or "")[:limit]


def storage_url(storage_account: str, container: str, path: str = "") -> str:
    suffix = path.strip("/")
    from api.services.storage.endpoint import blob_account_url

    base = f"{blob_account_url(storage_account)}/{container}"
    return f"{base}/{suffix}" if suffix else base


def relative_blob_path(value: str, label: str) -> str:
    path = value.strip().lstrip("/")
    if not path or any(part == ".." for part in path.split("/")):
        raise ValueError(f"{label} must be a relative blob path without '..'")
    return path


def normalise_query_url(storage_account: str, query_file: str) -> str:
    query = query_file.strip()
    absolute = validate_storage_blob_reference(
        storage_account=storage_account,
        value=query,
        label="query_file",
        expected_container="queries",
    )
    if absolute is not None:
        return absolute
    if query.startswith("queries/"):
        return storage_url(
            storage_account,
            "queries",
            relative_blob_path(query.removeprefix("queries/"), "query_file"),
        )
    return storage_url(storage_account, "queries", relative_blob_path(query, "query_file"))


def query_blob_path_from_query_file(*, storage_account: str, query_file: str) -> str:
    raw = query_file.strip()
    if not raw:
        raise ValueError("query_file is required")
    if raw.startswith("/") or raw.startswith("//") or "?" in raw or "#" in raw:
        raise ValueError("query_file must be a relative queries blob path without query strings")

    if raw.startswith("az://"):
        raw = "https://" + raw.removeprefix("az://")

    if raw.startswith("https://"):
        parsed = urlparse(raw)
        from api.services.storage.endpoint import blob_host_for_account

        expected_host = blob_host_for_account(storage_account)
        if (parsed.hostname or "").lower() != expected_host.lower():
            raise ValueError("query_file URL must belong to the selected Storage account")
        parts = parsed.path.lstrip("/").split("/", 1)
        if len(parts) != 2 or parts[0] != "queries" or not parts[1]:
            raise ValueError("query_file URL must point to the queries container")
        blob_path = parts[1]
    else:
        blob_path = raw.removeprefix("queries/")

    if blob_path.startswith("split/") or "/split/" in blob_path:
        raise ValueError("query_file must be the original query, not a split query blob")
    return relative_blob_path(blob_path, "query_file")


def normalise_database_url(storage_account: str, database: str) -> str:
    db = database.strip()
    absolute = validate_storage_blob_reference(
        storage_account=storage_account,
        value=db,
        label="database",
        expected_container="blast-db",
    )
    if absolute is not None:
        return absolute
    if db.startswith("blast-db/"):
        return storage_url(
            storage_account,
            "blast-db",
            relative_blob_path(db.removeprefix("blast-db/"), "database"),
        )
    if "/" in db:
        return storage_url(storage_account, "blast-db", relative_blob_path(db, "database"))
    db_name = relative_blob_path(db, "database")
    return storage_url(
        storage_account,
        "blast-db",
        f"{db_name}/{db_name}",
    )


_BLAST_DB_READY_SUFFIXES = (".nsq", ".psq", ".nal", ".pal")


def validate_blast_database_available(
    *,
    storage_account: str,
    database: str,
) -> dict[str, str]:
    """Fail fast unless the selected BLAST database prefix exists in Storage."""

    if not storage_account:
        raise BlastDatabaseAvailabilityError(
            "Storage account is required before submitting a BLAST job.",
            code="storage_account_required",
        )
    if not database:
        raise BlastDatabaseAvailabilityError(
            "A BLAST database must be selected before submitting a BLAST job.",
            code="database_required",
        )

    try:
        db_url = normalise_database_url(storage_account, database)
    except ValueError as exc:
        raise BlastDatabaseAvailabilityError(str(exc), code="invalid_database_reference") from exc

    parsed = urlparse(db_url)
    parts = parsed.path.lstrip("/").split("/", 1)
    if len(parts) != 2 or parts[0] != "blast-db" or not parts[1].strip("/"):
        raise BlastDatabaseAvailabilityError(
            "Database URL must point to a blob prefix in the blast-db container.",
            code="invalid_database_reference",
        )

    container_name = parts[0]
    blob_prefix = parts[1].strip("/")
    try:
        from api.services import get_credential
        from api.services.storage.data import _blob_service

        container = _blob_service(get_credential(), storage_account).get_container_client(
            container_name
        )
        # BLAST DB values are prefixes, e.g. ``core_nt/core_nt``. A valid
        # nucleotide/protein DB has either sequence-volume files under that
        # prefix (``.nsq`` / ``.psq``) or an alias file (``.nal`` / ``.pal``).
        for blob in container.list_blobs(name_starts_with=blob_prefix):
            name = str(getattr(blob, "name", "") or "")
            if name == blob_prefix and name.endswith(_BLAST_DB_READY_SUFFIXES):
                return {
                    "container": container_name,
                    "blob_prefix": blob_prefix,
                    "marker_blob": name,
                }
            if not name.startswith(f"{blob_prefix}."):
                continue
            if name.endswith(_BLAST_DB_READY_SUFFIXES):
                return {
                    "container": container_name,
                    "blob_prefix": blob_prefix,
                    "marker_blob": name,
                }
    except BlastDatabaseAvailabilityError:
        raise
    except Exception as exc:
        db_label = extract_db_name(database) or database
        raise BlastDatabaseAvailabilityError(
            f"Could not verify BLAST database '{db_label}' in Storage: "
            f"{type(exc).__name__}.",
            code="database_check_unavailable",
        ) from exc

    db_name = extract_db_name(database) or database
    raise BlastDatabaseAvailabilityError(
        f"BLAST database '{db_name}' is not available in Storage. "
        f"Expected BLAST DB files under blast-db/{blob_prefix}*. "
        "Download or prepare this database before submitting.",
        code="database_not_found",
    )


def results_job_url(storage_account: str, job_id: str) -> str:
    return storage_url(storage_account, "results", relative_blob_path(job_id, "job_id"))


def metadata_has_prepared_shard_layout(db_name: str, meta: Mapping[str, Any]) -> bool:
    metadata_db_name = str(meta.get("db_name") or "")
    if metadata_db_name and metadata_db_name != db_name:
        return False
    if meta.get("update_in_progress") or meta.get("sharding_in_progress"):
        return False
    if meta.get("shards_stale"):
        return False
    shard_sets = meta.get("shard_sets")
    if not bool(meta.get("sharded")) or not isinstance(shard_sets, list):
        return False
    has_partitioned_layout = False
    for value in shard_sets:
        try:
            has_partitioned_layout = int(value) > 1
        except (TypeError, ValueError):
            has_partitioned_layout = False
        if has_partitioned_layout:
            break
    if not has_partitioned_layout:
        return False
    source_version = str(meta.get("source_version") or "")
    shard_source_version = str(meta.get("shard_source_version") or source_version or "")
    return not (source_version and shard_source_version != source_version)


def build_config_content(
    *,
    job_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
    program: str = "blastn",
    database: str = "",
    query_file: str = "",
    options: Mapping[str, Any] | None = None,
    metadata_resolver: Callable[[str, str], Mapping[str, Any] | None] = resolve_db_metadata,
) -> str:
    from api.services.blast.config import generate_config

    params: dict[str, Any] = {
        "job_id": job_id,
        "resource_group": resource_group,
        "aks_cluster_name": cluster_name,
        "storage_account": storage_account,
        "program": program,
        "db": normalise_database_url(storage_account, database) if database else "",
        "query_blob_url": normalise_query_url(storage_account, query_file) if query_file else "",
        "results_url": results_job_url(storage_account, job_id),
        "reuse": True,
    }
    if options:
        params.update(dict(options))

    if database and storage_account:
        db_name = extract_db_name(database)
        meta = metadata_resolver(storage_account, db_name)
        if meta is not None:
            params.setdefault("db_name", db_name)
            if metadata_has_prepared_shard_layout(db_name, meta):
                params.setdefault("db_sharded", True)
            else:
                params["db_sharded"] = False
                params["db_auto_partition"] = False
                params["sharding_mode"] = "off"
                params["disable_sharding"] = True
                params.pop("db_partitions", None)
                params.pop("db_partition_prefix", None)
            tb = meta.get("total_bytes")
            if isinstance(tb, (int, float)) and tb > 0:
                params.setdefault("db_total_bytes", int(tb))
            for source_key, target_key in (
                ("total_letters", "db_total_letters"),
                ("number_of_letters", "db_total_letters"),
                ("number-of-letters", "db_total_letters"),
                ("effective_search_space", "db_effective_search_space"),
                ("db_effective_search_space", "db_effective_search_space"),
            ):
                value = meta.get(source_key)
                if isinstance(value, (int, float)) and value > 0:
                    params.setdefault(target_key, int(value))

    return generate_config(params)


def option_enabled(options: Mapping[str, Any], key: str) -> bool:
    value = options.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def disable_sharding_options(options: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(options, Mapping):
        return None
    adjusted = dict(options)
    adjusted["db_auto_partition"] = False
    adjusted["db_sharded"] = False
    adjusted["disable_sharding"] = True
    adjusted["sharding_mode"] = "off"
    adjusted.pop("db_partitions", None)
    adjusted.pop("db_partition_prefix", None)
    return adjusted


def suppress_sharding_for_unsharded_database(
    *,
    storage_account: str,
    database: str,
    options: Mapping[str, Any] | None,
    metadata_resolver: Callable[[str, str], Mapping[str, Any] | None] = resolve_db_metadata,
) -> dict[str, Any] | None:
    if not isinstance(options, Mapping) or not storage_account or not database:
        return dict(options) if isinstance(options, Mapping) else None
    db_name = extract_db_name(database)
    meta = metadata_resolver(storage_account, db_name)
    if meta is None:
        return dict(options)
    if metadata_has_prepared_shard_layout(db_name, meta):
        return dict(options)
    return disable_sharding_options(options)


def submit_requires_node_warmup(options: Mapping[str, Any] | None) -> bool:
    if not isinstance(options, Mapping):
        return False
    if options.get("enable_warmup") is False:
        return False
    from api.services.sharding_precision import normalize_sharding_mode

    return normalize_sharding_mode(options) != "off" or option_enabled(
        options,
        "db_auto_partition",
    )


def ensure_node_warmup_ready_for_submit(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    database: str,
    storage_account: str = "",
    options: Mapping[str, Any] | None,
    metadata_resolver: Callable[[str, str], Mapping[str, Any] | None] = resolve_db_metadata,
) -> dict[str, Any] | None:
    db_name = extract_db_name(database)
    meta = metadata_resolver(storage_account, db_name) if storage_account and db_name else None
    options = suppress_sharding_for_unsharded_database(
        storage_account=storage_account,
        database=database,
        options=options,
        metadata_resolver=lambda *_args: meta,
    )
    if not submit_requires_node_warmup(options):
        return None
    if not db_name:
        raise WarmupNotReadyError(
            "node warmup readiness cannot be checked without a database name",
            retryable=False,
        )
    storage_source_version = str(meta.get("source_version") or "") if meta else ""
    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_warmup_status

        credential = get_credential()
        status = k8s_warmup_status(
            credential,
            subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID", ""),
            resource_group,
            cluster_name,
        )
    except Exception as exc:
        raise WarmupNotReadyError(
            f"node warmup readiness check failed: {snippet(exc)}",
            retryable=True,
        ) from exc

    for item in status.get("databases", []):
        if not isinstance(item, Mapping) or item.get("name") != db_name:
            continue
        db_status = str(item.get("status") or "Unknown")
        if db_status == "Ready":
            warm_source_version = str(item.get("source_version") or "")
            warm_source_versions = {
                str(value) for value in item.get("source_versions", []) or [] if str(value)
            }
            if storage_source_version:
                if not warm_source_version and not warm_source_versions:
                    raise WarmupNotReadyError(
                        f"node warmup for {db_name} has no DB generation marker",
                        retryable=True,
                    )
                if warm_source_versions and warm_source_versions != {storage_source_version}:
                    raise WarmupNotReadyError(
                        f"node warmup for {db_name} is for a stale DB generation",
                        retryable=True,
                    )
                if warm_source_version and warm_source_version != storage_source_version:
                    raise WarmupNotReadyError(
                        f"node warmup for {db_name} is for a stale DB generation",
                        retryable=True,
                    )
            return dict(item)
        ready = int(item.get("nodes_ready") or 0)
        total = int(item.get("total_jobs") or 0)
        active = int(item.get("nodes_active") or 0)
        retryable = db_status in {"Loading", "Pending", "Starting", "Unknown"} or active > 0
        raise WarmupNotReadyError(
            f"node warmup for {db_name} is {db_status} ({ready}/{total} nodes ready)",
            retryable=retryable,
        )

    raise WarmupNotReadyError(
        f"node warmup for {db_name} has not started or is not visible yet",
        retryable=True,
    )

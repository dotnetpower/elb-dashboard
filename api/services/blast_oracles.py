"""Tie-order oracle helpers for BLAST finalizers.

These helpers keep oracle Storage metadata and finalizer pointer files out of
the Celery task module. The task decides *when* to attach an oracle; this module
owns how oracle payloads are normalized, validated, and uploaded.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from api.services.blast_db_metadata import extract_db_name, resolve_db_metadata

LOGGER = logging.getLogger(__name__)

TIE_ORDER_ORACLE_BLOB = "metadata/tie-order-oracle.txt"
TIE_ORDER_ORACLE_URLS_BLOB = "metadata/tie-order-oracle-urls.txt"
TIE_ORDER_ORACLE_STRICT_BLOB = "metadata/tie-order-oracle-strict.txt"
TIE_ORDER_ORACLE_MAX_BYTES = 1024 * 1024


def upload_tie_order_oracle_if_present(
    *,
    storage_account: str,
    job_id: str,
    options: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(options, Mapping):
        return None
    oracle = _normalise_tie_order_oracle(
        options.get("tie_order_oracle_accessions") or options.get("tie_order_oracle_text")
    )
    if oracle is None:
        return None
    text, accession_count = oracle
    from api.services import get_credential
    from api.services.storage_data import upload_blob_text

    blob_path = f"{_relative_blob_path(job_id, 'job_id')}/{TIE_ORDER_ORACLE_BLOB}"
    upload_blob_text(
        get_credential(),
        storage_account,
        "results",
        blob_path,
        text,
        content_type="text/plain; charset=utf-8",
    )
    strict_requested = _option_enabled(options, "tie_order_oracle_strict")
    if strict_requested:
        upload_blob_text(
            get_credential(),
            storage_account,
            "results",
            f"{_relative_blob_path(job_id, 'job_id')}/{TIE_ORDER_ORACLE_STRICT_BLOB}",
            "1\n",
            content_type="text/plain; charset=utf-8",
        )
    return {"blob_path": blob_path, "accession_count": accession_count, "strict": strict_requested}


def upload_db_order_oracle_pointer_if_available(
    *,
    storage_account: str,
    job_id: str,
    database: str,
    options: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(options, Mapping) or options.get("use_db_order_oracle") is not True:
        return None
    from api.services.sharding_precision import normalize_sharding_mode

    if normalize_sharding_mode(options) != "precise":
        return None
    if options.get("tie_order_oracle_accessions") or options.get("tie_order_oracle_text"):
        return None
    db_name = extract_db_name(database)
    if not db_name:
        return None
    metadata = resolve_db_metadata(storage_account, db_name)
    source_version = str(metadata.get("source_version") or "") if metadata else ""
    part_urls = db_order_oracle_part_urls(
        storage_account=storage_account,
        db_name=db_name,
        expected_source_version=source_version or None,
    )
    if not part_urls:
        return None
    from api.services import get_credential
    from api.services.storage_data import upload_blob_text

    blob_path = f"{_relative_blob_path(job_id, 'job_id')}/{TIE_ORDER_ORACLE_URLS_BLOB}"
    upload_blob_text(
        get_credential(),
        storage_account,
        "results",
        blob_path,
        "\n".join(part_urls) + "\n",
        content_type="text/plain; charset=utf-8",
    )
    return {
        "blob_path": blob_path,
        "db_name": db_name,
        "part_count": len(part_urls),
        "source_version": source_version or None,
    }


def db_order_oracle_part_urls(
    *,
    storage_account: str,
    db_name: str,
    expected_source_version: str | None = None,
) -> list[str]:
    from api.services import get_credential
    from api.services.db_order_oracle import ORACLE_PARTS_DIR, ORACLE_PREFIX_ROOT
    from api.services.storage_data import _blob_service

    svc = _blob_service(get_credential(), storage_account)
    container = svc.get_container_client("blast-db")
    status_blob = f"{ORACLE_PREFIX_ROOT}/{db_name}/status.json"
    try:
        status = json.loads(
            container.get_blob_client(status_blob).download_blob().readall().decode("utf-8")
        )
    except Exception:
        return []
    if not isinstance(status, dict):
        return []
    run_id = str(status.get("run_id") or "")
    expected_parts = int(status.get("expected_parts") or 0)
    oracle_source_version = str(status.get("source_version") or "")
    if expected_source_version and oracle_source_version != expected_source_version:
        LOGGER.info(
            "db-order oracle skipped for %s: source_version mismatch oracle=%s db=%s",
            db_name,
            oracle_source_version or "<missing>",
            expected_source_version,
        )
        return []
    if not run_id or expected_parts <= 0:
        return []
    prefix = f"{ORACLE_PREFIX_ROOT}/{db_name}/{ORACLE_PARTS_DIR}/{run_id}/"
    part_names = sorted(
        blob.name
        for blob in container.list_blobs(name_starts_with=prefix)
        if str(blob.name).endswith(".txt")
    )
    if len(part_names) < expected_parts:
        return []
    return [
        f"https://{storage_account}.blob.core.windows.net/blast-db/{name}"
        for name in part_names
    ]


def _normalise_tie_order_oracle(value: object) -> tuple[str, int] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        accessions = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise ValueError("tie order oracle accession list must contain only strings")
        accessions = [item.strip() for item in value if item.strip()]
    else:
        raise ValueError("tie order oracle must be a string or a list of accessions")
    if not accessions:
        return None
    text = "\n".join(accessions) + "\n"
    if len(text.encode("utf-8")) > TIE_ORDER_ORACLE_MAX_BYTES:
        raise ValueError("tie order oracle is too large")
    return text, len(accessions)


def _relative_blob_path(value: str, label: str) -> str:
    path = value.strip().lstrip("/")
    if not path or any(part == ".." for part in path.split("/")):
        raise ValueError(f"{label} must be a relative blob path without '..'")
    return path


def _option_enabled(options: Mapping[str, Any], key: str) -> bool:
    value = options.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)

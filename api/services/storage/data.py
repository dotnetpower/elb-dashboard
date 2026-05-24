"""Azure Blob data-plane helpers for BLAST storage workflows.

Responsibility: Azure Blob data-plane helpers for BLAST storage workflows
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `encode_blob_file_id`, `decode_blob_file_id`, `safe_download_filename`,
`result_media_type`, `upload_blob_bytes`, `upload_blob_text`, `container_usage_summaries`
Risky contracts: Validate Storage account/blob inputs and preserve the no-browser-SAS policy.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.storage import blob_io as _blob_io
from api.services.storage import client_pool as _client_pool
from api.services.storage import database_list as _database_list
from api.services.storage import usage as _usage
from api.services.storage.blob_ids import (
    decode_blob_file_id,
    encode_blob_file_id,
    result_media_type,
    safe_download_filename,
)
from api.services.storage.blob_io import (
    METADATA_BLOB_MAX_BYTES,
    read_metadata_blob_bytes,
    read_metadata_blob_text,
)
from api.services.storage.blob_paths import _validate_blob_path
from api.services.storage.client_pool import (
    _BLOB_SERVICE_POOL,
    _BLOB_SERVICE_POOL_LOCK,
    _BLOB_SERVICE_THREAD_LOCAL,
    _STORAGE_ACCOUNT_NAME_RE,
    prune_idle_blob_service_clients,
    reset_blob_service_pool,
)
from api.services.storage.failure_classifier import classify_storage_failure

LOGGER = logging.getLogger(__name__)
_BLOB_SERVICE_POOL_MAX = _client_pool._BLOB_SERVICE_POOL_MAX


def _sync_patch_surface() -> None:
    """Forward legacy `storage.data` monkeypatches into split submodules."""
    _client_pool._BLOB_SERVICE_POOL_MAX = _BLOB_SERVICE_POOL_MAX
    _blob_io._blob_service = _blob_service
    _usage._blob_service = _blob_service
    _database_list._blob_service = _blob_service
    _database_list.read_metadata_blob_text = read_metadata_blob_text


def _blob_service(credential: TokenCredential, account_name: str) -> Any:
    _client_pool._BLOB_SERVICE_POOL_MAX = _BLOB_SERVICE_POOL_MAX
    return _client_pool._blob_service(credential, account_name)


def upload_blob_bytes(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    data: bytes | Iterable[bytes],
    *,
    content_type: str = "application/octet-stream",
) -> str:
    _sync_patch_surface()
    return _blob_io.upload_blob_bytes(
        credential,
        account_name,
        container,
        blob_path,
        data,
        content_type=content_type,
    )


def upload_blob_text(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    text: str,
    *,
    content_type: str = "text/plain; charset=utf-8",
) -> str:
    _sync_patch_surface()
    return _blob_io.upload_blob_text(
        credential,
        account_name,
        container,
        blob_path,
        text,
        content_type=content_type,
    )


def upload_query_text(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    fasta_text: str,
) -> str:
    _sync_patch_surface()
    return _blob_io.upload_query_text(credential, account_name, container, blob_path, fasta_text)


def upload_group_fasta(
    credential: TokenCredential,
    account_name: str,
    query_blob_path: str,
    group_fasta: str,
) -> str:
    _sync_patch_surface()
    return _blob_io.upload_group_fasta(credential, account_name, query_blob_path, group_fasta)


def read_blob_text(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    max_bytes: int = 4096,
) -> str:
    _sync_patch_surface()
    return _blob_io.read_blob_text(credential, account_name, container, blob_path, max_bytes)


def read_result_blob_text(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    max_bytes: int = 4096,
) -> str:
    _sync_patch_surface()
    return _blob_io.read_result_blob_text(credential, account_name, container, blob_path, max_bytes)


def stream_blob_bytes(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
) -> Iterator[bytes]:
    _sync_patch_surface()
    return _blob_io.stream_blob_bytes(credential, account_name, container, blob_path)


def list_result_blobs(
    credential: TokenCredential,
    account_name: str,
    container: str = "results",
    prefix: str = "",
    *,
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    _sync_patch_surface()
    return _blob_io.list_result_blobs(
        credential, account_name, container, prefix, max_results=max_results
    )


def list_databases(
    credential: TokenCredential,
    account_name: str,
    container: str = "blast-db",
) -> list[dict[str, Any]]:
    _sync_patch_surface()
    return _database_list.list_databases(credential, account_name, container)


def container_usage_summaries(
    credential: TokenCredential,
    account_name: str,
    container_names: Iterable[str],
    *,
    max_blobs_per_container: int | None = None,
) -> dict[str, dict[str, Any]]:
    _sync_patch_surface()
    return _usage.container_usage_summaries(
        credential,
        account_name,
        container_names,
        max_blobs_per_container=max_blobs_per_container,
    )

__all__ = [
    "METADATA_BLOB_MAX_BYTES",
    "_BLOB_SERVICE_POOL",
    "_BLOB_SERVICE_POOL_LOCK",
    "_BLOB_SERVICE_POOL_MAX",
    "_BLOB_SERVICE_THREAD_LOCAL",
    "_STORAGE_ACCOUNT_NAME_RE",
    "_blob_service",
    "_validate_blob_path",
    "classify_storage_failure",
    "container_usage_summaries",
    "decode_blob_file_id",
    "encode_blob_file_id",
    "list_databases",
    "list_result_blobs",
    "prune_idle_blob_service_clients",
    "read_blob_text",
    "read_metadata_blob_bytes",
    "read_metadata_blob_text",
    "read_result_blob_text",
    "reset_blob_service_pool",
    "result_media_type",
    "safe_download_filename",
    "stream_blob_bytes",
    "upload_blob_bytes",
    "upload_blob_text",
    "upload_group_fasta",
    "upload_query_text",
]


# NOTE: There is intentionally NO `generate_download_url` / SAS issuer here.
# Per .github/copilot-instructions.md §9, every Storage account stays
# `publicNetworkAccess: Disabled` and **the browser must never receive a SAS
# token**. Result downloads are served by streaming the blob through the api
# sidecar (1 MiB chunks, 4 MiB block uploads, semaphore-capped to 4 concurrent
# transfers). When that route is implemented, add a `stream_blob_to_response`
# helper here that returns an async iterator the FastAPI route can await — do
# NOT bring back `generate_blob_sas` / `get_user_delegation_key`.



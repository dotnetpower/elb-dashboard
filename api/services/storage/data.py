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

from api.services.storage.blob_ids import (
    decode_blob_file_id,
    encode_blob_file_id,
    result_media_type,
    safe_download_filename,
)
from api.services.storage.blob_io import (
    METADATA_BLOB_MAX_BYTES,
    list_result_blobs,
    read_blob_text,
    read_metadata_blob_bytes,
    read_metadata_blob_text,
    read_result_blob_text,
    stream_blob_bytes,
    upload_blob_bytes,
    upload_blob_text,
    upload_group_fasta,
    upload_query_text,
)
from api.services.storage.blob_paths import _validate_blob_path
from api.services.storage.client_pool import (
    _blob_service,
    prune_idle_blob_service_clients,
    reset_blob_service_pool,
)
from api.services.storage.database_list import list_databases
from api.services.storage.failure_classifier import classify_storage_failure
from api.services.storage.usage import container_usage_summaries

LOGGER = logging.getLogger(__name__)

__all__ = [
    "METADATA_BLOB_MAX_BYTES",
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



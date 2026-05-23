"""Compatibility wrapper for `api.services.storage.data`.

Responsibility: Re-export `api.services.storage.data` at the legacy flat path.
Edit boundaries: Implementation lives in `api.services.storage.data`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from api.services.storage.data import (
    classify_storage_failure,
    container_usage_summaries,
    decode_blob_file_id,
    encode_blob_file_id,
    list_databases,
    list_result_blobs,
    prune_idle_blob_service_clients,
    read_blob_text,
    read_metadata_blob_bytes,
    read_metadata_blob_text,
    read_result_blob_text,
    reset_blob_service_pool,
    result_media_type,
    safe_download_filename,
    stream_blob_bytes,
    upload_blob_bytes,
    upload_blob_text,
    upload_group_fasta,
    upload_query_text,
)

__all__ = [
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

"""Best-effort Storage container usage summaries.

Responsibility: Count blobs and total bytes for named containers using the
shared BlobServiceClient pool.
Edit boundaries: Keep usage aggregation only. Blob uploads/downloads and failure
classification live in sibling modules.
Key entry points: `container_usage_summaries`.
Risky contracts: This is best-effort telemetry; per-container failures must be
reported in the returned payload rather than raised so dashboard cards can
degrade gracefully.
Validation: `uv run pytest -q api/tests/test_storage_usage_cache.py`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.storage.client_pool import _blob_service


def container_usage_summaries(
    credential: TokenCredential,
    account_name: str,
    container_names: Iterable[str],
    *,
    max_blobs_per_container: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Return best-effort blob-count and byte-size totals for named containers."""
    svc = _blob_service(credential, account_name)
    summaries: dict[str, dict[str, Any]] = {}
    for container_name in container_names:
        total_size = 0
        blob_count = 0
        truncated = False
        try:
            cc = svc.get_container_client(container_name)
            for blob in cc.list_blobs():
                blob_count += 1
                size = getattr(blob, "size", None)
                if size is None:
                    size = getattr(blob, "content_length", None)
                if isinstance(size, int):
                    total_size += size
                if max_blobs_per_container is not None and blob_count >= max_blobs_per_container:
                    truncated = True
                    break
        except Exception as exc:
            summaries[container_name] = {
                "blob_count": None,
                "size_bytes": None,
                "usage_error": type(exc).__name__,
                "usage_truncated": False,
            }
            continue
        summaries[container_name] = {
            "blob_count": blob_count,
            "size_bytes": total_size,
            "usage_error": None,
            "usage_truncated": truncated,
        }
    return summaries

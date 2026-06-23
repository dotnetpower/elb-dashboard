"""ADLS Gen2 (dfs) data-plane I/O helpers (directory listing).

Responsibility: Native dfs equivalents of the Blob result-listing path, used
when ``STORAGE_DFS_ENABLED`` is on. Returns the SAME row shape as
``blob_io.list_result_blobs`` (``file_id`` / ``name`` / ``size`` /
``last_modified``) so callers and the frontend are agnostic to which SDK served
the request.
Edit boundaries: dfs directory/file *listing* only. The pooled client lifecycle
lives in ``dfs_client_pool``; single-blob reads/streams stay on the Blob API
(they gain nothing from dfs on an HNS account and reuse the proven
range/gzip/416/semaphore logic in ``blob_io``). Recursive delete/rename belong
to issue #69, not here.
Key entry points: ``list_paths_dfs``.
Risky contracts: ``get_paths`` returns BOTH files and directories — directory
entries are filtered out to match Blob ``list_blobs`` (which yields only blobs).
A missing directory (job submitted, no results yet) degrades to ``[]`` exactly
like a Blob prefix scan with no matches. ``last_modified`` is normalized to an
ISO string regardless of whether the SDK hands back a ``datetime`` or a string.
Validation: ``uv run pytest -q api/tests/test_storage_dfs_io.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError

from api.services.storage.blob_ids import encode_blob_file_id

LOGGER = logging.getLogger(__name__)


def _iso_last_modified(value: Any) -> str | None:
    """Normalize a dfs ``PathProperties.last_modified`` to an ISO string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    # The dfs SDK may hand back an RFC-1123 string; pass it through as-is rather
    # than guessing a parse (callers only display / sort it).
    return str(value)


def list_paths_dfs(
    credential: TokenCredential,
    account_name: str,
    container: str,
    prefix: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """List files under ``prefix`` via the dfs ``get_paths`` directory walk.

    ``prefix`` is a directory path (``{job_id}/`` or the date-tiered
    ``YYYY/MM/DD/{job_id}/``). Returns the same row shape as
    ``blob_io.list_result_blobs``. Directory entries are skipped so the result
    set matches a Blob ``list_blobs(name_starts_with=prefix)`` (blobs only). A
    non-existent directory yields ``[]`` (parity with an empty prefix scan).
    """
    from api.services.storage.dfs_client_pool import _dfs_filesystem

    fs = _dfs_filesystem(credential, account_name, container)
    dir_path = prefix.rstrip("/") or None
    blobs: list[dict[str, Any]] = []
    try:
        paths = fs.get_paths(path=dir_path, recursive=True)
        for path in paths:
            if getattr(path, "is_directory", False):
                continue
            name = str(getattr(path, "name", "") or "")
            if not name:
                continue
            blobs.append(
                {
                    "file_id": encode_blob_file_id(name),
                    "name": name,
                    "size": int(getattr(path, "content_length", 0) or 0),
                    "last_modified": _iso_last_modified(getattr(path, "last_modified", None)),
                }
            )
            if len(blobs) >= limit:
                break
    except ResourceNotFoundError:
        # Directory not created yet (no results uploaded) — same as an empty
        # Blob prefix scan.
        return []
    return blobs

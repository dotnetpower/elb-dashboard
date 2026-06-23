"""ADLS Gen2 (dfs) data-plane I/O helpers (directory listing).

Responsibility: Native dfs equivalents of the Blob result-listing path, used
when ``STORAGE_DFS_ENABLED`` is on. Returns the SAME row shape as
``blob_io.list_result_blobs`` (``file_id`` / ``name`` / ``size`` /
``last_modified``) so callers and the frontend are agnostic to which SDK served
the request.
Edit boundaries: dfs directory/file *listing*, *recursive delete*, and atomic
*rename* only. The pooled client lifecycle lives in ``dfs_client_pool``;
single-blob reads/streams stay on the Blob API (they gain nothing from dfs on an
HNS account and reuse the proven range/gzip/416/semaphore logic in ``blob_io``).
Lifecycle retention is a #69 follow-up.
Key entry points: ``list_paths_dfs``, ``delete_directory_dfs``,
``rename_directory_dfs``.
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


def delete_directory_dfs(
    credential: TokenCredential,
    account_name: str,
    container: str,
    directory_path: str,
    *,
    expected_leaf: str | None = None,
) -> bool:
    """Recursively delete a directory on the HNS account in one metadata op.

    Returns ``True`` when the directory existed and was deleted, ``False`` when
    it was already absent (idempotent). On a non-HNS account this would be a
    per-blob loop, but the platform account is HNS so this is a single atomic
    ``delete_directory`` call regardless of how many blobs are underneath.

    SAFETY (recursive delete is irreversible):
      - ``directory_path`` must be non-empty and free of ``..`` segments.
      - When ``expected_leaf`` is given, the directory's LAST path segment must
        equal it. A per-job delete passes ``expected_leaf=job_id`` so a bug can
        never target a parent date bucket (``results/2026/06/23``) and wipe an
        entire day of unrelated jobs.
    """
    dir_path = (directory_path or "").strip("/")
    if not dir_path or any(part == ".." for part in dir_path.split("/")):
        raise ValueError(f"invalid directory path for delete: {directory_path!r}")
    if expected_leaf is not None and dir_path.split("/")[-1] != expected_leaf:
        raise ValueError(
            f"refusing recursive delete of {dir_path!r}: leaf segment != {expected_leaf!r}"
        )
    from api.services.storage.dfs_client_pool import _dfs_filesystem

    fs = _dfs_filesystem(credential, account_name, container)
    directory_client = fs.get_directory_client(dir_path)
    try:
        directory_client.delete_directory()
        return True
    except ResourceNotFoundError:
        return False


def rename_directory_dfs(
    credential: TokenCredential,
    account_name: str,
    container: str,
    src_path: str,
    dst_path: str,
    *,
    expected_src_leaf: str | None = None,
) -> bool:
    """Atomically move a directory within a filesystem (metadata-only on HNS).

    Returns ``True`` when the source existed and was renamed, ``False`` when the
    source is already absent (idempotent — a prior run may have moved it). Both
    paths live in the same ``container`` (dfs filesystem). On HNS this is a single
    metadata operation with no blob copy, so moving a job's whole result tree is
    O(1) regardless of size.

    SAFETY: rejects empty / ``..`` paths on either side; when ``expected_src_leaf``
    is given the SOURCE directory's last segment must equal it (a per-job move
    passes ``expected_src_leaf=job_id`` so it can only move the job's own tree).
    """
    src = (src_path or "").strip("/")
    dst = (dst_path or "").strip("/")
    if not src or any(part == ".." for part in src.split("/")):
        raise ValueError(f"invalid source path for rename: {src_path!r}")
    if not dst or any(part == ".." for part in dst.split("/")):
        raise ValueError(f"invalid destination path for rename: {dst_path!r}")
    if expected_src_leaf is not None and src.split("/")[-1] != expected_src_leaf:
        raise ValueError(
            f"refusing rename of {src!r}: source leaf segment != {expected_src_leaf!r}"
        )
    from api.services.storage.dfs_client_pool import _dfs_filesystem

    fs = _dfs_filesystem(credential, account_name, container)
    directory_client = fs.get_directory_client(src)
    try:
        # The dfs SDK takes the new name as ``{filesystem}/{new_path}``.
        directory_client.rename_directory(new_name=f"{container}/{dst}")
        return True
    except ResourceNotFoundError:
        return False

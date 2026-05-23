"""Blob path validation helpers for Storage data-plane operations.

Responsibility: Reject unsafe blob paths before upload/read/stream/list helpers
use them against Azure Storage.
Edit boundaries: Pure path validation only. No Azure SDK calls or container
selection logic.
Key entry points: `_validate_blob_path`.
Risky contracts: Reject path traversal, absolute paths, query strings, and URL
fragments before they reach a Storage SDK call.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations


def _validate_blob_path(blob_path: str) -> None:
    if ".." in blob_path or blob_path.startswith("/") or "?" in blob_path or "#" in blob_path:
        raise ValueError("invalid blob_path: path traversal not allowed")

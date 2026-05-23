"""Blob identifier and download presentation helpers for Storage routes.

Responsibility: Encode/decode opaque blob file IDs and derive safe download
filenames / media types for BLAST result blobs.
Edit boundaries: Pure string handling only. No Azure SDK clients, credentials,
network calls, or container/blob I/O here.
Key entry points: `encode_blob_file_id`, `decode_blob_file_id`,
`safe_download_filename`, `result_media_type`.
Risky contracts: `decode_blob_file_id` rejects path traversal and URL-fragment /
query injection markers before callers use the decoded path against Storage.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations

import base64
import binascii
import re

_BLOB_FILE_ID_PREFIX = "b64_"


def encode_blob_file_id(blob_name: str) -> str:
    encoded = base64.urlsafe_b64encode(blob_name.encode("utf-8")).decode("ascii")
    return f"{_BLOB_FILE_ID_PREFIX}{encoded.rstrip('=')}"


def decode_blob_file_id(file_id: str) -> str | None:
    if not file_id.startswith(_BLOB_FILE_ID_PREFIX):
        return None
    value = file_id[len(_BLOB_FILE_ID_PREFIX) :]
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{value}{padding}").decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise ValueError("invalid file_id") from None
    if ".." in decoded or decoded.startswith("/") or "?" in decoded or "#" in decoded:
        raise ValueError("invalid file_id")
    return decoded


def safe_download_filename(blob_name: str) -> str:
    name = blob_name.rsplit("/", 1)[-1].strip() or "blast-result.out"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:128]
    return name or "blast-result.out"


def result_media_type(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".gz"):
        return "application/gzip"
    if lowered.endswith(".xml"):
        return "application/xml"
    if lowered.endswith((".out", ".log", ".txt")):
        return "text/plain"
    return "application/octet-stream"

"""Storage helpers for BLAST query upload and results listing."""

from __future__ import annotations

import logging
import re
from typing import Any

from azure.core.credentials import TokenCredential
from azure.storage.blob import BlobServiceClient

LOGGER = logging.getLogger(__name__)


def _blob_service(credential: TokenCredential, account_name: str) -> BlobServiceClient:
    return BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=credential,
    )


def upload_query_text(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    fasta_text: str,
) -> str:
    """Upload FASTA text to blob storage. Returns the blob URL."""
    if ".." in blob_path or blob_path.startswith("/"):
        raise ValueError("invalid blob_path: path traversal not allowed")
    svc = _blob_service(credential, account_name)
    blob = svc.get_blob_client(container, blob_path)
    blob.upload_blob(fasta_text.encode("utf-8"), overwrite=True)
    return blob.url


def read_blob_text(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    max_bytes: int = 4096,
) -> str:
    """Read the first max_bytes of a text blob. Returns UTF-8 text."""
    if ".." in blob_path or blob_path.startswith("/"):
        raise ValueError("invalid blob_path: path traversal not allowed")
    svc = _blob_service(credential, account_name)
    blob = svc.get_blob_client(container, blob_path)
    data = blob.download_blob(offset=0, length=max_bytes).readall()
    return data.decode("utf-8", errors="replace")


def list_result_blobs(
    credential: TokenCredential,
    account_name: str,
    container: str,
    prefix: str,
) -> list[dict[str, Any]]:
    """List blobs under a results prefix."""
    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    blobs: list[dict[str, Any]] = []
    for blob in cc.list_blobs(name_starts_with=prefix):
        blobs.append(
            {
                "name": blob.name,
                "size": blob.size,
                "last_modified": blob.last_modified.isoformat() if blob.last_modified else None,
            }
        )
    return blobs


# NOTE: There is intentionally NO `generate_download_url` / SAS issuer here.
# Per .github/copilot-instructions.md §9, every Storage account stays
# `publicNetworkAccess: Disabled` and **the browser must never receive a SAS
# token**. Result downloads are served by streaming the blob through the api
# sidecar (1 MiB chunks, 4 MiB block uploads, semaphore-capped to 4 concurrent
# transfers). When that route is implemented, add a `stream_blob_to_response`
# helper here that returns an async iterator the FastAPI route can await — do
# NOT bring back `generate_blob_sas` / `get_user_delegation_key`.


def list_databases(
    credential: TokenCredential,
    account_name: str,
    container: str = "blast-db",
) -> list[dict[str, Any]]:
    """List available BLAST databases in the blast-db container.

    BLAST databases consist of multiple files like core_nt.00.nhd,
    core_nt.00.nhi, core_nt.nal, etc. We extract the base DB name
    by stripping the volume number and extension suffixes.
    """
    # Known BLAST DB file extensions
    _DB_EXTS = {
        ".nhd", ".nhi", ".nhr", ".nin", ".nnd", ".nni", ".nog", ".nsq", ".nxm",
        ".nal", ".ndb", ".njs", ".nos", ".not", ".ntf", ".nto",
        ".phd", ".phi", ".phr", ".pin", ".pnd", ".pni", ".pog", ".psq", ".pxm",
        ".pal", ".pdb", ".pjs", ".pos", ".pot", ".ptf", ".pto",
    }
    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    db_info: dict[str, dict[str, Any]] = {}
    metadata_blobs: dict[str, str] = {}  # db_name -> metadata json content
    for blob in cc.list_blobs():
        parts = blob.name.split("/")
        name = parts[-1]  # file name without directory prefix
        # Detect the prefix to distinguish NCBI (top-level) from custom (custom_db/)
        is_custom = len(parts) >= 3 and parts[0] == "custom_db"
        # Collect metadata files separately
        if name.endswith("-metadata.json"):
            meta_db_name = name.replace("-metadata.json", "")
            try:
                bc = cc.get_blob_client(blob.name)
                metadata_blobs[meta_db_name] = bc.download_blob().readall().decode("utf-8")
            except Exception:
                pass
            continue
        # Skip staging artifacts
        if parts[0] in ("custom-db-build",) or (len(parts) >= 2 and parts[1] == ".staging"):
            continue
        # Check if file has a known BLAST extension
        for ext in _DB_EXTS:
            if name.endswith(ext):
                base = name[: -len(ext)]
                # Strip volume number suffix (e.g. ".00", ".01")
                base = re.sub(r"\.\d+$", "", base)
                if base:
                    if base not in db_info:
                        # Build the blob prefix so the frontend can reconstruct the full path
                        prefix = f"custom_db/{base}" if is_custom else base
                        db_info[base] = {
                            "name": base,
                            "container": container,
                            "prefix": prefix,
                            "source": "custom" if is_custom else "ncbi",
                            "file_count": 0,
                            "total_bytes": 0,
                            "last_modified": None,
                        }
                    db_info[base]["file_count"] += 1
                    db_info[base]["total_bytes"] += blob.size or 0
                    blob_modified = blob.last_modified
                    if blob_modified:
                        mod_str = blob_modified.isoformat() if hasattr(blob_modified, "isoformat") else str(blob_modified)
                        prev = db_info[base]["last_modified"]
                        if not prev or mod_str > prev:
                            db_info[base]["last_modified"] = mod_str
                break
    # Enrich with metadata (source_version, downloaded_at)
    import json as _json
    for db_name, info in db_info.items():
        if db_name in metadata_blobs:
            try:
                meta = _json.loads(metadata_blobs[db_name])
                info["source_version"] = meta.get("source_version")
                info["downloaded_at"] = meta.get("downloaded_at")
            except Exception:
                pass
    return sorted(db_info.values(), key=lambda d: d["name"])

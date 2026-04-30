"""Storage helpers for BLAST query upload and results listing."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from azure.core.credentials import TokenCredential
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    generate_blob_sas,
)

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


def generate_download_url(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_name: str,
    expiry_minutes: int = 60,
) -> str:
    """Generate a SAS URL for downloading a result blob using user delegation key."""
    svc = _blob_service(credential, account_name)
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=expiry_minutes)
    delegation_key = svc.get_user_delegation_key(
        key_start_time=now - timedelta(minutes=5),
        key_expiry_time=expiry,
    )
    sas = generate_blob_sas(
        account_name=account_name,
        container_name=container,
        blob_name=blob_name,
        user_delegation_key=delegation_key,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
    )
    return f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}?{sas}"


def list_databases(
    credential: TokenCredential,
    account_name: str,
    container: str = "blast-db",
) -> list[dict[str, Any]]:
    """List available BLAST databases in the blast-db container.

    Scans for top-level directories that contain database files.
    """
    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    db_names: set[str] = set()
    for blob in cc.list_blobs():
        parts = blob.name.split("/")
        if len(parts) >= 1:
            db_names.add(parts[0])
    return [{"name": n, "container": container} for n in sorted(db_names)]

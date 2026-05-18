"""BLAST database metadata helpers shared by submit and oracle flows."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)


def extract_db_name(database: str) -> str:
    """Extract the bare DB name from a BLAST database value of any supported shape."""
    db = database.strip()
    if not db:
        return ""
    if db.startswith("https://"):
        parsed = urlparse(db)
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) < 2 or path_parts[0] != "blast-db":
            return ""
        return path_parts[1]
    db = db.removeprefix("blast-db/")
    return db.split("/", 1)[0]


def resolve_db_metadata(storage_account: str, db_name: str) -> dict[str, Any] | None:
    """Read ``{db}-metadata.json`` from the workload Storage account.

    Returns a parsed dict when present. Missing metadata or transient Storage
    failures return ``None`` so submits can proceed without auto-sharding.
    """
    if not storage_account or not db_name:
        return None
    try:
        from azure.core.exceptions import ResourceNotFoundError

        from api.services import get_credential
        from api.services.storage_data import _blob_service

        service = _blob_service(get_credential(), storage_account)
        container = service.get_container_client("blast-db")
        try:
            data = container.get_blob_client(f"{db_name}-metadata.json").download_blob().readall()
        except ResourceNotFoundError:
            return None
        metadata = json.loads(data.decode("utf-8"))
        if isinstance(metadata, dict):
            return metadata
    except Exception as exc:
        LOGGER.info("db metadata lookup skipped for %s: %s", db_name, type(exc).__name__)
    return None

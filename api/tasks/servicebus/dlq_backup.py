"""Append-blob backup of dead-lettered Service Bus messages before deletion.

Responsibility: Persist a DLQ message (body + metadata + dead-letter reason) as
    one JSON line in a date-partitioned append blob so the automatic cleanup
    policy can delete the message from Service Bus without losing the evidence
    of why a BLAST request failed. Backup-then-delete is the contract; this
    module owns the "backup" half.
Edit boundaries: Reusable persistence logic only — no Service Bus SDK, no Celery
    task body. Mirrors the append-blob pattern in ``api.services.upgrade.history``.
Key entry points: ``backup_dead_letter_message``.
Risky contracts: Returns ``True`` only when the line is durably appended; the
    caller MUST keep (not delete) the message when this returns ``False`` or
    raises. ``AZURE_BLOB_ENDPOINT`` gates the blob backend; without it (local
    dev) the backup degrades to a JSON-lines file under the local state dir so
    the cleanup task still has a durable record.
Validation: ``uv run pytest -q api/tests/test_servicebus_tasks.py``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

_BLOB_ENDPOINT_ENV = "AZURE_BLOB_ENDPOINT"
_CONTAINER = "servicebus-dlq-backup"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"
_FILE_LOCK = threading.Lock()


def _line(record: dict[str, Any]) -> str:
    return json.dumps(record, default=str, ensure_ascii=False) + "\n"


def _blob_name() -> str:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"{day}.jsonl"


def backup_dead_letter_message(record: dict[str, Any]) -> bool:
    """Append one DLQ record. Returns True only on a durable write."""
    endpoint = (os.environ.get(_BLOB_ENDPOINT_ENV) or "").strip()
    if endpoint:
        return _append_blob(endpoint, record)
    return _append_file(record)


def _append_blob(endpoint: str, record: dict[str, Any]) -> bool:
    try:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import BlobServiceClient

        svc = BlobServiceClient(account_url=endpoint, credential=get_credential())
        try:
            container = svc.get_container_client(_CONTAINER)
            try:
                container.create_container()
            except ResourceExistsError:
                pass
            blob = container.get_blob_client(_blob_name())
            if not blob.exists():
                try:
                    blob.create_append_blob()
                except ResourceExistsError:
                    pass
            blob.append_block(_line(record).encode("utf-8"))
        finally:
            svc.close()
        return True
    except Exception:
        LOGGER.exception("DLQ blob backup failed; message will be kept")
        return False


def _append_file(record: dict[str, Any]) -> bool:
    try:
        default_root = Path(__file__).resolve().parents[3] / ".logs" / "local" / "state"
        root = Path(os.environ.get(_LOCAL_STATE_ENV, str(default_root)))
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"servicebus_dlq_backup_{_blob_name()}"
        with _FILE_LOCK, path.open("a", encoding="utf-8") as fh:
            fh.write(_line(record))
        return True
    except OSError:
        LOGGER.exception("DLQ file backup failed; message will be kept")
        return False

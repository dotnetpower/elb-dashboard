"""Durable outbox for Service Bus producer response events.

Responsibility: Persist completion events before request settlement or bridge
    terminalisation, list pending events, and remove only events confirmed as
    published to the configured completion entity.
Edit boundaries: Persistence only. Event construction and Service Bus publish
    calls remain in ``api.tasks.servicebus.tasks``.
Key entry points: ``enqueue_response``, ``list_pending_responses``,
    ``mark_response_delivered``.
Risky contracts: A duplicate ``event_id`` is idempotent; deployed writes fail
    closed when Table Storage is unavailable; publish-before-delete may produce
    a duplicate event after a crash but can never lose the producer response.
Validation: ``uv run pytest -q api/tests/test_service_bus_outbox.py``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "servicebusoutbox"
_PARTITION_KEY = "producer_response"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"

_CLIENT: TableClient | None = None
_CLIENT_LOCK = threading.Lock()
_TABLE_ENSURED = False
_TABLE_ENSURED_LOCK = threading.Lock()
_FILE_LOCK = threading.Lock()


class ResponseOutboxPersistenceError(RuntimeError):
    """Raised when a deployed response cannot be persisted durably."""


@dataclass(frozen=True)
class PendingResponse:
    event_id: str
    event: dict[str, Any]
    created_at: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_key(event_id: str) -> str:
    return event_id.strip()[:512]


def _use_table_backend() -> bool:
    return bool(os.environ.get(_TABLE_ENDPOINT_ENV) and os.environ.get("CONTAINER_APP_NAME"))


def _require_deployed_table() -> None:
    if os.environ.get("CONTAINER_APP_NAME") and not os.environ.get(_TABLE_ENDPOINT_ENV):
        raise ResponseOutboxPersistenceError(
            "producer response outbox requires AZURE_TABLE_ENDPOINT when deployed"
        )


def _table_client() -> TableClient:
    global _CLIENT
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = TableClient(
                endpoint=endpoint,
                table_name=_TABLE_NAME,
                credential=get_credential(),
            )
        return _CLIENT


def _ensure_table() -> None:
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _TABLE_ENSURED_LOCK:
        if _TABLE_ENSURED:
            return
        service = TableServiceClient(endpoint=endpoint, credential=get_credential())
        try:
            service.create_table_if_not_exists(_TABLE_NAME)
        finally:
            service.close()
        _TABLE_ENSURED = True


def _entity(event_id: str, event: dict[str, Any], created_at: str) -> dict[str, Any]:
    return {
        "PartitionKey": _PARTITION_KEY,
        "RowKey": _row_key(event_id),
        "created_at": created_at,
        "payload_json": json.dumps(event, separators=(",", ":"), default=str),
    }


def _state_file() -> Path:
    default_root = Path(__file__).resolve().parents[2] / ".logs" / "local" / "state"
    root = Path(os.environ.get(_LOCAL_STATE_ENV, str(default_root)))
    return root / "service_bus_outbox.json"


def _read_file() -> dict[str, dict[str, Any]]:
    path = _state_file()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_file(value: dict[str, dict[str, Any]]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def enqueue_response(event: dict[str, Any]) -> bool:
    """Persist one response event, returning False when it already exists."""
    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("producer response event_id is required")
    _require_deployed_table()
    created_at = _now_iso()
    if _use_table_backend():
        try:
            _ensure_table()
            _table_client().create_entity(_entity(event_id, event, created_at))
            return True
        except ResourceExistsError:
            return False
        except Exception as exc:
            raise ResponseOutboxPersistenceError(
                "producer response could not be persisted to Azure Table Storage"
            ) from exc
    key = _row_key(event_id)
    with _FILE_LOCK:
        state = _read_file()
        if key in state:
            return False
        state[key] = {"event": dict(event), "created_at": created_at}
        _write_file(state)
    return True


def list_pending_responses(limit: int = 200) -> list[PendingResponse]:
    """Return a bounded oldest-first list of producer responses awaiting publish."""
    bounded = max(1, min(int(limit), 1000))
    _require_deployed_table()
    pending: list[PendingResponse] = []
    if _use_table_backend():
        _ensure_table()
        rows = _table_client().query_entities(
            query_filter=f"PartitionKey eq '{_PARTITION_KEY}'",
            results_per_page=bounded,
        )
        for row in rows:
            try:
                event = json.loads(str(row.get("payload_json") or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            pending.append(
                PendingResponse(
                    event_id=str(row.get("RowKey") or ""),
                    event=event,
                    created_at=str(row.get("created_at") or ""),
                )
            )
            if len(pending) >= bounded:
                break
    else:
        with _FILE_LOCK:
            state = _read_file()
        for event_id, raw in state.items():
            event = raw.get("event") if isinstance(raw, dict) else None
            if not isinstance(event, dict):
                continue
            pending.append(
                PendingResponse(
                    event_id=event_id,
                    event=dict(event),
                    created_at=str(raw.get("created_at") or ""),
                )
            )
    pending.sort(key=lambda item: (item.created_at, item.event_id))
    return pending[:bounded]


def mark_response_delivered(event_id: str) -> None:
    """Remove one response only after the completion publish succeeds."""
    key = _row_key(event_id)
    if not key:
        return
    _require_deployed_table()
    if _use_table_backend():
        _ensure_table()
        try:
            _table_client().delete_entity(_PARTITION_KEY, key)
        except ResourceNotFoundError:
            return
        return
    with _FILE_LOCK:
        state = _read_file()
        if key not in state:
            return
        del state[key]
        _write_file(state)


def _reset_outbox_for_tests() -> None:
    global _CLIENT, _TABLE_ENSURED
    with _CLIENT_LOCK:
        _CLIENT = None
    with _TABLE_ENSURED_LOCK:
        _TABLE_ENSURED = False


__all__ = [
    "PendingResponse",
    "ResponseOutboxPersistenceError",
    "enqueue_response",
    "list_pending_responses",
    "mark_response_delivered",
]

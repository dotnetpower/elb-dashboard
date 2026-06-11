"""Service Bus → OpenAPI bridge tracking rows (one per accepted request).

Responsibility: Persist the mapping a Service-Bus-originated BLAST request needs
    so the transition publisher can poll the sibling OpenAPI plane and emit one
    event per status change: ``external_correlation_id`` → sibling
    ``openapi_job_id`` plus the LAST published status (the de-dup marker that
    makes "publish every transition" emit each transition exactly once) and a
    ``done`` terminal flag. Also the drain de-dup key: a correlation id that
    already has a row must not be submitted twice (Service Bus is at-least-once).
Edit boundaries: Reusable persistence logic only. No Service Bus SDK, no HTTP
    shaping, no event payload construction (that lives in the tasks). Mirrors the
    Table/file backend gating of ``performance_pref`` / ``service_bus_pref``.
Key entry points: ``BridgeRecord``, ``get_bridge``, ``upsert_bridge``,
    ``list_active_bridges``, ``mark_published``, ``mark_done``.
Risky contracts: ``last_status`` is the published-transition marker — the
    publisher MUST only emit when the freshly observed status differs from it,
    then update it, or the topic floods with duplicate events. ``list_active_
    bridges`` returns only non-``done`` rows and is bounded by ``limit`` so the
    publisher tick stays bounded.
Validation: ``uv run pytest -q api/tests/test_service_bus_tracking.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "servicebusbridge"
_TYPE = "servicebus_bridge"
_PARTITION_KEY = "servicebus_bridge"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"

# Sanitise a correlation id into a Table RowKey (Table keys forbid /\#?).
_KEY_SAFE = re.compile(r"[^A-Za-z0-9._:-]")

_ENSURED_TABLES: set[str] = set()
_ENSURED_TABLES_LOCK = threading.Lock()
_BRIDGE_TABLE_POOLED: TableClient | None = None
_BRIDGE_TABLE_POOL_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_key(correlation_id: str) -> str:
    return _KEY_SAFE.sub("_", correlation_id)[:512] or "unknown"


@dataclass
class BridgeRecord:
    correlation_id: str
    openapi_job_id: str = ""
    last_status: str = ""
    done: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "openapi_job_id": self.openapi_job_id,
            "last_status": self.last_status,
            "done": self.done,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BridgeRecord:
        return cls(
            correlation_id=str(value.get("correlation_id") or ""),
            openapi_job_id=str(value.get("openapi_job_id") or ""),
            last_status=str(value.get("last_status") or ""),
            done=bool(value.get("done")),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
        )


def _use_table_backend() -> bool:
    return bool(os.environ.get(_TABLE_ENDPOINT_ENV) and os.environ.get("CONTAINER_APP_NAME"))


def get_bridge(correlation_id: str) -> BridgeRecord | None:
    if _use_table_backend():
        return _get_table(correlation_id)
    return _get_file(correlation_id)


def upsert_bridge(record: BridgeRecord) -> BridgeRecord:
    if not record.created_at:
        record.created_at = _now_iso()
    record.updated_at = _now_iso()
    if _use_table_backend():
        _save_table(record)
    else:
        _save_file(record)
    return record


def mark_published(correlation_id: str, status: str) -> None:
    rec = get_bridge(correlation_id)
    if rec is None:
        return
    rec.last_status = status
    upsert_bridge(rec)


def mark_done(correlation_id: str, status: str) -> None:
    rec = get_bridge(correlation_id)
    if rec is None:
        return
    rec.last_status = status
    rec.done = True
    upsert_bridge(rec)


def list_active_bridges(limit: int = 200) -> list[BridgeRecord]:
    if _use_table_backend():
        return _list_table(limit)
    return _list_file(limit)


# --------------------------------------------------------------------------- #
# Table backend
# --------------------------------------------------------------------------- #


def _entity(record: BridgeRecord) -> dict[str, Any]:
    return {
        "PartitionKey": _PARTITION_KEY,
        "RowKey": _row_key(record.correlation_id),
        "type": _TYPE,
        "done": record.done,
        "last_status": record.last_status,
        "updated_at": record.updated_at or _now_iso(),
        "payload_json": json.dumps(record.to_dict(), default=str),
    }


def _record_from_entity(entity: dict[str, Any]) -> BridgeRecord | None:
    try:
        payload = json.loads(str(entity.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        return None
    return BridgeRecord.from_dict(payload)


def _table_client() -> TableClient:
    global _BRIDGE_TABLE_POOLED
    pool = _BRIDGE_TABLE_POOLED
    if pool is not None:
        return pool  # type: ignore[return-value]
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _BRIDGE_TABLE_POOL_LOCK:
        if _BRIDGE_TABLE_POOLED is None:
            from api.services.state_repo import _PooledTableClient

            _BRIDGE_TABLE_POOLED = _PooledTableClient(  # type: ignore[assignment]
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _BRIDGE_TABLE_POOLED  # type: ignore[return-value]


def _reset_service_bus_bridge_pool() -> None:
    global _BRIDGE_TABLE_POOLED
    with _BRIDGE_TABLE_POOL_LOCK:
        pool = _BRIDGE_TABLE_POOLED
        _BRIDGE_TABLE_POOLED = None
    if pool is not None:
        close = getattr(pool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: S110 - close races are not fatal
                pass


def _ensure_table() -> None:
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    if endpoint in _ENSURED_TABLES:
        return
    with _ENSURED_TABLES_LOCK:
        if endpoint in _ENSURED_TABLES:
            return
        with TableServiceClient(endpoint=endpoint, credential=get_credential()) as service:
            try:
                service.create_table_if_not_exists(_TABLE_NAME)
            except AttributeError:
                try:
                    service.create_table(_TABLE_NAME)
                except ResourceExistsError:
                    pass
        _ENSURED_TABLES.add(endpoint)


def _save_table(record: BridgeRecord) -> None:
    _ensure_table()
    with _table_client() as table:
        table.upsert_entity(_entity(record), mode=UpdateMode.REPLACE)


def _get_table(correlation_id: str) -> BridgeRecord | None:
    _ensure_table()
    with _table_client() as table:
        try:
            entity = table.get_entity(
                partition_key=_PARTITION_KEY, row_key=_row_key(correlation_id)
            )
        except ResourceNotFoundError:
            return None
        entity_dict = dict(entity)
    return _record_from_entity(entity_dict)


def _list_table(limit: int) -> list[BridgeRecord]:
    out: list[BridgeRecord] = []
    _ensure_table()
    with _table_client() as table:
        rows = table.query_entities(f"type eq '{_TYPE}' and done eq false", results_per_page=limit)
        for row in rows:
            rec = _record_from_entity(dict(row))
            if rec is not None and not rec.done:
                out.append(rec)
            if len(out) >= limit:
                break
    return out


# --------------------------------------------------------------------------- #
# Local JSON file backend
# --------------------------------------------------------------------------- #

_FILE_LOCK = threading.Lock()


def _state_file() -> Path:
    default_root = Path(__file__).resolve().parents[2] / ".logs" / "local" / "state"
    root = Path(os.environ.get(_LOCAL_STATE_ENV, str(default_root)))
    return root / "service_bus_bridge.json"


def _read_file_state() -> dict[str, Any]:
    path = _state_file()
    if not path.exists():
        return {}
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_file_state(data: dict[str, Any]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)


def _get_file(correlation_id: str) -> BridgeRecord | None:
    with _FILE_LOCK:
        data = _read_file_state()
    raw = data.get(_row_key(correlation_id))
    if not isinstance(raw, dict):
        return None
    return BridgeRecord.from_dict(raw)


def _save_file(record: BridgeRecord) -> None:
    with _FILE_LOCK:
        data = _read_file_state()
        data[_row_key(record.correlation_id)] = record.to_dict()
        _write_file_state(data)


def _list_file(limit: int) -> list[BridgeRecord]:
    with _FILE_LOCK:
        data = _read_file_state()
    out: list[BridgeRecord] = []
    for raw in data.values():
        if not isinstance(raw, dict):
            continue
        rec = BridgeRecord.from_dict(raw)
        if not rec.done:
            out.append(rec)
        if len(out) >= limit:
            break
    return out

"""Durable outbox for Service Bus producer response events.

Responsibility: Persist completion events before request settlement or bridge
    terminalisation, list pending events, and remove only events confirmed as
    published to the configured completion entity.
Edit boundaries: Persistence only. Event construction and Service Bus publish
    calls remain in ``api.tasks.servicebus.tasks``.
Key entry points: ``enqueue_response``, ``list_pending_responses``,
    ``list_due_responses``, ``pending_response_correlations``, ``has_pending_response``,
    ``defer_response``, ``mark_response_delivered``,
    ``reset_service_bus_outbox_after_fork``.
Risky contracts: A duplicate ``event_id`` is idempotent; deployed writes fail
    closed when Table Storage is unavailable; publish-before-delete may produce
    a duplicate event after a crash but can never lose the producer response.
    Flush reads select due rows before applying a bounded scan so future-deferred
    RowKeys cannot starve ready responses; corrupt payload rows are retained and
    quarantined rather than published or silently skipped. Celery soft deadlines
    propagate through Table persistence.
    A Celery prefork child must drop inherited Table clients and replace copied
    locks without closing parent transports.
Validation: ``uv run pytest -q api/tests/test_service_bus_outbox.py``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode
from billiard.exceptions import SoftTimeLimitExceeded

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "servicebusoutbox"
_PARTITION_KEY = "producer_response"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"
_TABLE_SCAN_LIMIT = 1000

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
    failure_count: int = 0
    next_attempt_at: str = ""
    last_error_code: str = ""


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
        "correlation_id": str(event.get("external_correlation_id") or "")[:256],
        "failure_count": 0,
        "next_attempt_at": "",
        "last_error_code": "",
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
        except SoftTimeLimitExceeded:
            raise
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


def _pending_from_table_row(row: Any) -> PendingResponse | None:
    event_id = str(row.get("RowKey") or "").strip()
    if not event_id:
        return None
    corrupt = False
    try:
        event = json.loads(str(row.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        event = {}
        corrupt = True
    if not isinstance(event, dict):
        event = {}
        corrupt = True
    try:
        failure_count = max(0, int(row.get("failure_count") or 0))
    except (TypeError, ValueError):
        failure_count = 0
        corrupt = True
    return PendingResponse(
        event_id=event_id,
        event=event,
        created_at=str(row.get("created_at") or ""),
        failure_count=failure_count,
        next_attempt_at=str(row.get("next_attempt_at") or ""),
        last_error_code=(
            "outbox_payload_corrupt" if corrupt else str(row.get("last_error_code") or "")
        ),
    )


def _query_table_responses(
    query_filter: str,
    *,
    result_limit: int,
    scan_limit: int,
) -> list[PendingResponse]:
    """Read a bounded Table snapshot and sort valid rows within that snapshot."""
    bounded_scan = max(1, min(int(scan_limit), _TABLE_SCAN_LIMIT))
    rows = _table_client().query_entities(
        query_filter=query_filter,
        results_per_page=min(bounded_scan, 1000),
    )
    pending: list[PendingResponse] = []
    invalid_rows = 0
    for row in islice(rows, bounded_scan):
        item = _pending_from_table_row(row)
        if item is None:
            invalid_rows += 1
            continue
        if item.last_error_code == "outbox_payload_corrupt":
            invalid_rows += 1
        pending.append(item)
    if invalid_rows:
        LOGGER.warning(
            "producer response outbox skipped malformed rows count=%d scan=%d",
            invalid_rows,
            bounded_scan,
        )
    pending.sort(key=lambda item: (item.created_at, item.event_id))
    return pending[: max(1, int(result_limit))]


def _scan_limit_for(result_limit: int) -> int:
    return min(_TABLE_SCAN_LIMIT, max(result_limit, result_limit * 5))


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _is_due(item: PendingResponse, due_at: datetime) -> bool:
    if not item.next_attempt_at:
        return True
    try:
        return _as_utc(item.next_attempt_at) <= due_at
    except ValueError:
        # Keep corrupt retry metadata visible to the flush loop, which repairs
        # it through the existing deferred_timestamp_corrupt path.
        return True


def list_pending_responses(limit: int = 200) -> list[PendingResponse]:
    """Return pending responses sorted within a bounded persistence snapshot."""
    bounded = max(1, min(int(limit), 1000))
    _require_deployed_table()
    pending: list[PendingResponse] = []
    if _use_table_backend():
        _ensure_table()
        return _query_table_responses(
            query_filter=f"PartitionKey eq '{_PARTITION_KEY}'",
            result_limit=bounded,
            scan_limit=_scan_limit_for(bounded),
        )
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
                    failure_count=max(0, int(raw.get("failure_count") or 0)),
                    next_attempt_at=str(raw.get("next_attempt_at") or ""),
                    last_error_code=str(raw.get("last_error_code") or ""),
                )
            )
    pending.sort(key=lambda item: (item.created_at, item.event_id))
    return pending[:bounded]


def list_due_responses(
    limit: int = 200,
    *,
    due_before: str | None = None,
) -> list[PendingResponse]:
    """Return a bounded snapshot of responses whose retry delay has elapsed."""
    bounded = max(1, min(int(limit), 1000))
    due_at = _as_utc(due_before) if due_before else datetime.now(UTC)
    _require_deployed_table()
    if not _use_table_backend():
        return [
            item
            for item in list_pending_responses(limit=_TABLE_SCAN_LIMIT)
            if _is_due(item, due_at)
        ][:bounded]

    _ensure_table()
    scan_limit = _scan_limit_for(bounded)
    due_iso = due_at.isoformat(timespec="seconds").replace("'", "''")
    query_filter = f"PartitionKey eq '{_PARTITION_KEY}' and next_attempt_at le '{due_iso}'"
    try:
        selected = _query_table_responses(
            query_filter,
            result_limit=scan_limit,
            scan_limit=scan_limit,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        LOGGER.warning(
            "producer response due-filter query failed; using bounded fallback",
            exc_info=True,
        )
        selected = []

    # Legacy rows may predate ``next_attempt_at`` and corrupt timestamps cannot
    # satisfy an OData date-string comparison. Sample the same bounded window
    # without the due predicate so both remain recoverable rather than hidden.
    fallback = _query_table_responses(
        f"PartitionKey eq '{_PARTITION_KEY}'",
        result_limit=scan_limit,
        scan_limit=scan_limit,
    )
    by_event_id = {item.event_id: item for item in selected}
    for item in fallback:
        if item.event_id not in by_event_id and _is_due(item, due_at):
            by_event_id[item.event_id] = item
    ready = sorted(
        by_event_id.values(),
        key=lambda item: (item.created_at, item.event_id),
    )
    return ready[:bounded]


def has_pending_response(correlation_id: str) -> bool:
    """Return whether this producer still has an undelivered response.

    New Table rows carry a queryable ``correlation_id`` column. Legacy rows are
    migrated when deferred; avoiding a payload scan here keeps transition
    polling to one indexed Table query per active bridge.
    """
    correlation = correlation_id.strip()
    if not correlation:
        return False
    _require_deployed_table()
    if _use_table_backend():
        _ensure_table()
        escaped = correlation.replace("'", "''")
        rows = _table_client().query_entities(
            query_filter=(f"PartitionKey eq '{_PARTITION_KEY}' and correlation_id eq '{escaped}'"),
            results_per_page=1,
        )
        return any(True for _row in rows)
    with _FILE_LOCK:
        state = _read_file()
    return any(
        isinstance(raw, dict)
        and isinstance(raw.get("event"), dict)
        and str(raw["event"].get("external_correlation_id") or "") == correlation
        for raw in state.values()
    )


def pending_response_correlations(limit: int = 1000) -> tuple[set[str], bool]:
    """Return pending producer correlations and whether the snapshot is complete.

    Transition polling calls this once per tick instead of issuing one
    unindexed Table query per active bridge. A full-sized page is treated as
    truncated (fail closed); the outbox flush drains it before bridge polling
    resumes.
    """
    bounded = max(1, min(int(limit), 1000))
    pending = list_pending_responses(limit=bounded)
    correlations = {
        str(item.event.get("external_correlation_id") or "").strip()
        for item in pending
        if str(item.event.get("external_correlation_id") or "").strip()
    }
    return correlations, len(pending) < bounded


def defer_response(
    event_id: str,
    *,
    error_code: str,
    retry_after_seconds: int,
    replacement_event: dict[str, Any] | None = None,
) -> None:
    """Persist a bounded retry delay for one response without deleting it."""
    key = _row_key(event_id)
    if not key:
        return
    delay = max(1, min(int(retry_after_seconds), 24 * 60 * 60))
    next_attempt_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="seconds")
    bounded_error = error_code.strip()[:128]
    _require_deployed_table()
    if _use_table_backend():
        _ensure_table()
        table = _table_client()
        try:
            current = table.get_entity(_PARTITION_KEY, key)
        except ResourceNotFoundError:
            return
        correlation_id = ""
        source_event = replacement_event
        if source_event is None:
            try:
                parsed_event = json.loads(str(current.get("payload_json") or "{}"))
                source_event = parsed_event if isinstance(parsed_event, dict) else None
            except json.JSONDecodeError:
                source_event = None
        if source_event is not None:
            correlation_id = str(source_event.get("external_correlation_id") or "")[:256]
        table.update_entity(
            {
                "PartitionKey": _PARTITION_KEY,
                "RowKey": key,
                "failure_count": max(0, int(current.get("failure_count") or 0)) + 1,
                "next_attempt_at": next_attempt_at,
                "last_error_code": bounded_error,
                **({"correlation_id": correlation_id} if correlation_id else {}),
                **(
                    {
                        "payload_json": json.dumps(
                            replacement_event, separators=(",", ":"), default=str
                        )
                    }
                    if replacement_event is not None
                    else {}
                ),
            },
            mode=UpdateMode.MERGE,
        )
        return
    with _FILE_LOCK:
        state = _read_file()
        raw = state.get(key)
        if not isinstance(raw, dict):
            return
        raw["failure_count"] = max(0, int(raw.get("failure_count") or 0)) + 1
        raw["next_attempt_at"] = next_attempt_at
        raw["last_error_code"] = bounded_error
        if replacement_event is not None:
            raw["event"] = dict(replacement_event)
        _write_file(state)


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


def reset_service_bus_outbox_after_fork() -> None:
    """Drop inherited Table/file state without touching parent transports."""
    global _CLIENT, _CLIENT_LOCK, _TABLE_ENSURED, _TABLE_ENSURED_LOCK, _FILE_LOCK
    _CLIENT = None
    _CLIENT_LOCK = threading.Lock()
    _TABLE_ENSURED = False
    _TABLE_ENSURED_LOCK = threading.Lock()
    _FILE_LOCK = threading.Lock()


__all__ = [
    "PendingResponse",
    "ResponseOutboxPersistenceError",
    "defer_response",
    "enqueue_response",
    "has_pending_response",
    "list_due_responses",
    "list_pending_responses",
    "mark_response_delivered",
    "pending_response_correlations",
    "reset_service_bus_outbox_after_fork",
]

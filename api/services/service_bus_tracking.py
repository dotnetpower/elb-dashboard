"""Service Bus → OpenAPI bridge tracking rows (one per accepted request).

Responsibility: Persist the mapping a Service-Bus-originated BLAST request needs
    so the transition publisher can poll the sibling OpenAPI plane and emit one
    event per status change: ``external_correlation_id`` → sibling
    ``openapi_job_id`` plus the LAST published status (the de-dup marker that
    makes "publish every transition" emit each transition exactly once) and a
    ``done`` terminal flag. Also carries the caller-supplied ``request_id``
    pass-through value so every published transition can echo it. Also the drain
    de-dup key: a correlation id that already has a row must not be submitted
    twice (Service Bus is at-least-once).
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

from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
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


# How long a ``claimed`` placeholder (a row reserved before its sibling submit
# completed) may sit without an ``openapi_job_id`` before another drain worker
# is allowed to steal the claim. Guards against a worker that crashed between
# claiming and submitting leaving the correlation id reserved forever (which
# would make every redelivery ABANDON in a loop until the message dead-letters).
# Must comfortably exceed the sibling submit timeout so a slow-but-alive submit
# is never stolen out from under itself.
def _claim_stale_seconds_from_env() -> int:
    """Resolve the stale-claim threshold from env, floored at 30s, fail-safe.

    A non-numeric override must never crash module import (which would take the
    whole worker down on startup); it logs and falls back to the 180s default.
    """
    raw = os.environ.get("SERVICEBUS_CLAIM_STALE_SECONDS", "180")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        LOGGER.warning(
            "invalid SERVICEBUS_CLAIM_STALE_SECONDS=%r; defaulting to 180", raw
        )
        value = 180
    return max(30, value)


_CLAIM_STALE_SECONDS = _claim_stale_seconds_from_env()


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
    # Caller-supplied pass-through tracking value (e.g. ``request_id``) taken
    # from the request queue message. Persisted here so every transition event
    # the publisher emits to the completion topic can echo it — a topic
    # subscriber then correlates on the SAME value the producer set. Empty when
    # the producer did not supply one.
    request_id: str = ""
    # When this correlation id was reserved (atomic claim) before its sibling
    # submit ran. Empty for legacy rows written by the non-claim path. A row
    # with a ``claimed_at`` but no ``openapi_job_id`` is a reservation in
    # flight; once submit succeeds ``openapi_job_id`` is filled and the row is
    # ``confirmed``. Used only by the staleness check in ``claim_bridge``.
    claimed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "openapi_job_id": self.openapi_job_id,
            "last_status": self.last_status,
            "done": self.done,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "request_id": self.request_id,
            "claimed_at": self.claimed_at,
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
            request_id=str(value.get("request_id") or ""),
            claimed_at=str(value.get("claimed_at") or ""),
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


def _claim_is_stale(rec: BridgeRecord) -> bool:
    """True when an UNCONFIRMED reservation is old enough to be stolen.

    A confirmed row (one carrying an ``openapi_job_id``) is never stale-stealable
    — its job exists and must not be resubmitted. An unconfirmed reservation with
    no parseable timestamp is treated as stealable so a malformed legacy row can
    never wedge a correlation id forever.
    """
    if rec.openapi_job_id:
        return False
    stamp = rec.claimed_at or rec.created_at
    if not stamp:
        return True
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).total_seconds() > _CLAIM_STALE_SECONDS


def claim_bridge(correlation_id: str, request_id: str = "") -> bool:
    """Atomically reserve a correlation id BEFORE its sibling submit runs.

    Returns True when THIS caller won the reservation (it must proceed to submit
    and then confirm via :func:`upsert_bridge` with the ``openapi_job_id``).
    Returns False when another in-flight drain already holds a fresh reservation
    or the row is already confirmed — the caller must NOT submit (it should defer
    so the winner's single submit is the only one). A reservation whose submit
    never confirmed and is older than ``_CLAIM_STALE_SECONDS`` is stolen (via
    optimistic concurrency on the Table backend) so a worker that crashed
    between claim and submit cannot reserve a correlation id forever. This is the
    single-writer guard that makes a parallel / multi-worker drain safe: at most
    one caller ever submits a given correlation id.
    """
    if _use_table_backend():
        return _claim_table(correlation_id, request_id)
    return _claim_file(correlation_id, request_id)


def release_bridge(correlation_id: str) -> None:
    """Drop an UNCONFIRMED reservation so a redelivery can re-claim + resubmit.

    Called when the submit after a successful claim fails (transient or
    permanent) so the placeholder does not linger as a phantom ``claimed`` row
    that blocks every future delivery. Best-effort and idempotent; it NEVER
    deletes a confirmed row (one that already carries an ``openapi_job_id``), so
    a late/duplicate release can never wipe a live job's tracking row.
    """
    if _use_table_backend():
        _release_table(correlation_id)
    else:
        _release_file(correlation_id)


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


def _claim_table(correlation_id: str, request_id: str) -> bool:
    """Table-backend atomic claim: insert-if-absent, else steal-if-stale."""
    _ensure_table()
    now = _now_iso()
    placeholder = BridgeRecord(
        correlation_id=correlation_id,
        request_id=request_id,
        created_at=now,
        updated_at=now,
        claimed_at=now,
    )
    with _table_client() as table:
        try:
            table.create_entity(_entity(placeholder))
            return True
        except ResourceExistsError:
            pass
        # The row exists. Steal it only if it is a stale, unconfirmed
        # reservation; a confirmed row or a fresh reservation means another
        # worker owns this correlation id.
        try:
            existing_ent = dict(
                table.get_entity(
                    partition_key=_PARTITION_KEY, row_key=_row_key(correlation_id)
                )
            )
        except ResourceNotFoundError:
            # Released between our create and get — race to re-create once.
            try:
                table.create_entity(_entity(placeholder))
                return True
            except ResourceExistsError:
                return False
        rec = _record_from_entity(existing_ent)
        if rec is None or not _claim_is_stale(rec):
            return False
        # Steal via optimistic concurrency: succeed only if the row has not
        # changed since we read it, so two workers racing to steal the same
        # stale reservation cannot both win.
        steal = BridgeRecord(
            correlation_id=correlation_id,
            request_id=request_id or rec.request_id,
            created_at=rec.created_at or now,
            updated_at=now,
            claimed_at=now,
        )
        try:
            table.update_entity(
                _entity(steal),
                mode=UpdateMode.REPLACE,
                etag=existing_ent.get("odata.etag"),
                match_condition=MatchConditions.IfNotModified,
            )
            # A steal means a prior worker reserved this id and never confirmed
            # (likely crashed mid-submit). It is expected to be rare; log at INFO
            # so an unexpectedly high steal rate is visible as a worker-health or
            # stale-threshold-too-low signal.
            LOGGER.info(
                "service bus claim stole stale reservation corr=%s", correlation_id
            )
            return True
        except (ResourceModifiedError, ResourceNotFoundError):
            return False


def _release_table(correlation_id: str) -> None:
    """Delete an unconfirmed reservation row (never a confirmed one)."""
    try:
        _ensure_table()
        with _table_client() as table:
            try:
                ent = dict(
                    table.get_entity(
                        partition_key=_PARTITION_KEY, row_key=_row_key(correlation_id)
                    )
                )
            except ResourceNotFoundError:
                return
            rec = _record_from_entity(ent)
            if rec is not None and rec.openapi_job_id:
                return  # confirmed — never delete a live job's row
            try:
                # Conditional delete: only remove the row if it has NOT changed
                # since we read it. If the winner confirmed it (filled
                # openapi_job_id) in the gap between our get and delete, the
                # etag no longer matches and we leave the now-confirmed row
                # intact instead of wiping a live job.
                table.delete_entity(
                    partition_key=_PARTITION_KEY,
                    row_key=_row_key(correlation_id),
                    etag=ent.get("odata.etag"),
                    match_condition=MatchConditions.IfNotModified,
                )
            except ResourceModifiedError:
                return
    except Exception:
        LOGGER.debug(
            "release_bridge (table) best-effort skip corr=%s", correlation_id, exc_info=True
        )


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


def _claim_file(correlation_id: str, request_id: str) -> bool:
    """File-backend atomic claim under the process file lock (single-writer)."""
    now = _now_iso()
    key = _row_key(correlation_id)
    with _FILE_LOCK:
        data = _read_file_state()
        raw = data.get(key)
        if isinstance(raw, dict):
            rec = BridgeRecord.from_dict(raw)
            if not _claim_is_stale(rec):
                return False
            created = rec.created_at or now
            request_id = request_id or rec.request_id
        else:
            created = now
        data[key] = BridgeRecord(
            correlation_id=correlation_id,
            request_id=request_id,
            created_at=created,
            updated_at=now,
            claimed_at=now,
        ).to_dict()
        _write_file_state(data)
        return True


def _release_file(correlation_id: str) -> None:
    """Delete an unconfirmed reservation row from the file state (never confirmed)."""
    key = _row_key(correlation_id)
    with _FILE_LOCK:
        data = _read_file_state()
        raw = data.get(key)
        if isinstance(raw, dict) and BridgeRecord.from_dict(raw).openapi_job_id:
            return  # confirmed — never delete
        if key in data:
            del data[key]
            _write_file_state(data)

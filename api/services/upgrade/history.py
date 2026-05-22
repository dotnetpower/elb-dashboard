"""Append-blob history log for upgrade lifecycle events.

Module summary: Writes one JSON line per significant upgrade transition
(`start`, `state`, `succeeded`, `failed`, `rollback`, etc.) to an Append
Blob so an operator can review the timeline after the producing revision
has been torn down. Read path streams the tail of the blob for the SPA
history page.

Responsibility: Persistent audit trail for the self-upgrade flow.
Edit boundaries: Blob naming and event shape live here; routes/tasks
  call `record_event` / `tail_events`.
Key entry points: `record_event`, `tail_events`, `HistoryEvent`,
  `set_backend`, `InMemoryHistoryBackend`.
Risky contracts: Failures inside `record_event` MUST NOT propagate —
  audit logging is best-effort and never blocks an upgrade step. Tail
  returns at most `MAX_TAIL_ENTRIES` rows so the SPA payload stays
  bounded.
Validation: `uv run pytest -q api/tests/test_upgrade_history.py`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

HISTORY_CONTAINER = "upgrade-history"
HISTORY_BLOB = "events.log"
MAX_TAIL_ENTRIES = 200
_BLOB_ENDPOINT_ENV = "AZURE_BLOB_ENDPOINT"


@dataclass(frozen=True)
class HistoryEvent:
    """One persisted lifecycle event."""

    ts: str  # ISO-8601 UTC
    job_id: str
    event: str  # start | state | succeeded | failed | rollback_start | rollback_done | escape_hatch
    detail: dict[str, Any]

    def to_json_line(self) -> bytes:
        payload = {"ts": self.ts, "job_id": self.job_id, "event": self.event, **self.detail}
        return (json.dumps(payload, default=str) + "\n").encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes | str) -> HistoryEvent:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        payload = json.loads(raw)
        ts = str(payload.pop("ts", ""))
        job_id = str(payload.pop("job_id", ""))
        event = str(payload.pop("event", ""))
        return cls(ts=ts, job_id=job_id, event=event, detail=payload)


# ---------------------------------------------------------------------------
# Backend abstraction.
# ---------------------------------------------------------------------------


class _Backend(Protocol):
    def append(self, payload: bytes) -> None: ...

    def read_all(self) -> bytes: ...


class InMemoryHistoryBackend:
    """Test-only in-memory append-blob substitute."""

    def __init__(self) -> None:
        if not os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get(
            "ELB_ALLOW_INMEMORY_UPGRADE_HISTORY", ""
        ).lower() != "true":
            raise RuntimeError("InMemoryHistoryBackend is for tests only")
        self._buf = bytearray()
        self._lock = threading.Lock()

    def append(self, payload: bytes) -> None:
        with self._lock:
            self._buf.extend(payload)

    def read_all(self) -> bytes:
        with self._lock:
            return bytes(self._buf)


class _AzureAppendHistoryBackend:
    def __init__(self) -> None:
        endpoint = os.environ.get(_BLOB_ENDPOINT_ENV, "").strip()
        if not endpoint:
            raise RuntimeError(f"{_BLOB_ENDPOINT_ENV} is not set")
        from azure.storage.blob import BlobServiceClient

        self._svc = BlobServiceClient(account_url=endpoint, credential=get_credential())
        self._ensured = False
        self._ensure_lock = threading.Lock()

    def _container(self):  # type: ignore[no-untyped-def]
        if not self._ensured:
            with self._ensure_lock:
                if not self._ensured:
                    try:
                        self._svc.create_container(HISTORY_CONTAINER)
                    except ResourceExistsError:
                        pass
                    self._ensured = True
        return self._svc.get_container_client(HISTORY_CONTAINER)

    def _blob(self):  # type: ignore[no-untyped-def]
        blob = self._container().get_blob_client(HISTORY_BLOB)
        try:
            blob.create_append_blob()
        except ResourceExistsError:
            pass
        return blob

    def append(self, payload: bytes) -> None:
        try:
            self._blob().append_block(payload)
        except Exception as exc:
            LOGGER.warning("upgrade.history append failed: %s", exc)

    def read_all(self) -> bytes:
        try:
            return self._blob().download_blob().readall()
        except ResourceNotFoundError:
            return b""


_BACKEND_LOCK = threading.Lock()
_BACKEND: _Backend | None = None


def set_backend(backend: _Backend | None) -> None:
    global _BACKEND
    with _BACKEND_LOCK:
        _BACKEND = backend


def _backend() -> _Backend:
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is not None:
            return _BACKEND
        return _AzureAppendHistoryBackend()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def record_event(event: str, *, job_id: str, **detail: Any) -> None:
    """Best-effort: append one event to the history blob.

    Never raises — audit logging must never break the upgrade flow.
    """
    payload = HistoryEvent(
        ts=_now_iso(), job_id=job_id or "", event=event, detail=detail
    ).to_json_line()
    try:
        _backend().append(payload)
    except Exception as exc:
        LOGGER.warning("upgrade.history record_event swallowed: %s", exc)


def tail_events(*, limit: int = 50) -> list[HistoryEvent]:
    """Return the most recent ``limit`` events (newest first), capped at MAX."""
    capped = max(1, min(limit, MAX_TAIL_ENTRIES))
    try:
        raw = _backend().read_all()
    except Exception as exc:
        LOGGER.warning("upgrade.history read failed: %s", exc)
        return []
    if not raw:
        return []
    lines = [line for line in raw.split(b"\n") if line.strip()]
    tail = lines[-capped:]
    events: list[HistoryEvent] = []
    for line in tail:
        try:
            events.append(HistoryEvent.from_json(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(events))


def record_events(events: Iterable[HistoryEvent]) -> None:
    """Batch helper for tests; appends each event sequentially."""
    for event in events:
        try:
            _backend().append(event.to_json_line())
        except Exception as exc:
            LOGGER.warning("upgrade.history record_events swallowed: %s", exc)

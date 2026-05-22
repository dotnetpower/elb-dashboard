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

import hashlib
import json
import logging
import os
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

HISTORY_CONTAINER = "upgrade-history"
HISTORY_BLOB = "events.log"
MAX_TAIL_ENTRIES = 200
# Cap event age surfaced to the SPA. The append blob itself is bounded by
# the operator's container retention policy (default: forever); this
# limit only governs what the UI shows so the timeline stays scannable.
MAX_TAIL_AGE_DAYS = 180
_BLOB_ENDPOINT_ENV = "AZURE_BLOB_ENDPOINT"


@dataclass(frozen=True)
class HistoryEvent:
    """One persisted lifecycle event.

    ``event_id`` is a per-event UUID stamped at write time so the read
    path can dedupe in case the append-blob backend ever double-writes
    (network retry, partial-failure resend). It is also persisted to the
    blob alongside the rest of the payload so dedup survives a process
    restart.

    ``prev_hash`` chains each event to the previous one's SHA-256 so a
    later operator can verify the audit blob has not been tampered with
    (a manually-edited row breaks the chain at that point onward). The
    chain is computed by `record_event` at write time; tests assert end
    -to-end via `verify_chain`.
    """

    ts: str  # ISO-8601 UTC
    job_id: str
    # Known event types: start | state | succeeded | failed | rollback_start |
    # rollback_done | escape_hatch | orphan_acr_tags
    event: str
    detail: dict[str, Any]
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    prev_hash: str = ""

    def to_json_line(self) -> bytes:
        payload = {
            "ts": self.ts,
            "job_id": self.job_id,
            "event": self.event,
            "event_id": self.event_id,
            "prev_hash": self.prev_hash,
            **self.detail,
        }
        return (json.dumps(payload, default=str) + "\n").encode("utf-8")

    def content_hash(self) -> str:
        """SHA-256 of this event's canonical payload + prev_hash.

        This is the value the next event's ``prev_hash`` is compared
        against during chain verification.
        """
        canonical = json.dumps(
            {
                "ts": self.ts,
                "job_id": self.job_id,
                "event": self.event,
                "event_id": self.event_id,
                "detail": self.detail,
                "prev_hash": self.prev_hash,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, raw: bytes | str) -> HistoryEvent:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        payload = json.loads(raw)
        ts = str(payload.pop("ts", ""))
        job_id = str(payload.pop("job_id", ""))
        event = str(payload.pop("event", ""))
        event_id = str(payload.pop("event_id", "") or "")
        prev_hash = str(payload.pop("prev_hash", "") or "")
        if not event_id:
            # Backfill: events written before the event_id field landed
            # are deduped by their full payload hash, which is stable
            # for the historical record.
            digest = hashlib.sha256(
                f"{ts}|{job_id}|{event}|{json.dumps(payload, sort_keys=True, default=str)}".encode()
            ).hexdigest()
            event_id = f"sha256:{digest[:32]}"
        return cls(
            ts=ts,
            job_id=job_id,
            event=event,
            detail=payload,
            event_id=event_id,
            prev_hash=prev_hash,
        )


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
            from api.services.storage_data import (
                METADATA_BLOB_MAX_BYTES,
                read_metadata_blob_bytes,
            )

            return read_metadata_blob_bytes(
                self._blob(),
                max_bytes=METADATA_BLOB_MAX_BYTES,
                label="upgrade-history",
            )
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


# Track the last written event's hash so the next `record_event` can
# chain to it. On process start the chain is bootstrapped from the
# tail of the existing blob so the chain survives a revision restart.
_CHAIN_LOCK = threading.Lock()
_LAST_HASH: str | None = None


def _chain_predecessor() -> str:
    """Return the previous event's content hash for chaining.

    Lazy bootstrap: on the first call we walk the existing blob to find
    the last event's hash so the chain continues across restarts. A
    fresh deployment starts with the empty string.
    """
    global _LAST_HASH
    with _CHAIN_LOCK:
        if _LAST_HASH is not None:
            return _LAST_HASH
        try:
            raw = _backend().read_all()
        except Exception as exc:
            LOGGER.warning("upgrade.history chain bootstrap failed: %s", exc)
            _LAST_HASH = ""
            return _LAST_HASH
        last = ""
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                evt = HistoryEvent.from_json(line)
            except json.JSONDecodeError:
                continue
            last = evt.content_hash()
        _LAST_HASH = last
        return _LAST_HASH


def _advance_chain(evt: HistoryEvent) -> None:
    global _LAST_HASH
    with _CHAIN_LOCK:
        _LAST_HASH = evt.content_hash()


def reset_chain_for_tests() -> None:
    """Reset the in-process chain bootstrap so each test sees a clean state."""
    global _LAST_HASH
    with _CHAIN_LOCK:
        _LAST_HASH = None


def record_event(event: str, *, job_id: str, **detail: Any) -> None:
    """Best-effort: append one event to the history blob.

    Never raises — audit logging must never break the upgrade flow.
    Each event embeds the SHA-256 of the previous event's payload so a
    later reader can verify the chain has not been tampered with.
    """
    prev_hash = _chain_predecessor()
    evt = HistoryEvent(
        ts=_now_iso(), job_id=job_id or "", event=event, detail=detail, prev_hash=prev_hash
    )
    try:
        _backend().append(evt.to_json_line())
    except Exception as exc:
        LOGGER.warning("upgrade.history record_event swallowed: %s", exc)
        return
    _advance_chain(evt)


def verify_chain() -> tuple[bool, str]:
    """Walk the audit blob and verify every ``prev_hash`` links correctly.

    Returns ``(ok, reason)``. ``ok=False`` means the chain is broken at
    the position called out in ``reason`` (a row was tampered with or
    inserted out of order). The first event's ``prev_hash`` should be
    empty; subsequent events must hash-match.
    """
    try:
        raw = _backend().read_all()
    except Exception as exc:
        return False, f"read failed: {exc}"
    expected = ""
    n = 0
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            evt = HistoryEvent.from_json(line)
        except json.JSONDecodeError:
            continue
        if evt.prev_hash != expected:
            return False, (
                f"chain broken at event #{n} (event_id={evt.event_id}, "
                f"event={evt.event}): expected prev_hash={expected!r}, "
                f"got {evt.prev_hash!r}"
            )
        expected = evt.content_hash()
        n += 1
    return True, f"chain verified across {n} events"


def tail_events(*, limit: int = 50) -> list[HistoryEvent]:
    """Return the most recent ``limit`` events (newest first), capped at MAX.

    Deduplicates by ``event_id`` so a double-written event (network retry
    on the append-blob side) only appears once. Within a duplicate set,
    the first observed (oldest position) wins so the timeline preserves
    the original ordering. Events older than ``MAX_TAIL_AGE_DAYS`` are
    dropped so the UI never surfaces year-old runs that distract from
    the current state.
    """
    capped = max(1, min(limit, MAX_TAIL_ENTRIES))
    try:
        raw = _backend().read_all()
    except Exception as exc:
        LOGGER.warning("upgrade.history read failed: %s", exc)
        return []
    if not raw:
        return []
    lines = [line for line in raw.split(b"\n") if line.strip()]
    events: list[HistoryEvent] = []
    seen: set[str] = set()
    cutoff = datetime.now(UTC).timestamp() - MAX_TAIL_AGE_DAYS * 86400
    for line in lines:
        try:
            evt = HistoryEvent.from_json(line)
        except json.JSONDecodeError:
            continue
        if evt.event_id in seen:
            continue
        # Age cap. Malformed or missing ts → keep (cheaper than guessing).
        if evt.ts:
            try:
                evt_ts = datetime.fromisoformat(evt.ts).timestamp()
                if evt_ts < cutoff:
                    continue
            except ValueError:
                pass
        seen.add(evt.event_id)
        events.append(evt)
    return list(reversed(events))[:capped]


def record_events(events: Iterable[HistoryEvent]) -> None:
    """Batch helper for tests; appends each event sequentially."""
    for event in events:
        try:
            _backend().append(event.to_json_line())
        except Exception as exc:
            LOGGER.warning("upgrade.history record_events swallowed: %s", exc)

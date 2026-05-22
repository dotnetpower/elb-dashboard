"""Storage Table-backed state row for the in-app self-upgrade flow.

Module summary: Reads and writes the single ``upgradestate`` row that drives
the upgrade UI and Celery tasks. Uses ETag-based optimistic concurrency so
two operators clicking "Upgrade" simultaneously cannot both proceed. A
swappable backend is provided so unit tests can run without a real Azure
Tables endpoint.

Responsibility: Persistent state for the self-upgrade flow (single row).
Edit boundaries: Table I/O and row schema live here. Routes/tasks call the
  module-level helpers; they never reach for `azure.data.tables` themselves.
Key entry points: `UpgradeState`, `get_state`, `update_state`, `set_backend`,
  `InMemoryBackend`, `RowEtagMismatch`.
Risky contracts: Single shared row keyed by (control-plane, current). Concurrent
  writers race on ETag; callers should retry on `RowEtagMismatch`. Setting
  the backend to an InMemoryBackend is only valid in tests.
Validation: `uv run pytest -q api/tests/test_upgrade_state.py`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "upgradestate"
_PARTITION_KEY = "control-plane"
_ROW_KEY = "current"

STATE_IDLE = "idle"
STATE_CHECKING = "checking"
STATE_QUEUED = "queued"
STATE_FETCHING = "fetching"
STATE_BUILDING = "building"
STATE_PATCHING = "patching"
STATE_ROLLING_OUT = "rolling_out"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED_PRE = "failed_pre"
STATE_FAILED_ROLLOUT = "failed_rollout"
STATE_ROLLING_BACK = "rolling_back"
STATE_ROLLED_BACK = "rolled_back"
STATE_ROLLBACK_FAILED = "rollback_failed"

VALID_STATES = frozenset(
    {
        STATE_IDLE,
        STATE_CHECKING,
        STATE_QUEUED,
        STATE_FETCHING,
        STATE_BUILDING,
        STATE_PATCHING,
        STATE_ROLLING_OUT,
        STATE_SUCCEEDED,
        STATE_FAILED_PRE,
        STATE_FAILED_ROLLOUT,
        STATE_ROLLING_BACK,
        STATE_ROLLED_BACK,
        STATE_ROLLBACK_FAILED,
    }
)


class RowEtagMismatch(RuntimeError):
    """Raised when a CAS update races against a concurrent writer."""


@dataclass
class UpgradeState:
    """Snapshot of the upgrade state row.

    String defaults (not ``None``) are used everywhere because Azure Tables
    represents missing columns as absent attributes; round-tripping ``None``
    causes type drift across reads. The ``etag`` field is excluded from
    equality comparisons so tests can assert on payload content alone.

    Schema is grown additively across PRs:
      * PR1 (this) — read-only fields populated by the discovery flow.
      * PR2 — build_log_blob, target_*, job_id are populated.
      * PR3 — current_images_json, rollback_target_json, rollback_available_until,
        phase_*, started_*, and a future `error` field for execution failures.
    """

    running_version: str = ""
    running_sha: str = ""
    running_revision: str = ""
    current_images_json: str = ""
    latest_version: str = ""
    latest_sha: str = ""
    latest_checked_at: str = ""
    git_remote: str = ""
    state: str = STATE_IDLE
    target_version: str = ""
    target_sha: str = ""
    job_id: str = ""
    started_by_oid: str = ""
    started_at: str = ""
    phase_detail: str = ""
    phase_progress: int = 0
    build_log_blob: str = ""
    rollback_target_json: str = ""
    rollback_available_until: str = ""
    updated_at: str = ""
    etag: str = field(default="", compare=False)

    def current_images(self) -> dict[str, str]:
        return _load_json_dict(self.current_images_json)

    def rollback_target(self) -> dict[str, str]:
        return _load_json_dict(self.rollback_target_json)

    def to_public_dict(self) -> dict[str, Any]:
        """Serialise the row for the SPA. Drops internal JSON-encoded blobs
        in favour of expanded dicts and elides the ETag (auth-sensitive)."""
        d = asdict(self)
        d.pop("etag", None)
        d["current_images"] = self.current_images()
        d["rollback_target"] = self.rollback_target()
        d.pop("current_images_json", None)
        d.pop("rollback_target_json", None)
        return d


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_json_dict(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


# ---------------------------------------------------------------------------
# Backend abstraction so tests can run without an Azure Tables endpoint.
# ---------------------------------------------------------------------------


class _Backend(Protocol):
    def get(self) -> UpgradeState: ...

    def upsert(self, state: UpgradeState, *, expected_etag: str) -> UpgradeState: ...


class InMemoryBackend:
    """Test-only backend that holds the row in process memory.

    Refuses to construct outside a test context so a misconfiguration in
    production cannot silently lose state. Pytest sets `PYTEST_CURRENT_TEST`
    on every test; production processes never have it set.
    """

    def __init__(self) -> None:
        if not os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get(
            "ELB_ALLOW_INMEMORY_UPGRADE_STATE", ""
        ).lower() != "true":
            raise RuntimeError(
                "InMemoryBackend is for tests only; set ELB_ALLOW_INMEMORY_UPGRADE_STATE=true "
                "to opt in (never do this in deployed Container Apps)."
            )
        self._row: UpgradeState | None = None
        self._etag = ""
        self._etag_counter = 0
        # Reentrant — `upsert` returns a fresh snapshot via the same code
        # path `get` uses, so the lock must be re-acquirable on the same
        # thread.
        self._lock = threading.RLock()

    def _snapshot(self) -> UpgradeState:
        if self._row is None:
            return UpgradeState()
        copy = UpgradeState(**{k: v for k, v in asdict(self._row).items() if k != "etag"})
        copy.etag = self._etag
        return copy

    def get(self) -> UpgradeState:
        with self._lock:
            return self._snapshot()

    def upsert(self, state: UpgradeState, *, expected_etag: str) -> UpgradeState:
        with self._lock:
            if self._row is not None and expected_etag and expected_etag != self._etag:
                raise RowEtagMismatch(
                    f"in-memory etag mismatch: have={self._etag!r} expected={expected_etag!r}"
                )
            self._etag_counter += 1
            self._etag = f"W/\"v{self._etag_counter}\""
            stored = UpgradeState(**{k: v for k, v in asdict(state).items() if k != "etag"})
            stored.etag = self._etag
            self._row = stored
            return self._snapshot()


class _AzureTablesBackend:
    """Production backend backed by Azure Storage Tables."""

    _ensured: ClassVar[set[str]] = set()
    _ensured_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        endpoint = os.environ.get(_TABLE_ENDPOINT_ENV, "").strip()
        if not endpoint:
            raise RuntimeError(
                f"{_TABLE_ENDPOINT_ENV} is not set; upgrade state requires Azure Tables."
            )
        self._endpoint = endpoint
        self._cred = get_credential()
        # Lazy: only ensure the table on first read/write so a transient
        # Tables endpoint failure does not block api sidecar startup.

    def _ensure_table(self) -> None:
        key = f"{self._endpoint}/{_TABLE_NAME}"
        if key in self._ensured:
            return
        with self._ensured_lock:
            if key in self._ensured:
                return
            with TableServiceClient(endpoint=self._endpoint, credential=self._cred) as svc:
                try:
                    svc.create_table_if_not_exists(_TABLE_NAME)
                except AttributeError:
                    try:
                        svc.create_table(_TABLE_NAME)
                    except ResourceExistsError:
                        pass
            self._ensured.add(key)

    def _client(self) -> TableClient:
        return TableClient(
            endpoint=self._endpoint, table_name=_TABLE_NAME, credential=self._cred
        )

    def get(self) -> UpgradeState:
        self._ensure_table()
        with self._client() as table:
            try:
                entity = table.get_entity(_PARTITION_KEY, _ROW_KEY)
            except ResourceNotFoundError:
                return UpgradeState()
            return _entity_to_state(entity)

    def upsert(self, state: UpgradeState, *, expected_etag: str) -> UpgradeState:
        self._ensure_table()
        new_entity = _state_to_entity(state)
        with self._client() as table:
            if expected_etag:
                try:
                    table.update_entity(
                        new_entity,
                        mode=UpdateMode.REPLACE,
                        etag=expected_etag,
                        match_condition=MatchConditions.IfNotModified,
                    )
                except ResourceModifiedError as exc:
                    raise RowEtagMismatch(str(exc)) from exc
                except ResourceNotFoundError as exc:
                    # Row was deleted between read and write; treat as CAS miss.
                    raise RowEtagMismatch(str(exc)) from exc
            else:
                # First write — upsert so we tolerate a row created concurrently.
                table.upsert_entity(new_entity, mode=UpdateMode.REPLACE)
        return self.get()


_BACKEND_LOCK = threading.Lock()
_BACKEND: _Backend | None = None


def set_backend(backend: _Backend | None) -> None:
    """Install a backend (or reset to default Azure Tables when ``None``).

    Intended for tests. Production code should not call this.
    """
    global _BACKEND
    with _BACKEND_LOCK:
        _BACKEND = backend


def _backend() -> _Backend:
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is not None:
            return _BACKEND
        return _AzureTablesBackend()


def _entity_to_state(entity: Any) -> UpgradeState:
    metadata = getattr(entity, "metadata", None) or {}
    etag = ""
    if isinstance(metadata, dict):
        etag = str(metadata.get("etag", "") or "")
    if not etag:
        etag = str(entity.get("odata.etag", "") or "")
    return UpgradeState(
        running_version=str(entity.get("running_version", "")),
        running_sha=str(entity.get("running_sha", "")),
        running_revision=str(entity.get("running_revision", "")),
        current_images_json=str(entity.get("current_images_json", "")),
        latest_version=str(entity.get("latest_version", "")),
        latest_sha=str(entity.get("latest_sha", "")),
        latest_checked_at=str(entity.get("latest_checked_at", "")),
        git_remote=str(entity.get("git_remote", "")),
        state=str(entity.get("state", STATE_IDLE)),
        target_version=str(entity.get("target_version", "")),
        target_sha=str(entity.get("target_sha", "")),
        job_id=str(entity.get("job_id", "")),
        started_by_oid=str(entity.get("started_by_oid", "")),
        started_at=str(entity.get("started_at", "")),
        phase_detail=str(entity.get("phase_detail", "")),
        phase_progress=int(entity.get("phase_progress") or 0),
        build_log_blob=str(entity.get("build_log_blob", "")),
        rollback_target_json=str(entity.get("rollback_target_json", "")),
        rollback_available_until=str(entity.get("rollback_available_until", "")),
        updated_at=str(entity.get("updated_at", "")),
        etag=etag,
    )


def _state_to_entity(state: UpgradeState) -> dict[str, Any]:
    return {
        "PartitionKey": _PARTITION_KEY,
        "RowKey": _ROW_KEY,
        "running_version": state.running_version,
        "running_sha": state.running_sha,
        "running_revision": state.running_revision,
        "current_images_json": state.current_images_json,
        "latest_version": state.latest_version,
        "latest_sha": state.latest_sha,
        "latest_checked_at": state.latest_checked_at,
        "git_remote": state.git_remote,
        "state": state.state,
        "target_version": state.target_version,
        "target_sha": state.target_sha,
        "job_id": state.job_id,
        "started_by_oid": state.started_by_oid,
        "started_at": state.started_at,
        "phase_detail": state.phase_detail,
        "phase_progress": int(state.phase_progress),
        "build_log_blob": state.build_log_blob,
        "rollback_target_json": state.rollback_target_json,
        "rollback_available_until": state.rollback_available_until,
        "updated_at": state.updated_at,
    }


def get_state() -> UpgradeState:
    """Read the singleton state row, returning defaults when not present."""
    return _backend().get()


def update_state(mutate: Callable[[UpgradeState], None]) -> UpgradeState:
    """Read-modify-write helper with ETag CAS.

    The caller-supplied ``mutate`` is applied to a fresh snapshot of the row
    and the result is written back conditional on the ETag we read it with.
    A `RowEtagMismatch` indicates a concurrent writer raced us; callers
    that need to be authoritative should retry. The state-machine flow
    (introduced in PR3) will wrap this with explicit precondition checks.
    """
    backend = _backend()
    current = backend.get()
    expected_etag = current.etag
    mutate(current)
    current.updated_at = _now_iso()
    return backend.upsert(current, expected_etag=expected_etag)

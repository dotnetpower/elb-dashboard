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
import random
import threading
import time
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
# Backward-compat: rows written by an older deployment that briefly
# carried STATE_CHECKING should not crash this build. The reader
# coerces an unrecognised state into IDLE so the upgrade flow remains
# operable. This keeps `VALID_STATES` strict for new writes while not
# bricking old data.
_LEGACY_TO_IDLE: frozenset[str] = frozenset({"checking"})


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
    idempotency_key: str = ""
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
            if expected_etag:
                if self._row is None:
                    raise RowEtagMismatch(
                        "in-memory row was deleted between read and write"
                    )
                if expected_etag != self._etag:
                    raise RowEtagMismatch(
                        f"in-memory etag mismatch: have={self._etag!r} "
                        f"expected={expected_etag!r}"
                    )
            else:
                # First write semantics: refuse if a concurrent creator
                # already populated the row (mirrors the Azure backend's
                # `create_entity`-on-no-etag behaviour).
                if self._row is not None:
                    raise RowEtagMismatch(
                        "first-write race: row was created by a concurrent writer"
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
                # First-ever write. Use create_entity (fails if the row already
                # exists) so a concurrent creator surfaces as a CAS miss. The
                # previous unconditional `upsert_entity` here let two
                # operators race past `cas_state(IDLE -> QUEUED)` on a fresh
                # deployment, silently overwriting each other's job_id /
                # target_version on the single shared row.
                try:
                    table.create_entity(new_entity)
                except ResourceExistsError as exc:
                    raise RowEtagMismatch(
                        "first-write race: another operator created the state row"
                    ) from exc
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
    raw_state = str(entity.get("state", STATE_IDLE))
    # Legacy coercion: a row written by a previous deployment that briefly
    # used a now-removed state name lands in IDLE rather than crashing
    # the reader. The next operator-driven transition then re-anchors
    # the row in a known state.
    if raw_state in _LEGACY_TO_IDLE:
        LOGGER.warning(
            "upgrade.state: coercing legacy state %r to %r on read",
            raw_state,
            STATE_IDLE,
        )
        raw_state = STATE_IDLE
    return UpgradeState(
        running_version=str(entity.get("running_version", "")),
        running_sha=str(entity.get("running_sha", "")),
        running_revision=str(entity.get("running_revision", "")),
        current_images_json=str(entity.get("current_images_json", "")),
        latest_version=str(entity.get("latest_version", "")),
        latest_sha=str(entity.get("latest_sha", "")),
        latest_checked_at=str(entity.get("latest_checked_at", "")),
        git_remote=str(entity.get("git_remote", "")),
        state=raw_state,
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
        idempotency_key=str(entity.get("idempotency_key", "")),
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
        "idempotency_key": state.idempotency_key,
        "updated_at": state.updated_at,
    }


def get_state() -> UpgradeState:
    """Read the singleton state row, returning defaults when not present.

    Applies legacy-state coercion (see `_LEGACY_TO_IDLE`) so a row
    written by an older deployment with a now-removed state name does
    not propagate up to callers as an unknown state.
    """
    row = _backend().get()
    if row.state in _LEGACY_TO_IDLE:
        LOGGER.warning(
            "upgrade.state: coercing legacy state %r to %r on get_state",
            row.state,
            STATE_IDLE,
        )
        row.state = STATE_IDLE
    return row


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


class StateTransitionRefused(RuntimeError):
    """Raised when a CAS state transition is attempted from the wrong state."""

    def __init__(self, current: str, expected: str, target: str) -> None:
        super().__init__(
            f"transition refused: cannot move state {current!r} -> {target!r} "
            f"(precondition required {expected!r})"
        )
        self.current = current
        self.expected = expected
        self.target = target


_CAS_BACKOFF_SCHEDULE_SECONDS: tuple[float, ...] = (0.02, 0.05, 0.1, 0.2, 0.4)
# Maximum jitter ratio applied per backoff step. With ratio 0.5, a base
# of 100ms becomes uniformly distributed over [50ms, 150ms]. Jitter
# breaks lock-step retry waves when N workers all wake on the same
# beat tick — a thundering-herd scenario the deterministic schedule
# alone cannot disperse.
_CAS_JITTER_RATIO: float = 0.5
# Jitter is for spread, not security.
_jitter_rng = random.Random()  # noqa: S311


def cas_state(
    *,
    expected_state: str,
    new_state: str,
    mutate: Callable[[UpgradeState], None] | None = None,
    retries: int = 5,
    sleeper: Callable[[float], None] = time.sleep,
) -> UpgradeState:
    """Transition the persisted state row's `state` field via CAS.

    Refuses the write when the row is not in ``expected_state``; this is
    the gate the upgrade flow uses to prevent concurrent operators from
    starting overlapping upgrades (only `idle -> queued` is valid). When
    the underlying Tables ETag is stale we retry up to ``retries`` times
    with exponential backoff so a flurry of racing readers does not
    derail a legitimate transition. ``StateTransitionRefused`` is NOT
    retried (a wrong-state CAS is a real precondition failure, not a
    transient race) and propagates immediately.
    """
    if new_state not in VALID_STATES:
        raise ValueError(f"unknown target state {new_state!r}")

    attempts = max(1, retries + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return _do_cas(expected_state, new_state, mutate)
        except RowEtagMismatch as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            delay_idx = min(attempt, len(_CAS_BACKOFF_SCHEDULE_SECONDS) - 1)
            base = _CAS_BACKOFF_SCHEDULE_SECONDS[delay_idx]
            # Jitter spread: uniform over [base*(1-r), base*(1+r)].
            spread = base * _CAS_JITTER_RATIO
            delay = base + _jitter_rng.uniform(-spread, spread)
            try:
                sleeper(max(0.0, delay))
            except Exception as sleep_exc:
                # A test sleeper that raises is treated as "do not sleep".
                LOGGER.debug("cas_state sleeper raised; skipping backoff: %s", sleep_exc)
            continue
    # Should be unreachable: _do_cas either returns or raises
    # StateTransitionRefused before falling out.
    raise RowEtagMismatch(str(last_exc) if last_exc else "cas_state exhausted retries")


def _do_cas(
    expected_state: str,
    new_state: str,
    mutate: Callable[[UpgradeState], None] | None,
) -> UpgradeState:
    backend = _backend()
    current = backend.get()
    if current.state != expected_state:
        raise StateTransitionRefused(current.state, expected_state, new_state)
    expected_etag = current.etag
    current.state = new_state
    if mutate is not None:
        mutate(current)
    current.updated_at = _now_iso()
    return backend.upsert(current, expected_etag=expected_etag)

"""Per-cluster Auto-Stop preferences for idle AKS cost saver.

Responsibility: Persist opt-in idle-auto-stop preferences keyed by
    (subscription_id, resource_group, cluster_name). Mirrors the
    `auto_warmup` storage pattern: Azure Tables in deployed Container Apps,
    JSON file under `.logs/local/state/` for laptop dev.
Edit boundaries: Storage + serialisation only. The "should we stop?"
    decision lives in `auto_stop_evaluator`; the Celery driver lives in
    `api/tasks/azure/idle_autostop.py`. Do NOT call the AKS SDK here.
Key entry points: `AutoStopPreference`, `get_auto_stop_preference`,
    `save_auto_stop_preference`, `list_auto_stop_preferences`,
    `extend_auto_stop_preference`, `mark_auto_stop_event`.
Risky contracts: `preference_key` shape is shared with the Table
    PartitionKey — changing it orphans existing rows.
Validation: `uv run pytest -q api/tests/test_auto_stop.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from api.services.preference_concurrency import (
    PreferenceUpdateConflict,
    cas_retry,
)

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "autostop"
_TYPE = "auto_stop"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"
_ENSURED_TABLES: set[str] = set()
_ENSURED_TABLES_LOCK = threading.Lock()
_AUTOSTOP_TABLE_POOLED: TableClient | None = None
_AUTOSTOP_TABLE_POOL_LOCK = threading.Lock()

# UX-tuned default. 60 min is long enough that a researcher returning from
# lunch does not trigger a stop, short enough that overnight idle saves
# meaningful cost. See docs/features_change/2026-05/2026-05-29-aks-idle-auto-stop.md.
DEFAULT_IDLE_MINUTES = 60
ALLOWED_IDLE_MINUTES = (15, 30, 60, 120, 240)
# Cooldown after a stop so a researcher who immediately restarts is not
# kicked back into a stop loop. Also covers the (~3-5 min) Azure AKS
# stop LRO settling window.
DEFAULT_COOLDOWN_MINUTES = 30
EXTEND_GRANT_MINUTES = 30


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def _clamp_idle_minutes(value: Any) -> int:
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return DEFAULT_IDLE_MINUTES
    if candidate in ALLOWED_IDLE_MINUTES:
        return candidate
    # Out-of-band values clamp to the nearest allowed bucket so an old
    # client cannot push the cluster into a 1-min loop or a 99-year wait.
    return min(ALLOWED_IDLE_MINUTES, key=lambda allowed: abs(allowed - candidate))


@dataclass
class AutoStopPreference:
    """Persisted idle-auto-stop preference for a single AKS cluster."""

    subscription_id: str
    resource_group: str
    cluster_name: str
    enabled: bool = False
    idle_minutes: int = DEFAULT_IDLE_MINUTES
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES
    last_stop_at: str = ""
    last_stop_reason: str = ""
    last_skip_at: str = ""
    last_skip_reason: str = ""
    extend_until: str = ""
    updated_at: str = ""
    # Critique #9.2: ``updated_at`` and ``last_skip_at`` BOTH drift forward
    # on every warn tick because ``mark_auto_stop_event`` writes them.
    # The evaluator needs a STABLE anchor for the "no jobs ever observed"
    # branch (otherwise the 60-min idle clock keeps getting pushed back
    # by warn ticks themselves and the cluster takes 90-120 min to stop).
    # ``created_at`` is set ONCE on first save and never touched again.
    # Legacy rows (no ``created_at``) keep the old drifting behaviour;
    # next time the user toggles the pref the field will populate.
    created_at: str = ""
    owner_oid: str = ""
    tenant_id: str = ""
    # Optimistic-concurrency token populated by ``_get_*`` reads. NEVER
    # persisted in ``payload_json`` (excluded from ``to_dict``) — the Azure
    # Tables backend carries it in entity metadata, and the file backend
    # synthesises it from a content hash. Excluded from equality so
    # round-trip dataclass comparisons stay payload-only.
    etag: str = field(default="", compare=False, repr=False)

    @property
    def key(self) -> str:
        return preference_key(self.subscription_id, self.resource_group, self.cluster_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "resource_group": self.resource_group,
            "cluster_name": self.cluster_name,
            "enabled": self.enabled,
            "idle_minutes": self.idle_minutes,
            "cooldown_minutes": self.cooldown_minutes,
            "last_stop_at": self.last_stop_at,
            "last_stop_reason": self.last_stop_reason,
            "last_skip_at": self.last_skip_at,
            "last_skip_reason": self.last_skip_reason,
            "extend_until": self.extend_until,
            "updated_at": self.updated_at,
            "created_at": self.created_at,
            "owner_oid": self.owner_oid,
            "tenant_id": self.tenant_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AutoStopPreference:
        return cls(
            subscription_id=str(value.get("subscription_id") or ""),
            resource_group=str(value.get("resource_group") or ""),
            cluster_name=str(value.get("cluster_name") or ""),
            enabled=bool(value.get("enabled", False)),
            idle_minutes=_clamp_idle_minutes(value.get("idle_minutes")),
            cooldown_minutes=int(value.get("cooldown_minutes") or DEFAULT_COOLDOWN_MINUTES),
            last_stop_at=str(value.get("last_stop_at") or ""),
            last_stop_reason=str(value.get("last_stop_reason") or ""),
            last_skip_at=str(value.get("last_skip_at") or ""),
            last_skip_reason=str(value.get("last_skip_reason") or ""),
            extend_until=str(value.get("extend_until") or ""),
            updated_at=str(value.get("updated_at") or ""),
            created_at=str(value.get("created_at") or ""),
            owner_oid=str(value.get("owner_oid") or ""),
            tenant_id=str(value.get("tenant_id") or ""),
        )


def preference_key(subscription_id: str, resource_group: str, cluster_name: str) -> str:
    raw = f"{subscription_id}:{resource_group}:{cluster_name}"
    return "auto_stop:" + re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)


def normalise_preference(value: dict[str, Any]) -> AutoStopPreference:
    pref = AutoStopPreference.from_dict(value)
    if not pref.subscription_id:
        raise ValueError("subscription_id is required")
    if not pref.resource_group:
        raise ValueError("resource_group is required")
    if not pref.cluster_name:
        raise ValueError("cluster_name is required")
    pref.idle_minutes = _clamp_idle_minutes(pref.idle_minutes)
    pref.updated_at = _now_iso()
    # Critique #9.2: stamp created_at exactly once. If the input value
    # already carried one (e.g. import / migration path), preserve it.
    if not pref.created_at:
        pref.created_at = pref.updated_at
    return pref


def _use_table_backend() -> bool:
    """Mirror `auto_warmup._use_table_backend`.

    Local-dev escape: a workstation `az login` identity often lacks Storage
    Table RBAC on the platform account; falling back to the file backend
    keeps the worker healthy.
    """
    return bool(
        os.environ.get(_TABLE_ENDPOINT_ENV) and os.environ.get("CONTAINER_APP_NAME")
    )


def save_auto_stop_preference(pref: AutoStopPreference) -> AutoStopPreference:
    """Persist the preference. Mode depends on ``pref.etag``:

    * Empty ``pref.etag`` — unconditional upsert (legacy first-write
      semantics; the route handler that accepts a user PUT does this so
      the user always wins over a missing-row state).
    * Non-empty ``pref.etag`` — conditional update (Azure Tables
      ``If-Match``; raises :class:`PreferenceUpdateConflict` when the
      stored ETag has moved on). Background bookkeeping writers
      (``mark_auto_stop_event`` / ``extend_auto_stop_preference``) set
      this from a fresh read so a sibling write cannot be silently
      clobbered.
    """
    if _use_table_backend():
        new_etag = _save_table(pref)
    else:
        new_etag = _save_file(pref)
    pref.etag = new_etag
    return pref


def get_auto_stop_preference(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> AutoStopPreference | None:
    key = preference_key(subscription_id, resource_group, cluster_name)
    if _use_table_backend():
        return _get_table(key)
    return _get_file(key)


def list_auto_stop_preferences(limit: int = 100) -> list[AutoStopPreference]:
    if _use_table_backend():
        return _list_table(limit)
    return _list_file(limit)


def extend_auto_stop_preference(
    pref: AutoStopPreference,
    *,
    minutes: int = EXTEND_GRANT_MINUTES,
) -> AutoStopPreference:
    """Push the next-stop deadline out by `minutes`.

    Used by the SPA "Extend" button — does not change `enabled` or
    `idle_minutes`. The next evaluator tick treats the cluster as active
    until `extend_until` passes.

    Atomicity: re-reads the latest row (with its ETag) and writes back
    with an Azure Tables ``If-Match`` conditional update. If a sibling
    PUT slips in between the read and the write the CAS save raises
    :class:`PreferenceUpdateConflict`; ``cas_retry`` then refreshes the
    snapshot and retries (bounded by
    :data:`api.services.preference_concurrency.DEFAULT_CAS_MAX_ATTEMPTS`).
    On exhaustion the conflict surfaces — the caller is the SPA route,
    which translates it into a 409 Conflict and the user re-sends the
    Extend press.
    """
    grant = max(1, min(int(minutes or EXTEND_GRANT_MINUTES), 24 * 60))

    def _attempt() -> AutoStopPreference:
        latest = get_auto_stop_preference(
            pref.subscription_id, pref.resource_group, pref.cluster_name
        )
        base = latest if latest is not None else pref
        next_pref = AutoStopPreference.from_dict(base.to_dict())
        # Carry the ETag through so the save is conditional on the
        # exact row we just read. ``latest`` is None on a first-write
        # path; in that case the empty etag falls through to an
        # unconditional upsert (legacy behaviour).
        next_pref.etag = base.etag
        next_pref.extend_until = (
            datetime.now(UTC) + timedelta(minutes=grant)
        ).isoformat(timespec="seconds")
        next_pref.updated_at = _now_iso()
        return save_auto_stop_preference(next_pref)

    return cas_retry(_attempt, operation="auto_stop.extend")


def mark_auto_stop_event(
    pref: AutoStopPreference,
    *,
    stopped: bool,
    reason: str,
) -> AutoStopPreference:
    """Record the outcome of an evaluator tick.

    `stopped=True` records a real stop; `stopped=False` records a skip
    (idle but blocked by guard). The fields feed the SPA's "last
    evaluation" line and the cooldown gate.

    Atomicity: this helper re-fetches the latest persisted row (with
    its ETag) before writing and uses Azure Tables ``If-Match``
    conditional update so a concurrent ``PUT /api/aks/autostop`` (e.g.
    the user toggled ``enabled=False`` between beat-decide and
    beat-write) cannot be silently reverted by the in-memory ``pref``
    snapshot the beat task is holding. Only the bookkeeping fields
    (``last_stop_*`` / ``last_skip_*`` / ``updated_at``) are written
    from this path — user-owned fields (``enabled``, ``idle_minutes``,
    ``cooldown_minutes``, ``extend_until``, ``owner_oid``,
    ``tenant_id``) are always taken from the freshly-read row. On an
    ETag conflict ``cas_retry`` refreshes the snapshot and retries
    (bounded by
    :data:`api.services.preference_concurrency.DEFAULT_CAS_MAX_ATTEMPTS`).
    If retries are exhausted the helper logs a warning and returns the
    in-memory next-state without persisting it — a bookkeeping miss
    is preferred over clobbering whatever the sibling writer just
    persisted. When the row no longer exists (user deleted the pref),
    we silently no-op and return the in-memory snapshot — there is
    nothing to update.
    """
    fallback: AutoStopPreference | None = None

    def _attempt() -> AutoStopPreference:
        nonlocal fallback
        latest = get_auto_stop_preference(
            pref.subscription_id, pref.resource_group, pref.cluster_name
        )
        base = latest if latest is not None else pref
        next_pref = AutoStopPreference.from_dict(base.to_dict())
        next_pref.etag = base.etag
        now = _now_iso()
        if stopped:
            next_pref.last_stop_at = now
            next_pref.last_stop_reason = reason[:200]
        else:
            next_pref.last_skip_at = now
            next_pref.last_skip_reason = reason[:200]
        next_pref.updated_at = now
        fallback = next_pref
        if latest is None:
            # Row vanished mid-tick; do not resurrect a stale preference.
            return next_pref
        return save_auto_stop_preference(next_pref)

    try:
        return cas_retry(_attempt, operation="auto_stop.mark_event")
    except PreferenceUpdateConflict:
        # Bookkeeping write: log and return the in-memory snapshot
        # rather than re-raising. The sibling writer that won the CAS
        # already persisted user-owned fields; a momentarily stale
        # ``last_skip_at`` is the lesser evil compared to a 500 from a
        # background beat tick.
        LOGGER.warning(
            "auto_stop.mark_auto_stop_event giving up after CAS exhaustion; "
            "in-memory snapshot returned without persisting",
        )
        return fallback if fallback is not None else pref


def is_extended(pref: AutoStopPreference, *, now: datetime | None = None) -> bool:
    """Return True when the user pressed Extend and the grant has not expired."""
    until = _parse_iso(pref.extend_until)
    if until is None:
        return False
    current = now or datetime.now(UTC)
    return until > current


def is_in_cooldown(pref: AutoStopPreference, *, now: datetime | None = None) -> bool:
    """Return True when we recently stopped this cluster and must wait."""
    last = _parse_iso(pref.last_stop_at)
    if last is None:
        return False
    current = now or datetime.now(UTC)
    cooldown = max(1, int(pref.cooldown_minutes or DEFAULT_COOLDOWN_MINUTES))
    return current < last + timedelta(minutes=cooldown)


def _entity_from_pref(pref: AutoStopPreference) -> dict[str, Any]:
    return {
        "PartitionKey": pref.key,
        "RowKey": "current",
        "type": _TYPE,
        "status": "enabled" if pref.enabled else "disabled",
        "updated_at": pref.updated_at or _now_iso(),
        "owner_oid": pref.owner_oid,
        "tenant_id": pref.tenant_id,
        "payload_json": json.dumps(pref.to_dict(), default=str),
    }


def _pref_from_entity(entity: dict[str, Any]) -> AutoStopPreference | None:
    try:
        payload = json.loads(str(entity.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        return None
    return AutoStopPreference.from_dict(payload)


def _table_client() -> TableClient:
    global _AUTOSTOP_TABLE_POOLED
    pool = _AUTOSTOP_TABLE_POOLED
    if pool is not None:
        return pool  # type: ignore[return-value]
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _AUTOSTOP_TABLE_POOL_LOCK:
        if _AUTOSTOP_TABLE_POOLED is None:
            from api.services.state_repo import _PooledTableClient

            _AUTOSTOP_TABLE_POOLED = _PooledTableClient(  # type: ignore[assignment]
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _AUTOSTOP_TABLE_POOLED  # type: ignore[return-value]


def _reset_autostop_table_pool() -> None:
    """Test hook + credential-reset safety valve."""
    global _AUTOSTOP_TABLE_POOLED
    with _AUTOSTOP_TABLE_POOL_LOCK:
        pool = _AUTOSTOP_TABLE_POOLED
        _AUTOSTOP_TABLE_POOLED = None
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


def _save_table(pref: AutoStopPreference) -> str:
    """Persist ``pref`` and return the new ETag.

    When ``pref.etag`` is empty the write is an unconditional upsert
    (legacy first-write path used by SPA PUT). When ``pref.etag`` is
    non-empty we issue ``update_entity`` with ``If-Match=pref.etag``;
    a stored row that has moved on raises
    :class:`PreferenceUpdateConflict` so the caller can retry on the
    fresh state.
    """
    _ensure_table()
    entity = _entity_from_pref(pref)
    with _table_client() as table:
        if pref.etag:
            try:
                response = table.update_entity(
                    entity,
                    mode=UpdateMode.REPLACE,
                    etag=pref.etag,
                    match_condition=MatchConditions.IfNotModified,
                )
            except ResourceModifiedError as exc:
                raise PreferenceUpdateConflict(
                    f"auto_stop row {pref.key!r} changed since last read"
                ) from exc
            except ResourceNotFoundError:
                # Row vanished between our read and our write. Fall back
                # to an unconditional upsert — either we recreate it (a
                # first-time write semantically) or a sibling will, and
                # the caller's retry loop will reconverge.
                response = table.upsert_entity(entity, mode=UpdateMode.REPLACE)
        else:
            response = table.upsert_entity(entity, mode=UpdateMode.REPLACE)
    return _extract_etag(response)


def _extract_etag(response: Any) -> str:
    if response is None:
        return ""
    if isinstance(response, dict):
        return str(response.get("etag") or response.get("odata.etag") or "")
    etag = getattr(response, "etag", None)
    if etag:
        return str(etag)
    return ""


def _get_table(key: str) -> AutoStopPreference | None:
    _ensure_table()
    with _table_client() as table:
        try:
            entity = table.get_entity(partition_key=key, row_key="current")
        except ResourceNotFoundError:
            return None
        metadata = getattr(entity, "metadata", None) or {}
        entity_dict = dict(entity)
    etag = str(metadata.get("etag") or entity_dict.get("odata.etag") or "")
    pref = _pref_from_entity(entity_dict)
    if pref is not None and etag:
        pref.etag = etag
    return pref


def _list_table(limit: int) -> list[AutoStopPreference]:
    prefs: list[AutoStopPreference] = []
    _ensure_table()
    with _table_client() as table:
        rows = table.query_entities(f"type eq '{_TYPE}'", results_per_page=limit)
        for row in rows:
            pref = _pref_from_entity(dict(row))
            if pref is not None:
                prefs.append(pref)
            if len(prefs) >= limit:
                break
    return prefs


def _state_file() -> Path:
    default_root = Path(__file__).resolve().parents[2] / ".logs" / "local" / "state"
    root = Path(os.environ.get(_LOCAL_STATE_ENV, str(default_root)))
    return root / "auto_stop.json"


# Per-state-file ``threading.Lock`` registry. Replaces the previous
# sibling ``.lock`` file pattern (critique #14) which leaked an empty
# sentinel file every time the file backend ran. The file backend is
# intentionally single-process (local dev only — deployed Container
# Apps always uses the Table backend), so a plain in-process lock is
# enough to serialise read-modify-write on the JSON state file.
_FILE_BACKEND_LOCKS: dict[str, threading.Lock] = {}
_FILE_BACKEND_LOCKS_GUARD = threading.Lock()


def _file_backend_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _FILE_BACKEND_LOCKS_GUARD:
        lock = _FILE_BACKEND_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_BACKEND_LOCKS[key] = lock
    return lock


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
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _save_file(pref: AutoStopPreference) -> str:
    # Critique #14: the previous implementation opened a sibling
    # ``<state>.lock`` file with ``open("a")`` and ``fcntl.flock`` for
    # cross-process exclusion. That leaves an orphan ``auto_stop.json.lock``
    # file forever (no cleanup) AND the file backend is intentionally
    # single-process (dev/local-run only — the Container App always
    # uses the Table backend), so the cross-process flock was never
    # exercised in production. A plain ``threading.Lock`` keyed by the
    # state file path is enough: serialises concurrent writes from the
    # same process without spawning a ``.lock`` sentinel.
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _file_backend_lock(path)
    with lock:
        data = _read_file_state()
        if pref.etag:
            existing = data.get(pref.key)
            current_etag = _file_etag(existing) if isinstance(existing, dict) else ""
            if current_etag != pref.etag:
                raise PreferenceUpdateConflict(
                    f"auto_stop row {pref.key!r} changed since last read"
                )
        row = pref.to_dict()
        data[pref.key] = row
        _write_file_state(data)
    return _file_etag(row)


def _file_etag(row: dict[str, Any] | None) -> str:
    """Synthesise a deterministic ETag from a stored row payload.

    The file backend is single-process, so optimistic concurrency is
    technically unnecessary — but mirroring the Table backend's CAS
    contract means tests can exercise the conflict path identically.
    """
    if not isinstance(row, dict):
        return ""
    blob = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _get_file(key: str) -> AutoStopPreference | None:
    value = _read_file_state().get(key)
    if not isinstance(value, dict):
        return None
    pref = AutoStopPreference.from_dict(value)
    pref.etag = _file_etag(value)
    return pref


def _list_file(limit: int) -> list[AutoStopPreference]:
    prefs: list[AutoStopPreference] = []
    for value in _read_file_state().values():
        if isinstance(value, dict):
            prefs.append(AutoStopPreference.from_dict(value))
        if len(prefs) >= limit:
            break
    return prefs


__all__ = [
    "ALLOWED_IDLE_MINUTES",
    "DEFAULT_COOLDOWN_MINUTES",
    "DEFAULT_IDLE_MINUTES",
    "EXTEND_GRANT_MINUTES",
    "AutoStopPreference",
    "_reset_autostop_table_pool",
    "extend_auto_stop_preference",
    "get_auto_stop_preference",
    "is_extended",
    "is_in_cooldown",
    "list_auto_stop_preferences",
    "mark_auto_stop_event",
    "normalise_preference",
    "preference_key",
    "save_auto_stop_preference",
]

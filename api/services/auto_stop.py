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

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential

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
    owner_oid: str = ""
    tenant_id: str = ""

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
    if _use_table_backend():
        _save_table(pref)
    else:
        _save_file(pref)
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

    Lost-update guard mirrors ``mark_auto_stop_event``: re-fetch the
    latest persisted row so a concurrent beat tick's
    ``mark_auto_stop_event`` write (or a sibling SPA tab toggling the
    preference) is not silently rolled back. We only mutate
    ``extend_until`` + ``updated_at``; every other field is taken from
    the freshly-read row.
    """
    grant = max(1, min(int(minutes or EXTEND_GRANT_MINUTES), 24 * 60))
    latest = get_auto_stop_preference(
        pref.subscription_id, pref.resource_group, pref.cluster_name
    )
    base = latest if latest is not None else pref
    next_pref = AutoStopPreference.from_dict(base.to_dict())
    next_pref.extend_until = (
        datetime.now(UTC) + timedelta(minutes=grant)
    ).isoformat(timespec="seconds")
    next_pref.updated_at = _now_iso()
    return save_auto_stop_preference(next_pref)


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

    LOST-UPDATE GUARD: this helper re-fetches the latest persisted row
    BEFORE writing so a concurrent ``PUT /api/aks/autostop`` (e.g. the
    user toggled ``enabled=False`` between beat-decide and beat-write)
    does not get its toggle silently reverted by the in-memory ``pref``
    snapshot the beat task is holding. Only the bookkeeping fields
    (``last_stop_*`` / ``last_skip_*`` / ``updated_at``) are written
    from this path — user-owned fields (``enabled``, ``idle_minutes``,
    ``cooldown_minutes``, ``extend_until``, ``owner_oid``,
    ``tenant_id``) are taken from the freshly-read row. When the row no
    longer exists (user deleted the pref), we silently no-op and return
    the in-memory snapshot — there is nothing to update.
    """
    latest = get_auto_stop_preference(
        pref.subscription_id, pref.resource_group, pref.cluster_name
    )
    base = latest if latest is not None else pref
    next_pref = AutoStopPreference.from_dict(base.to_dict())
    now = _now_iso()
    if stopped:
        next_pref.last_stop_at = now
        next_pref.last_stop_reason = reason[:200]
    else:
        next_pref.last_skip_at = now
        next_pref.last_skip_reason = reason[:200]
    next_pref.updated_at = now
    if latest is None:
        # Row vanished mid-tick; do not resurrect a stale preference.
        return next_pref
    return save_auto_stop_preference(next_pref)


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


def _save_table(pref: AutoStopPreference) -> None:
    _ensure_table()
    with _table_client() as table:
        table.upsert_entity(_entity_from_pref(pref), mode=UpdateMode.REPLACE)


def _get_table(key: str) -> AutoStopPreference | None:
    from azure.core.exceptions import ResourceNotFoundError

    _ensure_table()
    with _table_client() as table:
        try:
            entity = dict(table.get_entity(partition_key=key, row_key="current"))
        except ResourceNotFoundError:
            return None
    return _pref_from_entity(entity)


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


def _save_file(pref: AutoStopPreference) -> None:
    lock_path = _state_file().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        data = _read_file_state()
        data[pref.key] = pref.to_dict()
        _write_file_state(data)
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass


def _get_file(key: str) -> AutoStopPreference | None:
    value = _read_file_state().get(key)
    if not isinstance(value, dict):
        return None
    return AutoStopPreference.from_dict(value)


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

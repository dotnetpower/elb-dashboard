"""Server-side Auto warm preferences for AKS warm cache reconciliation.

Responsibility: Server-side Auto warm preferences for AKS warm cache reconciliation
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_now_iso`, `_clean_db_names`, `_clean_programs`, `AutoWarmupPreference`,
`preference_key`, `normalise_preference`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
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
from api.services.preference_concurrency import (
    PreferenceUpdateConflict,
    cas_retry,
)

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "autowarmup"
_TYPE = "auto_warmup"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"
_ENSURED_TABLES: set[str] = set()
_ENSURED_TABLES_LOCK = threading.Lock()
_AUTOWARMUP_TABLE_POOLED: TableClient | None = None
_AUTOWARMUP_TABLE_POOL_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean_db_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    names: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        name = value.rsplit("/", 1)[-1].strip()
        if name:
            names.add(name)
    return sorted(names)


def _clean_programs(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, program in value.items():
        if not isinstance(key, str) or not isinstance(program, str):
            continue
        db_name = key.rsplit("/", 1)[-1].strip()
        if db_name and program in {"blastn", "blastp", "blastx", "tblastn", "tblastx"}:
            out[db_name] = program
    return out


@dataclass
class AutoWarmupPreference:
    subscription_id: str
    resource_group: str
    cluster_name: str
    storage_account: str
    storage_resource_group: str
    region: str = ""
    databases: list[str] = field(default_factory=list)
    programs: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    acr_resource_group: str = ""
    acr_name: str = ""
    terminal_resource_group: str = ""
    terminal_vm_name: str = ""
    machine_type: str = ""
    num_nodes: int = 0
    last_ready: bool = False
    last_triggered_at: str = ""
    updated_at: str = ""
    owner_oid: str = ""
    tenant_id: str = ""
    # Optimistic-concurrency token populated by ``_get_*`` reads. See the
    # matching note in ``api.services.auto_stop`` — NEVER persisted in
    # ``payload_json`` (excluded from ``to_dict``), carried in entity
    # metadata on the Table backend and synthesised from a content hash
    # on the file backend.
    etag: str = field(default="", compare=False, repr=False)

    @property
    def key(self) -> str:
        return preference_key(self.subscription_id, self.resource_group, self.cluster_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "resource_group": self.resource_group,
            "cluster_name": self.cluster_name,
            "storage_account": self.storage_account,
            "storage_resource_group": self.storage_resource_group,
            "region": self.region,
            "databases": list(self.databases),
            "programs": dict(self.programs),
            "enabled": self.enabled,
            "acr_resource_group": self.acr_resource_group,
            "acr_name": self.acr_name,
            "terminal_resource_group": self.terminal_resource_group,
            "terminal_vm_name": self.terminal_vm_name,
            "machine_type": self.machine_type,
            "num_nodes": self.num_nodes,
            "last_ready": self.last_ready,
            "last_triggered_at": self.last_triggered_at,
            "updated_at": self.updated_at,
            "owner_oid": self.owner_oid,
            "tenant_id": self.tenant_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AutoWarmupPreference:
        try:
            num_nodes = int(value.get("num_nodes") or 0)
        except (TypeError, ValueError):
            num_nodes = 0
        return cls(
            subscription_id=str(value.get("subscription_id") or ""),
            resource_group=str(value.get("resource_group") or ""),
            cluster_name=str(value.get("cluster_name") or ""),
            storage_account=str(value.get("storage_account") or ""),
            storage_resource_group=str(
                value.get("storage_resource_group") or value.get("resource_group") or ""
            ),
            region=str(value.get("region") or ""),
            databases=_clean_db_names(value.get("databases")),
            programs=_clean_programs(value.get("programs")),
            enabled=bool(value.get("enabled", True)),
            acr_resource_group=str(value.get("acr_resource_group") or ""),
            acr_name=str(value.get("acr_name") or ""),
            terminal_resource_group=str(value.get("terminal_resource_group") or ""),
            terminal_vm_name=str(value.get("terminal_vm_name") or ""),
            machine_type=str(value.get("machine_type") or ""),
            num_nodes=num_nodes,
            last_ready=bool(value.get("last_ready", False)),
            last_triggered_at=str(value.get("last_triggered_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            owner_oid=str(value.get("owner_oid") or ""),
            tenant_id=str(value.get("tenant_id") or ""),
        )


def preference_key(subscription_id: str, resource_group: str, cluster_name: str) -> str:
    raw = f"{subscription_id}:{resource_group}:{cluster_name}"
    return "auto_warmup:" + re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)


def normalise_preference(value: dict[str, Any]) -> AutoWarmupPreference:
    pref = AutoWarmupPreference.from_dict(value)
    if not pref.subscription_id:
        raise ValueError("subscription_id is required")
    if not pref.resource_group:
        raise ValueError("resource_group is required")
    if not pref.cluster_name:
        raise ValueError("cluster_name is required")
    if pref.enabled and not pref.storage_account:
        raise ValueError("storage_account is required when Auto warm is enabled")
    pref.storage_resource_group = pref.storage_resource_group or pref.resource_group
    pref.updated_at = _now_iso()
    return pref


def _use_table_backend() -> bool:
    """Return True when the Azure Tables backend should be used.

    Requires both ``AZURE_TABLE_ENDPOINT`` *and* ``CONTAINER_APP_NAME``.
    The Container App guard is the local-dev escape hatch: a workstation
    `az login` identity often lacks Storage Table RBAC on the platform
    account, so every preference read would 403 and crash the
    `reconcile_auto_warmup` Celery tick. When not in a deployed Container
    App we silently fall back to the file backend so the worker stays
    healthy. Production (CONTAINER_APP_NAME set by ACA) keeps using
    Azure Tables.
    """
    return bool(
        os.environ.get(_TABLE_ENDPOINT_ENV) and os.environ.get("CONTAINER_APP_NAME")
    )


def save_auto_warmup_preference(pref: AutoWarmupPreference) -> AutoWarmupPreference:
    """Persist the preference. Mode depends on ``pref.etag``:

    * Empty ``pref.etag`` — unconditional upsert (legacy first-write
      semantics; the route handler that accepts a user PUT does this
      so the user always wins over a missing-row state).
    * Non-empty ``pref.etag`` — conditional update (Azure Tables
      ``If-Match``; raises :class:`PreferenceUpdateConflict` when the
      stored ETag has moved on). Background bookkeeping writers
      (``mark_auto_warmup_ready_state``) set this from a fresh read so
      a sibling write cannot be silently clobbered.
    """
    if _use_table_backend():
        new_etag = _save_table(pref)
    else:
        new_etag = _save_file(pref)
    pref.etag = new_etag
    return pref


def get_auto_warmup_preference(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> AutoWarmupPreference | None:
    key = preference_key(subscription_id, resource_group, cluster_name)
    if _use_table_backend():
        return _get_table(key)
    return _get_file(key)


def list_auto_warmup_preferences(limit: int = 100) -> list[AutoWarmupPreference]:
    if _use_table_backend():
        return _list_table(limit)
    return _list_file(limit)


def mark_auto_warmup_ready_state(
    pref: AutoWarmupPreference,
    *,
    ready: bool,
    triggered: bool = False,
) -> AutoWarmupPreference:
    """Update the warm-readiness bookkeeping with optimistic concurrency.

    Re-reads the latest persisted row (with its ETag) before writing
    and uses an Azure Tables ``If-Match`` conditional update so a
    concurrent ``PUT /api/aks/autowarmup`` (e.g. the user toggled
    ``enabled=False`` or changed the database list between the
    reconciler's decide and write) cannot be silently reverted by the
    in-memory ``pref`` snapshot the reconciler is holding. Only the
    bookkeeping fields (``last_ready`` / ``last_triggered_at`` /
    ``updated_at``) are written from this path — every user-owned
    field is taken from the freshly-read row. When the row no longer
    exists (user deleted the pref), we silently no-op and return the
    in-memory snapshot — there is nothing to update.

    On an ETag conflict ``cas_retry`` refreshes the snapshot and
    retries (bounded by
    :data:`api.services.preference_concurrency.DEFAULT_CAS_MAX_ATTEMPTS`).
    On exhaustion we log a warning and return the in-memory
    next-state without persisting it — a bookkeeping miss is preferred
    over clobbering whatever the sibling writer just persisted.
    """
    fallback: AutoWarmupPreference | None = None

    def _attempt() -> AutoWarmupPreference:
        nonlocal fallback
        latest = get_auto_warmup_preference(
            pref.subscription_id, pref.resource_group, pref.cluster_name
        )
        base = latest if latest is not None else pref
        next_pref = AutoWarmupPreference.from_dict(base.to_dict())
        next_pref.etag = base.etag
        next_pref.last_ready = ready
        if triggered:
            next_pref.last_triggered_at = _now_iso()
        next_pref.updated_at = _now_iso()
        fallback = next_pref
        if latest is None:
            # Row vanished mid-tick; do not resurrect a stale preference.
            return next_pref
        return save_auto_warmup_preference(next_pref)

    try:
        return cas_retry(_attempt, operation="auto_warmup.mark_ready")
    except PreferenceUpdateConflict:
        LOGGER.warning(
            "auto_warmup.mark_auto_warmup_ready_state giving up after CAS "
            "exhaustion; in-memory snapshot returned without persisting",
        )
        return fallback if fallback is not None else pref


def _entity_from_pref(pref: AutoWarmupPreference) -> dict[str, Any]:
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


def _pref_from_entity(entity: dict[str, Any]) -> AutoWarmupPreference | None:
    try:
        payload = json.loads(str(entity.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        return None
    return AutoWarmupPreference.from_dict(payload)


def _table_client() -> TableClient:
    """Return a process-shared pooled ``TableClient`` for the autowarmup table."""
    global _AUTOWARMUP_TABLE_POOLED
    pool = _AUTOWARMUP_TABLE_POOLED
    if pool is not None:
        return pool  # type: ignore[return-value]
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _AUTOWARMUP_TABLE_POOL_LOCK:
        if _AUTOWARMUP_TABLE_POOLED is None:
            from api.services.state_repo import _PooledTableClient

            _AUTOWARMUP_TABLE_POOLED = _PooledTableClient(  # type: ignore[assignment]
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _AUTOWARMUP_TABLE_POOLED  # type: ignore[return-value]


def _reset_autowarmup_table_pool() -> None:
    """Test hook + safety valve for credential reset."""
    global _AUTOWARMUP_TABLE_POOLED
    with _AUTOWARMUP_TABLE_POOL_LOCK:
        pool = _AUTOWARMUP_TABLE_POOLED
        _AUTOWARMUP_TABLE_POOLED = None
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


def _save_table(pref: AutoWarmupPreference) -> str:
    """Persist ``pref`` and return the new ETag.

    See ``api.services.auto_stop._save_table`` for the conditional
    update contract — this helper is identical other than the table
    name.
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
                    f"auto_warmup row {pref.key!r} changed since last read"
                ) from exc
            except ResourceNotFoundError:
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


def _get_table(key: str) -> AutoWarmupPreference | None:
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


def _list_table(limit: int) -> list[AutoWarmupPreference]:
    prefs: list[AutoWarmupPreference] = []
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
    return root / "auto_warmup.json"


# Per-state-file ``threading.Lock`` registry. See the matching note in
# ``api.services.auto_stop`` (critique #14): replaces the sibling
# ``.lock`` file pattern which leaked an empty sentinel file every time
# the file backend ran. The file backend is dev-only (Container Apps
# always use the Table backend) so an in-process lock is enough.
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


def _save_file(pref: AutoWarmupPreference) -> str:
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
                    f"auto_warmup row {pref.key!r} changed since last read"
                )
        row = pref.to_dict()
        data[pref.key] = row
        _write_file_state(data)
    return _file_etag(row)


def _file_etag(row: dict[str, Any] | None) -> str:
    """Mirror ``api.services.auto_stop._file_etag`` — deterministic
    content hash so the file backend's CAS contract matches the Table
    backend in tests."""
    if not isinstance(row, dict):
        return ""
    blob = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _get_file(key: str) -> AutoWarmupPreference | None:
    value = _read_file_state().get(key)
    if not isinstance(value, dict):
        return None
    pref = AutoWarmupPreference.from_dict(value)
    pref.etag = _file_etag(value)
    return pref


def _list_file(limit: int) -> list[AutoWarmupPreference]:
    prefs: list[AutoWarmupPreference] = []
    for value in _read_file_state().values():
        if isinstance(value, dict):
            prefs.append(AutoWarmupPreference.from_dict(value))
        if len(prefs) >= limit:
            break
    return prefs

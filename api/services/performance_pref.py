"""Per-cluster Performance preferences (warm-cache persistence mode).

Responsibility: Persist and read the per-cluster ``warm_cache_mode`` choice that
    governs how the AKS warm cache survives an ``az aks stop``/``start`` cycle
    (``ephemeral`` redownloads every start; ``node_disk`` / ``data_disk`` persist
    the staged DB so a restart only re-touches RAM).
Edit boundaries: Reusable domain/persistence logic only. HTTP shaping lives in
    ``api.routes.settings.performance``; cluster provisioning consumes the mode in
    ``api.tasks.azure.cluster_params``. No Azure SDK management calls here.
Key entry points: ``WARM_CACHE_MODES``, ``PerformancePreference``,
    ``normalise_preference``, ``get_performance_preference``,
    ``save_performance_preference``, ``list_performance_preferences``.
Risky contracts: ``warm_cache_mode`` is a closed enum — adding a value requires
    updating both the route model and ``cluster_params``. The default mode
    ``ephemeral`` MUST keep the historical behaviour (no provisioning change), so
    a missing row reads back as ``ephemeral``. Table backend is gated by
    ``AZURE_TABLE_ENDPOINT`` + ``CONTAINER_APP_NAME`` (mirrors ``auto_warmup``);
    local dev falls back to a JSON file so a workstation identity without Table
    RBAC does not 403.
Validation: ``uv run pytest -q api/tests/test_performance_pref.py``.
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

# Closed enum of warm-cache persistence modes. Keep in lock-step with the
# route model and `cluster_params.build_cluster_params`.
WARM_CACHE_MODE_EPHEMERAL = "ephemeral"
WARM_CACHE_MODE_NODE_DISK = "node_disk"
WARM_CACHE_MODE_DATA_DISK = "data_disk"
WARM_CACHE_MODES: tuple[str, ...] = (
    WARM_CACHE_MODE_EPHEMERAL,
    WARM_CACHE_MODE_NODE_DISK,
    WARM_CACHE_MODE_DATA_DISK,
)
DEFAULT_WARM_CACHE_MODE = WARM_CACHE_MODE_EPHEMERAL

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "performancepref"
_TYPE = "performance_pref"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"
_ENSURED_TABLES: set[str] = set()
_ENSURED_TABLES_LOCK = threading.Lock()
_PERF_TABLE_POOLED: TableClient | None = None
_PERF_TABLE_POOL_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean_mode(value: Any) -> str:
    mode = str(value or "").strip()
    if mode in WARM_CACHE_MODES:
        return mode
    return DEFAULT_WARM_CACHE_MODE


@dataclass
class PerformancePreference:
    subscription_id: str
    resource_group: str
    cluster_name: str
    warm_cache_mode: str = DEFAULT_WARM_CACHE_MODE
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
            "warm_cache_mode": self.warm_cache_mode,
            "updated_at": self.updated_at,
            "owner_oid": self.owner_oid,
            "tenant_id": self.tenant_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PerformancePreference:
        return cls(
            subscription_id=str(value.get("subscription_id") or ""),
            resource_group=str(value.get("resource_group") or ""),
            cluster_name=str(value.get("cluster_name") or ""),
            warm_cache_mode=_clean_mode(value.get("warm_cache_mode")),
            updated_at=str(value.get("updated_at") or ""),
            owner_oid=str(value.get("owner_oid") or ""),
            tenant_id=str(value.get("tenant_id") or ""),
        )


def preference_key(subscription_id: str, resource_group: str, cluster_name: str) -> str:
    raw = f"{subscription_id}:{resource_group}:{cluster_name}"
    return "performance_pref:" + re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)


def normalise_preference(value: dict[str, Any]) -> PerformancePreference:
    pref = PerformancePreference.from_dict(value)
    if not pref.subscription_id:
        raise ValueError("subscription_id is required")
    if not pref.resource_group:
        raise ValueError("resource_group is required")
    if not pref.cluster_name:
        raise ValueError("cluster_name is required")
    pref.updated_at = _now_iso()
    return pref


def resolve_warm_cache_mode(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> str:
    """Return the effective warm-cache mode (default ``ephemeral`` when unset).

    Provisioning callers use this so a missing preference row keeps the
    historical behaviour without special-casing ``None`` at every call site.
    """
    pref = get_performance_preference(subscription_id, resource_group, cluster_name)
    if pref is None:
        return DEFAULT_WARM_CACHE_MODE
    return pref.warm_cache_mode


def _use_table_backend() -> bool:
    """Mirror ``auto_warmup._use_table_backend``: Table only inside a deployed
    Container App (where the shared MI has Table RBAC); file backend locally."""
    return bool(os.environ.get(_TABLE_ENDPOINT_ENV) and os.environ.get("CONTAINER_APP_NAME"))


def save_performance_preference(pref: PerformancePreference) -> PerformancePreference:
    """Unconditional upsert. There is no background writer for this row (only the
    user PUT), so the optimistic-concurrency dance used by ``auto_warmup`` is not
    needed here."""
    if _use_table_backend():
        _save_table(pref)
    else:
        _save_file(pref)
    return pref


def get_performance_preference(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> PerformancePreference | None:
    key = preference_key(subscription_id, resource_group, cluster_name)
    if _use_table_backend():
        return _get_table(key)
    return _get_file(key)


def list_performance_preferences(limit: int = 100) -> list[PerformancePreference]:
    if _use_table_backend():
        return _list_table(limit)
    return _list_file(limit)


def _entity_from_pref(pref: PerformancePreference) -> dict[str, Any]:
    return {
        "PartitionKey": pref.key,
        "RowKey": "current",
        "type": _TYPE,
        "warm_cache_mode": pref.warm_cache_mode,
        "updated_at": pref.updated_at or _now_iso(),
        "owner_oid": pref.owner_oid,
        "tenant_id": pref.tenant_id,
        "payload_json": json.dumps(pref.to_dict(), default=str),
    }


def _pref_from_entity(entity: dict[str, Any]) -> PerformancePreference | None:
    try:
        payload = json.loads(str(entity.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        return None
    return PerformancePreference.from_dict(payload)


def _table_client() -> TableClient:
    global _PERF_TABLE_POOLED
    pool = _PERF_TABLE_POOLED
    if pool is not None:
        return pool  # type: ignore[return-value]
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _PERF_TABLE_POOL_LOCK:
        if _PERF_TABLE_POOLED is None:
            from api.services.state_repo import _PooledTableClient

            _PERF_TABLE_POOLED = _PooledTableClient(  # type: ignore[assignment]
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _PERF_TABLE_POOLED  # type: ignore[return-value]


def _reset_performance_table_pool() -> None:
    """Test hook + safety valve for credential reset."""
    global _PERF_TABLE_POOLED
    with _PERF_TABLE_POOL_LOCK:
        pool = _PERF_TABLE_POOLED
        _PERF_TABLE_POOLED = None
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


def _save_table(pref: PerformancePreference) -> None:
    _ensure_table()
    entity = _entity_from_pref(pref)
    with _table_client() as table:
        table.upsert_entity(entity, mode=UpdateMode.REPLACE)


def _get_table(key: str) -> PerformancePreference | None:
    _ensure_table()
    with _table_client() as table:
        try:
            entity = table.get_entity(partition_key=key, row_key="current")
        except ResourceNotFoundError:
            return None
        entity_dict = dict(entity)
    return _pref_from_entity(entity_dict)


def _list_table(limit: int) -> list[PerformancePreference]:
    prefs: list[PerformancePreference] = []
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
    return root / "performance_pref.json"


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


def _save_file(pref: PerformancePreference) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _file_backend_lock(path)
    with lock:
        data = _read_file_state()
        data[pref.key] = pref.to_dict()
        _write_file_state(data)


def _get_file(key: str) -> PerformancePreference | None:
    data = _read_file_state()
    row = data.get(key)
    if not isinstance(row, dict):
        return None
    return PerformancePreference.from_dict(row)


def _list_file(limit: int) -> list[PerformancePreference]:
    data = _read_file_state()
    prefs: list[PerformancePreference] = []
    for row in data.values():
        if not isinstance(row, dict):
            continue
        prefs.append(PerformancePreference.from_dict(row))
        if len(prefs) >= limit:
            break
    return prefs

"""Server-side Auto warm preferences for AKS warm cache reconciliation."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "autowarmup"
_TYPE = "auto_warmup"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"
_ENSURED_TABLES: set[str] = set()


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


def save_auto_warmup_preference(pref: AutoWarmupPreference) -> AutoWarmupPreference:
    if os.environ.get(_TABLE_ENDPOINT_ENV):
        _save_table(pref)
    else:
        _save_file(pref)
    return pref


def get_auto_warmup_preference(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> AutoWarmupPreference | None:
    key = preference_key(subscription_id, resource_group, cluster_name)
    if os.environ.get(_TABLE_ENDPOINT_ENV):
        return _get_table(key)
    return _get_file(key)


def list_auto_warmup_preferences(limit: int = 100) -> list[AutoWarmupPreference]:
    if os.environ.get(_TABLE_ENDPOINT_ENV):
        return _list_table(limit)
    return _list_file(limit)


def mark_auto_warmup_ready_state(
    pref: AutoWarmupPreference,
    *,
    ready: bool,
    triggered: bool = False,
) -> AutoWarmupPreference:
    next_pref = AutoWarmupPreference.from_dict(pref.to_dict())
    next_pref.last_ready = ready
    if triggered:
        next_pref.last_triggered_at = _now_iso()
    next_pref.updated_at = _now_iso()
    return save_auto_warmup_preference(next_pref)


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
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    return TableClient(endpoint=endpoint, table_name=_TABLE_NAME, credential=get_credential())


def _ensure_table() -> None:
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
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


def _save_table(pref: AutoWarmupPreference) -> None:
    _ensure_table()
    with _table_client() as table:
        table.upsert_entity(_entity_from_pref(pref), mode=UpdateMode.REPLACE)


def _get_table(key: str) -> AutoWarmupPreference | None:
    from azure.core.exceptions import ResourceNotFoundError

    _ensure_table()
    with _table_client() as table:
        try:
            entity = dict(table.get_entity(partition_key=key, row_key="current"))
        except ResourceNotFoundError:
            return None
    return _pref_from_entity(entity)


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


def _read_file_state() -> dict[str, Any]:
    path = _state_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_file_state(data: dict[str, Any]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _save_file(pref: AutoWarmupPreference) -> None:
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


def _get_file(key: str) -> AutoWarmupPreference | None:
    value = _read_file_state().get(key)
    if not isinstance(value, dict):
        return None
    return AutoWarmupPreference.from_dict(value)


def _list_file(limit: int) -> list[AutoWarmupPreference]:
    prefs: list[AutoWarmupPreference] = []
    for value in _read_file_state().values():
        if isinstance(value, dict):
            prefs.append(AutoWarmupPreference.from_dict(value))
        if len(prefs) >= limit:
            break
    return prefs

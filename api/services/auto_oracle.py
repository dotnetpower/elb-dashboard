"""Per-database Auto oracle preferences.

Responsibility: Normalize, version, persist, and page shared automatic
    order-oracle settings and named scan cursors using Azure Table in Container
    Apps and an atomic local JSON fallback during development.
Edit boundaries: Preference data and persistence only; HTTP authorization,
    readiness reconciliation, build dispatch, retry state, and progress live in
    their owning modules.
Key entry points: `AutoOraclePreference`, `normalise_auto_oracle_preference`,
    `save_auto_oracle_preference`, `get_auto_oracle_preference`,
    `list_auto_oracle_preference_page`, `get_auto_oracle_scan_cursor`.
Risky contracts: Missing rows mean disabled; background reconcilers never write
    preference rows; enforced updates use create-only/If-Match; scope columns
    remain indexed; named cursors are independent; local file writes are atomic.
Validation: `uv run pytest -q api/tests/test_auto_oracle_preference.py`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
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
from api.services.preference_concurrency import PreferenceUpdateConflict

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "autooracle"
_TYPE = "auto_oracle"
_CURSOR_TYPE = "auto_oracle_cursor"
_CURSOR_FILE_PREFIX = "__auto_oracle_cursor__:"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.()-]{1,120}$")
_SAFE_STORAGE_RE = re.compile(r"^[a-z0-9]{3,24}$")
_SAFE_ACR_RE = re.compile(r"^[A-Za-z0-9]{5,50}$")
_SAFE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_SAFE_DB_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_SUB_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_ENSURED_ENDPOINTS: set[str] = set()
_ENSURE_LOCK = threading.Lock()
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class AutoOraclePreference:
    subscription_id: str
    cluster_resource_group: str
    cluster_name: str
    storage_resource_group: str
    storage_account: str
    db_name: str
    acr_name: str = ""
    image: str = ""
    enabled: bool = False
    owner_oid: str = ""
    tenant_id: str = ""
    updated_at: str = ""
    etag: str = field(default="", compare=False, repr=False)

    @property
    def key(self) -> str:
        return auto_oracle_preference_key(
            self.subscription_id,
            self.cluster_resource_group,
            self.cluster_name,
            self.storage_account,
            self.db_name,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "cluster_resource_group": self.cluster_resource_group,
            "cluster_name": self.cluster_name,
            "storage_resource_group": self.storage_resource_group,
            "storage_account": self.storage_account,
            "db_name": self.db_name,
            "acr_name": self.acr_name,
            "image": self.image,
            "enabled": self.enabled,
            "owner_oid": self.owner_oid,
            "tenant_id": self.tenant_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AutoOraclePreference:
        return cls(
            subscription_id=str(value.get("subscription_id") or ""),
            cluster_resource_group=str(value.get("cluster_resource_group") or ""),
            cluster_name=str(value.get("cluster_name") or ""),
            storage_resource_group=str(value.get("storage_resource_group") or ""),
            storage_account=str(value.get("storage_account") or ""),
            db_name=str(value.get("db_name") or ""),
            acr_name=str(value.get("acr_name") or ""),
            image=str(value.get("image") or ""),
            enabled=bool(value.get("enabled", False)),
            owner_oid=str(value.get("owner_oid") or ""),
            tenant_id=str(value.get("tenant_id") or ""),
            updated_at=str(value.get("updated_at") or ""),
        )


def auto_oracle_preference_key(
    subscription_id: str,
    cluster_resource_group: str,
    cluster_name: str,
    storage_account: str,
    db_name: str,
) -> str:
    raw = ":".join(
        (
            subscription_id,
            cluster_resource_group,
            cluster_name,
            storage_account,
            db_name,
        )
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"auto_oracle:{digest}"


def normalise_auto_oracle_preference(
    value: dict[str, Any],
) -> AutoOraclePreference:
    pref = AutoOraclePreference.from_dict(value)
    if not _SAFE_SUB_RE.fullmatch(pref.subscription_id):
        raise ValueError("valid subscription_id is required")
    for label, segment in (
        ("cluster_resource_group", pref.cluster_resource_group),
        ("cluster_name", pref.cluster_name),
        ("storage_resource_group", pref.storage_resource_group),
    ):
        if not _SAFE_SEGMENT_RE.fullmatch(segment):
            raise ValueError(f"valid {label} is required")
    if not _SAFE_STORAGE_RE.fullmatch(pref.storage_account):
        raise ValueError("valid storage_account is required")
    if not _SAFE_DB_RE.fullmatch(pref.db_name):
        raise ValueError("valid db_name is required")
    if pref.acr_name and not _SAFE_ACR_RE.fullmatch(pref.acr_name):
        raise ValueError("valid acr_name is required")
    if pref.image and not _SAFE_IMAGE_RE.fullmatch(pref.image):
        raise ValueError("valid image reference is required")
    if pref.enabled and not (pref.image or pref.acr_name):
        raise ValueError("acr_name or image is required when Auto oracle is enabled")
    version = str(value.get("version") or value.get("etag") or "")
    if len(version) > 1024:
        raise ValueError("preference version is too large")
    return AutoOraclePreference(
        **{**pref.to_dict(), "updated_at": _now_iso()},
        etag=version,
    )


def _use_table_backend() -> bool:
    return bool(os.environ.get(_TABLE_ENDPOINT_ENV) and os.environ.get("CONTAINER_APP_NAME"))


def save_auto_oracle_preference(
    pref: AutoOraclePreference,
    *,
    create_only: bool = False,
) -> AutoOraclePreference:
    if _use_table_backend():
        etag = _save_table(pref, create_only=create_only)
    else:
        etag = _save_file(pref, create_only=create_only)
    return replace(pref, etag=etag)


def get_auto_oracle_preference(
    subscription_id: str,
    cluster_resource_group: str,
    cluster_name: str,
    storage_account: str,
    db_name: str,
) -> AutoOraclePreference | None:
    key = auto_oracle_preference_key(
        subscription_id,
        cluster_resource_group,
        cluster_name,
        storage_account,
        db_name,
    )
    return _get_table(key) if _use_table_backend() else _get_file(key)


def list_auto_oracle_preferences(limit: int = 500) -> list[AutoOraclePreference]:
    bounded = max(1, min(int(limit or 500), 1000))
    return _list_table(bounded) if _use_table_backend() else _list_file(bounded)


def list_auto_oracle_preference_page(
    *,
    limit: int = 50,
    continuation_token: str = "",
    enabled_only: bool = False,
    subscription_id: str = "",
    cluster_resource_group: str = "",
    cluster_name: str = "",
    storage_account: str = "",
) -> tuple[list[AutoOraclePreference], str]:
    """Return one bounded page and an opaque continuation token."""
    bounded = max(1, min(int(limit or 50), 500))
    if len(continuation_token) > 8192:
        raise ValueError("continuation token is too large")
    filters = {
        "subscription_id": subscription_id,
        "cluster_resource_group": cluster_resource_group,
        "cluster_name": cluster_name,
        "storage_account": storage_account,
    }
    if _use_table_backend():
        return _list_table_page(
            bounded,
            continuation_token=continuation_token,
            enabled_only=enabled_only,
            filters=filters,
        )
    return _list_file_page(
        bounded,
        continuation_token=continuation_token,
        enabled_only=enabled_only,
        filters=filters,
    )


def get_auto_oracle_scan_cursor(name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        raise ValueError("invalid Auto oracle cursor name")
    if _use_table_backend():
        _ensure_table()
        with _table_client() as table:
            try:
                row = table.get_entity(
                    partition_key=f"auto_oracle_cursor:{name}",
                    row_key="current",
                )
            except ResourceNotFoundError:
                return ""
        return str(row.get("continuation_token") or "")
    value = _read_file().get(f"{_CURSOR_FILE_PREFIX}{name}")
    return str(value or "") if isinstance(value, str) else ""


def save_auto_oracle_scan_cursor(name: str, continuation_token: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        raise ValueError("invalid Auto oracle cursor name")
    if len(continuation_token) > 8192:
        raise ValueError("continuation token is too large")
    if _use_table_backend():
        _ensure_table()
        with _table_client() as table:
            table.upsert_entity(
                {
                    "PartitionKey": f"auto_oracle_cursor:{name}",
                    "RowKey": "current",
                    "type": _CURSOR_TYPE,
                    "continuation_token": continuation_token,
                    "updated_at": _now_iso(),
                },
                mode=UpdateMode.REPLACE,
            )
        return
    path = _state_file()
    with _file_lock(path):
        data = _read_file()
        data[f"{_CURSOR_FILE_PREFIX}{name}"] = continuation_token
        _write_file(data)


def _entity(pref: AutoOraclePreference) -> dict[str, Any]:
    return {
        "PartitionKey": pref.key,
        "RowKey": "current",
        "type": _TYPE,
        "status": "enabled" if pref.enabled else "disabled",
        "subscription_id": pref.subscription_id,
        "cluster_resource_group": pref.cluster_resource_group,
        "cluster_name": pref.cluster_name,
        "storage_account": pref.storage_account,
        "db_name": pref.db_name,
        "owner_oid": pref.owner_oid,
        "tenant_id": pref.tenant_id,
        "updated_at": pref.updated_at,
        "payload_json": json.dumps(pref.to_dict(), sort_keys=True),
    }


def _from_entity(entity: dict[str, Any]) -> AutoOraclePreference | None:
    try:
        payload = json.loads(str(entity.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        return None
    return AutoOraclePreference.from_dict(payload) if isinstance(payload, dict) else None


def _ensure_table() -> None:
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    if endpoint in _ENSURED_ENDPOINTS:
        return
    with _ENSURE_LOCK:
        if endpoint in _ENSURED_ENDPOINTS:
            return
        with TableServiceClient(endpoint=endpoint, credential=get_credential()) as service:
            try:
                service.create_table_if_not_exists(_TABLE_NAME)
            except AttributeError:
                try:
                    service.create_table(_TABLE_NAME)
                except ResourceExistsError:
                    pass
        _ENSURED_ENDPOINTS.add(endpoint)


def _table_client() -> TableClient:
    return TableClient(
        endpoint=os.environ[_TABLE_ENDPOINT_ENV],
        table_name=_TABLE_NAME,
        credential=get_credential(),
    )


def _extract_etag(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("etag") or value.get("odata.etag") or "")
    return str(getattr(value, "etag", "") or "")


def _save_table(
    pref: AutoOraclePreference,
    *,
    create_only: bool,
) -> str:
    _ensure_table()
    with _table_client() as table:
        try:
            if create_only:
                response = table.create_entity(_entity(pref))
            elif pref.etag:
                response = table.update_entity(
                    _entity(pref),
                    mode=UpdateMode.REPLACE,
                    etag=pref.etag,
                    match_condition=MatchConditions.IfNotModified,
                )
            else:
                response = table.upsert_entity(_entity(pref), mode=UpdateMode.REPLACE)
        except (ResourceExistsError, ResourceModifiedError) as exc:
            raise PreferenceUpdateConflict(
                f"auto_oracle row {pref.key!r} changed since last read"
            ) from exc
    return _extract_etag(response)


def _get_table(key: str) -> AutoOraclePreference | None:
    _ensure_table()
    with _table_client() as table:
        try:
            row = table.get_entity(partition_key=key, row_key="current")
        except ResourceNotFoundError:
            return None
    pref = _from_entity(dict(row))
    metadata = getattr(row, "metadata", None) or {}
    etag = str(metadata.get("etag") or dict(row).get("odata.etag") or "")
    return replace(pref, etag=etag) if pref is not None and etag else pref


def _list_table(limit: int) -> list[AutoOraclePreference]:
    _ensure_table()
    out: list[AutoOraclePreference] = []
    with _table_client() as table:
        rows = table.query_entities(f"type eq '{_TYPE}'", results_per_page=limit)
        for row in rows:
            pref = _from_entity(dict(row))
            if pref is not None:
                metadata = getattr(row, "metadata", None) or {}
                etag = str(metadata.get("etag") or dict(row).get("odata.etag") or "")
                out.append(replace(pref, etag=etag) if etag else pref)
            if len(out) >= limit:
                break
    return out


def _encode_continuation_token(value: Any) -> str:
    if value in (None, "", {}):
        return ""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_continuation_token(value: str) -> Any:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        return json.loads(base64.urlsafe_b64decode(value + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Auto oracle continuation token") from exc


def _preference_with_row_etag(
    row: Any,
) -> AutoOraclePreference | None:
    row_dict = dict(row)
    pref = _from_entity(row_dict)
    if pref is None:
        return None
    metadata = getattr(row, "metadata", None) or {}
    etag = str(metadata.get("etag") or row_dict.get("odata.etag") or "")
    return replace(pref, etag=etag) if etag else pref


def _list_table_page(
    limit: int,
    *,
    continuation_token: str,
    enabled_only: bool,
    filters: dict[str, str],
) -> tuple[list[AutoOraclePreference], str]:
    _ensure_table()
    clauses = [f"type eq '{_TYPE}'"]
    if enabled_only:
        clauses.append("status eq 'enabled'")
    for key, value in filters.items():
        if value:
            clauses.append(f"{key} eq '{value}'")
    decoded_token = _decode_continuation_token(continuation_token)
    if decoded_token is not None and (
        not isinstance(decoded_token, dict)
        or len(decoded_token) > 16
        or any(
            not isinstance(key, str) or not isinstance(value, str | int | float | bool | type(None))
            for key, value in decoded_token.items()
        )
    ):
        raise ValueError("invalid Auto oracle continuation token")
    with _table_client() as table:
        pages = table.query_entities(" and ".join(clauses), results_per_page=limit).by_page(
            continuation_token=decoded_token
        )
        try:
            page = next(pages)
        except StopIteration:
            return [], ""
        out = [pref for row in page if (pref := _preference_with_row_etag(row)) is not None]
        next_token = _encode_continuation_token(getattr(pages, "continuation_token", None))
    return out, next_token


def _state_file() -> Path:
    default_root = Path(__file__).resolve().parents[2] / ".logs" / "local" / "state"
    root = Path(os.environ.get(_LOCAL_STATE_ENV, str(default_root)))
    return root / "auto_oracle.json"


def _file_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[key] = lock
    return lock


def _read_file() -> dict[str, Any]:
    path = _state_file()
    if not path.exists():
        return {}
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_file(data: dict[str, Any]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _file_etag(value: dict[str, Any] | None) -> str:
    if not isinstance(value, dict):
        return ""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _save_file(
    pref: AutoOraclePreference,
    *,
    create_only: bool,
) -> str:
    path = _state_file()
    with _file_lock(path):
        data = _read_file()
        existing = data.get(pref.key)
        if create_only and isinstance(existing, dict):
            raise PreferenceUpdateConflict(f"auto_oracle row {pref.key!r} already exists")
        if pref.etag and _file_etag(existing if isinstance(existing, dict) else None) != pref.etag:
            raise PreferenceUpdateConflict(f"auto_oracle row {pref.key!r} changed since last read")
        row = pref.to_dict()
        data[pref.key] = row
        _write_file(data)
    return _file_etag(row)


def _get_file(key: str) -> AutoOraclePreference | None:
    value = _read_file().get(key)
    return (
        replace(AutoOraclePreference.from_dict(value), etag=_file_etag(value))
        if isinstance(value, dict)
        else None
    )


def _list_file(limit: int) -> list[AutoOraclePreference]:
    out: list[AutoOraclePreference] = []
    for value in _read_file().values():
        if isinstance(value, dict):
            out.append(
                replace(
                    AutoOraclePreference.from_dict(value),
                    etag=_file_etag(value),
                )
            )
        if len(out) >= limit:
            break
    return out


def _list_file_page(
    limit: int,
    *,
    continuation_token: str,
    enabled_only: bool,
    filters: dict[str, str],
) -> tuple[list[AutoOraclePreference], str]:
    decoded = _decode_continuation_token(continuation_token)
    if decoded is not None and (not isinstance(decoded, dict) or decoded.get("backend") != "file"):
        raise ValueError("invalid Auto oracle continuation token")
    after = str(decoded.get("after") or "") if isinstance(decoded, dict) else ""
    rows: list[tuple[str, AutoOraclePreference]] = []
    for key, value in _read_file().items():
        if not isinstance(value, dict) or key <= after:
            continue
        pref = replace(
            AutoOraclePreference.from_dict(value),
            etag=_file_etag(value),
        )
        if enabled_only and not pref.enabled:
            continue
        if any(
            expected and str(getattr(pref, field)) != expected
            for field, expected in filters.items()
        ):
            continue
        rows.append((key, pref))
    rows.sort(key=lambda item: item[0])
    selected = rows[:limit]
    next_token = (
        _encode_continuation_token({"backend": "file", "after": selected[-1][0]})
        if len(rows) > len(selected) and selected
        else ""
    )
    return [pref for _key, pref in selected], next_token


__all__ = [
    "AutoOraclePreference",
    "auto_oracle_preference_key",
    "get_auto_oracle_preference",
    "get_auto_oracle_scan_cursor",
    "list_auto_oracle_preference_page",
    "list_auto_oracle_preferences",
    "normalise_auto_oracle_preference",
    "save_auto_oracle_preference",
    "save_auto_oracle_scan_cursor",
]

"""Applied Application Insights connection string (single deployment-wide row).

Module summary: Durable persistence for the App Insights connection string an
operator applies to the server sidecars via Settings → Telemetry. Mirrors
``service_bus_pref`` so the same RBAC / backend gating applies: a single row
(PartitionKey ``appinsights_config`` / RowKey ``current``) in the
``appinsightspref`` Azure Table in Container Apps, and a JSON file locally.

Responsibility: Read / write / clear the applied connection string as the
    durable source of truth. The env var ``APPLICATIONINSIGHTS_CONNECTION_STRING``
    on the Container App revision is imperatively patched by the apply task and
    is wiped by any full ``azd provision`` (Bicep re-applies the param from the
    azd env, empty by default); this row survives that so telemetry self-heals.
Edit boundaries: Reusable persistence logic only. HTTP shaping lives in
    ``api.routes.settings.app_insights``; the ARM template patch lives in
    ``api.services.upgrade.aca_template``; the read-with-fallback wrapper lives
    in ``api.services.app_insights_provisioning.deployment_connection_string``.
    No Azure SDK management calls here.
Key entry points: ``get_persisted_connection_string``,
    ``save_persisted_connection_string``, ``clear_persisted_connection_string``.
Risky contracts: ``get_persisted_connection_string`` NEVER raises — any Table /
    file error degrades to an empty string so telemetry init and the settings
    status endpoint stay non-fatal. The connection string is not a secret in the
    Key Vault sense (it is already injected as a plain Container App env var and
    surfaced to authenticated operators by the settings endpoint), so it is
    stored in the row directly, consistent with the existing env-var handling.
Validation: ``uv run pytest -q api/tests/test_app_insights_pref.py``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "appinsightspref"
_TYPE = "appinsights_config"
_PARTITION_KEY = "appinsights_config"
_ROW_KEY = "current"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"

_MAX_LEN = 4096

_ENSURED_TABLES: set[str] = set()
_ENSURED_TABLES_LOCK = threading.Lock()
_AI_TABLE_POOLED: TableClient | None = None
_AI_TABLE_POOL_LOCK = threading.Lock()
_FILE_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean(value: str) -> str:
    """Normalise a candidate connection string; return '' when unusable."""
    cs = (value or "").strip()
    if not cs or len(cs) > _MAX_LEN or "InstrumentationKey=" not in cs:
        return ""
    return cs


def _use_table_backend() -> bool:
    return bool(os.environ.get(_TABLE_ENDPOINT_ENV) and os.environ.get("CONTAINER_APP_NAME"))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def get_persisted_connection_string() -> str:
    """Return the applied connection string, or '' when none / on any error.

    Never raises: a missing row, a Table RBAC/network failure, or a corrupt
    local file all degrade to '' so callers (telemetry init, settings status)
    stay non-fatal.
    """
    try:
        found = _get_table() if _use_table_backend() else _get_file()
    except Exception as exc:
        LOGGER.warning("app_insights_pref read failed: %s", exc)
        return ""
    return _clean(found or "")


def save_persisted_connection_string(
    connection_string: str, *, owner_oid: str = "", tenant_id: str = ""
) -> None:
    """Persist the applied connection string as the durable source of truth."""
    cs = _clean(connection_string)
    if not cs:
        raise ValueError("connection_string is empty or malformed")
    if _use_table_backend():
        _save_table(cs, owner_oid=owner_oid, tenant_id=tenant_id)
    else:
        _save_file(cs, owner_oid=owner_oid, tenant_id=tenant_id)


def clear_persisted_connection_string() -> None:
    """Remove the persisted row so the Table fallback stops resurfacing it."""
    if _use_table_backend():
        _clear_table()
    else:
        _clear_file()


# --------------------------------------------------------------------------- #
# Table backend (Container Apps)
# --------------------------------------------------------------------------- #


def _entity(cs: str, *, owner_oid: str, tenant_id: str) -> dict[str, Any]:
    return {
        "PartitionKey": _PARTITION_KEY,
        "RowKey": _ROW_KEY,
        "type": _TYPE,
        "connection_string": cs,
        "updated_at": _now_iso(),
        "owner_oid": owner_oid,
        "tenant_id": tenant_id,
    }


def _table_client() -> TableClient:
    global _AI_TABLE_POOLED
    pool = _AI_TABLE_POOLED
    if pool is not None:
        return pool  # type: ignore[return-value]
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _AI_TABLE_POOL_LOCK:
        if _AI_TABLE_POOLED is None:
            from api.services.state_repo import _PooledTableClient

            _AI_TABLE_POOLED = _PooledTableClient(  # type: ignore[assignment]
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _AI_TABLE_POOLED  # type: ignore[return-value]


def _reset_app_insights_table_pool() -> None:
    """Test hook + safety valve for credential reset."""
    global _AI_TABLE_POOLED
    with _AI_TABLE_POOL_LOCK:
        pool = _AI_TABLE_POOLED
        _AI_TABLE_POOLED = None
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


def _save_table(cs: str, *, owner_oid: str, tenant_id: str) -> None:
    _ensure_table()
    with _table_client() as table:
        table.upsert_entity(
            _entity(cs, owner_oid=owner_oid, tenant_id=tenant_id), mode=UpdateMode.REPLACE
        )


def _get_table() -> str | None:
    _ensure_table()
    with _table_client() as table:
        try:
            entity = table.get_entity(partition_key=_PARTITION_KEY, row_key=_ROW_KEY)
        except ResourceNotFoundError:
            return None
        return str(dict(entity).get("connection_string") or "")


def _clear_table() -> None:
    _ensure_table()
    with _table_client() as table:
        try:
            table.delete_entity(partition_key=_PARTITION_KEY, row_key=_ROW_KEY)
        except ResourceNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# Local JSON file backend (workstation dev without Table RBAC)
# --------------------------------------------------------------------------- #


def _state_file() -> Path:
    default_root = Path(__file__).resolve().parents[2] / ".logs" / "local" / "state"
    root = Path(os.environ.get(_LOCAL_STATE_ENV, str(default_root)))
    return root / "app_insights_config.json"


def _get_file() -> str | None:
    path = _state_file()
    if not path.exists():
        return None
    try:
        data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None
    return str(data.get("connection_string") or "")


def _save_file(cs: str, *, owner_oid: str, tenant_id: str) -> None:
    path = _state_file()
    payload = {
        "connection_string": cs,
        "updated_at": _now_iso(),
        "owner_oid": owner_oid,
        "tenant_id": tenant_id,
    }
    with _FILE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
        tmp.replace(path)


def _clear_file() -> None:
    path = _state_file()
    with _FILE_LOCK:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

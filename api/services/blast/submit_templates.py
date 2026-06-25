"""Per-user saved BLAST submit templates (named option presets).

Responsibility: CRUD for a researcher's named submit-option presets, persisted
one Azure Table row per template under a per-owner partition. The stored
``fields`` blob is an opaque JSON snapshot of the frontend submit form's option
fields (never the query data itself); this layer validates size/count/name only
and does not interpret the field contents.
Edit boundaries: Azure-Tables access for the ``blasttemplates`` table lives here.
No HTTP shaping (that is ``api/routes/blast/templates.py``) and no submit-option
semantics (those live in ``submit_payload.py``).
Key entry points: ``list_templates``, ``create_template``, ``update_template``,
``delete_template``, ``get_template``.
Risky contracts: ``fields`` is opaque and capped at ``_MAX_FIELDS_BYTES`` so a
template can never store the (potentially large) query FASTA. Per-user count is
capped at ``_MAX_TEMPLATES_PER_USER``. PartitionKey is derived from the caller
``owner_oid`` so a caller can only ever read/write their own partition — the route
layer passes the authenticated ``caller.object_id``, never a client-supplied owner.
Validation: ``uv run pytest -q api/tests/test_blast_templates.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential
from api.services.state.table_pool import (
    _ENSURED_TABLES,
    _ENSURED_TABLES_LOCK,
    _TABLE_ENDPOINT_ENV,
    _PooledTableClient,
)

LOGGER = logging.getLogger(__name__)

_TABLE_NAME = "blasttemplates"
_PARTITION_PREFIX = "tmpl:"

_MAX_TEMPLATES_PER_USER = 50
_MAX_NAME_LEN = 120
_MAX_FIELDS_BYTES = 32_000  # well under the Azure Table 64 KiB property limit
_MAX_FIELDS_KEYS = 200  # the submit form has ~30 option fields; this is generous headroom

_TABLE_POOL: _PooledTableClient | None = None
_TABLE_POOL_LOCK = Lock()


class TemplateValidationError(ValueError):
    """Raised when a template name / fields / count limit is violated."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _partition_key(owner_oid: str) -> str:
    raw = owner_oid or "anonymous"
    return _PARTITION_PREFIX + re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)


def _table_client() -> TableClient:
    global _TABLE_POOL
    pool = _TABLE_POOL
    if pool is not None:
        return pool  # type: ignore[return-value]
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _TABLE_POOL_LOCK:
        if _TABLE_POOL is None:
            _TABLE_POOL = _PooledTableClient(
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _TABLE_POOL  # type: ignore[return-value]


def _reset_table_pool() -> None:
    """Test hook + credential-reset safety valve."""
    global _TABLE_POOL
    with _TABLE_POOL_LOCK:
        pool = _TABLE_POOL
        _TABLE_POOL = None
    if pool is not None:
        close = getattr(pool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: S110 - close races are not fatal
                pass


def _ensure_table() -> None:
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    cache_key = (endpoint, _TABLE_NAME)
    if cache_key in _ENSURED_TABLES:
        return
    with _ENSURED_TABLES_LOCK:
        if cache_key in _ENSURED_TABLES:
            return
        with TableServiceClient(endpoint=endpoint, credential=get_credential()) as service:
            try:
                service.create_table_if_not_exists(_TABLE_NAME)
            except AttributeError:
                try:
                    service.create_table(_TABLE_NAME)
                except ResourceExistsError:
                    pass
        _ENSURED_TABLES.add(cache_key)


@dataclass(frozen=True)
class BlastTemplate:
    id: str
    name: str
    owner_oid: str
    fields: dict[str, Any]
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "fields": self.fields,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _validate_name(name: str) -> str:
    # Strip control characters before trimming so a name of only control chars
    # is rejected as empty and none leak into the stored row / UI.
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", name or "").strip()
    if not cleaned:
        raise TemplateValidationError("template name is required")
    if len(cleaned) > _MAX_NAME_LEN:
        raise TemplateValidationError(f"template name exceeds {_MAX_NAME_LEN} characters")
    return cleaned


def _validate_fields(fields: Any) -> str:
    if not isinstance(fields, dict):
        raise TemplateValidationError("template fields must be an object")
    if len(fields) > _MAX_FIELDS_KEYS:
        raise TemplateValidationError(f"template fields exceed {_MAX_FIELDS_KEYS} keys")
    try:
        encoded = json.dumps(fields, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise TemplateValidationError("template fields are not JSON-serialisable") from exc
    if len(encoded.encode("utf-8")) > _MAX_FIELDS_BYTES:
        raise TemplateValidationError(
            f"template fields exceed {_MAX_FIELDS_BYTES} bytes (the query data is not stored "
            "in a template — only submit options)"
        )
    return encoded


def _row_to_template(entity: dict[str, Any]) -> BlastTemplate:
    try:
        fields = json.loads(str(entity.get("fields_json") or "{}"))
    except json.JSONDecodeError:
        fields = {}
    if not isinstance(fields, dict):
        fields = {}
    return BlastTemplate(
        id=str(entity.get("RowKey") or ""),
        name=str(entity.get("name") or ""),
        owner_oid=str(entity.get("owner_oid") or ""),
        fields=fields,
        created_at=str(entity.get("created_at") or ""),
        updated_at=str(entity.get("updated_at") or ""),
    )


def list_templates(owner_oid: str) -> list[BlastTemplate]:
    """Return every template owned by ``owner_oid`` (most-recently-updated first)."""
    try:
        _ensure_table()
        rows: list[BlastTemplate] = []
        with _table_client() as table:
            entities = table.query_entities(
                f"PartitionKey eq '{_partition_key(owner_oid)}'"
            )
            for e in entities:
                rows.append(_row_to_template(dict(e)))
        rows.sort(key=lambda t: t.updated_at, reverse=True)
        return rows
    except Exception as exc:
        LOGGER.warning("blast template list failed: %s", type(exc).__name__)
        return []


def get_template(owner_oid: str, template_id: str) -> BlastTemplate | None:
    try:
        _ensure_table()
        with _table_client() as table:
            try:
                entity = table.get_entity(
                    partition_key=_partition_key(owner_oid), row_key=template_id
                )
            except ResourceNotFoundError:
                return None
            return _row_to_template(dict(entity))
    except Exception as exc:
        LOGGER.warning("blast template get failed: %s", type(exc).__name__)
        return None


def create_template(owner_oid: str, name: str, fields: Any) -> BlastTemplate:
    """Create a new template. Raises ``TemplateValidationError`` on limit breach."""
    clean_name = _validate_name(name)
    encoded_fields = _validate_fields(fields)

    existing = list_templates(owner_oid)
    if len(existing) >= _MAX_TEMPLATES_PER_USER:
        raise TemplateValidationError(
            f"template limit reached ({_MAX_TEMPLATES_PER_USER}); delete one first"
        )
    if any(t.name == clean_name for t in existing):
        raise TemplateValidationError(f"a template named '{clean_name}' already exists")

    _ensure_table()
    now = _now_iso()
    template_id = uuid.uuid4().hex
    entity = {
        "PartitionKey": _partition_key(owner_oid),
        "RowKey": template_id,
        "owner_oid": owner_oid or "",
        "name": clean_name,
        "fields_json": encoded_fields,
        "created_at": now,
        "updated_at": now,
    }
    with _table_client() as table:
        table.create_entity(entity)
    return _row_to_template(entity)


def update_template(
    owner_oid: str,
    template_id: str,
    *,
    name: str | None = None,
    fields: Any | None = None,
) -> BlastTemplate | None:
    """Patch a template's name and/or fields. Returns ``None`` when not found."""
    current = get_template(owner_oid, template_id)
    if current is None:
        return None

    clean_name = _validate_name(name) if name is not None else current.name
    encoded_fields = (
        _validate_fields(fields)
        if fields is not None
        else json.dumps(current.fields, separators=(",", ":"), ensure_ascii=False)
    )

    now = _now_iso()
    entity = {
        "PartitionKey": _partition_key(owner_oid),
        "RowKey": template_id,
        "owner_oid": owner_oid or "",
        "name": clean_name,
        "fields_json": encoded_fields,
        "created_at": current.created_at,
        "updated_at": now,
    }
    with _table_client() as table:
        table.upsert_entity(entity, mode=UpdateMode.REPLACE)
    return _row_to_template(entity)


def delete_template(owner_oid: str, template_id: str) -> bool:
    """Delete a template. Returns ``False`` when it did not exist."""
    try:
        _ensure_table()
        with _table_client() as table:
            try:
                table.delete_entity(
                    partition_key=_partition_key(owner_oid), row_key=template_id
                )
            except ResourceNotFoundError:
                return False
        return True
    except Exception as exc:
        LOGGER.warning("blast template delete failed: %s", type(exc).__name__)
        return False

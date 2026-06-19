"""NCBI API key preference (single deployment-wide row).

Responsibility: Persist and read the optional NCBI E-utilities API key that a
dashboard operator can paste in Settings to lift the shared NCBI rate tier from
3 req/s (no key) to 10 req/s. Exactly ONE row per deployment (PartitionKey
``ncbi_pref`` / RowKey ``current``). The key is consumed by
``api.services.ncbi._eutils.ncbi_identity_params`` via ``get_ncbi_api_key`` only
when the ``NCBI_API_KEY`` env is unset (env always wins).
Edit boundaries: Reusable persistence logic only — HTTP shaping lives in
``api.routes.settings.ncbi``; the NCBI calls live in ``api.services.ncbi``. No
NCBI HTTP here.
Key entry points: ``get_ncbi_api_key``, ``save_ncbi_api_key``,
``ncbi_settings_public``, ``clear_ncbi_pref_cache``.
Risky contracts: The plaintext key is NEVER returned to the browser — only a
masked view (presence + last 4 chars + source) via ``ncbi_settings_public`` /
the route. A short in-process TTL cache keeps the per-request NCBI identity
lookup cheap; ``save_ncbi_api_key`` invalidates it. Table backend is gated by
``AZURE_TABLE_ENDPOINT`` + ``CONTAINER_APP_NAME`` (mirrors ``service_bus_pref``);
local dev falls back to a JSON file under ``ELB_LOCAL_STATE_DIR``. The key is a
read-only rate-limit token (no NCBI account access), so a missing Key Vault
round-trip is an acceptable trade for the lighter store.
Validation: ``uv run pytest -q api/tests/test_ncbi_pref.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_TABLE_NAME = "ncbipref"
_PARTITION_KEY = "ncbi_pref"
_ROW_KEY = "current"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"

# NCBI API keys are 36-char lowercase hex-ish tokens; accept a generous
# alphanumeric range so a future format change does not reject a valid key,
# but bound the length so a paste error cannot store a huge blob.
_RE_API_KEY = re.compile(r"^[A-Za-z0-9]{10,128}$")

# In-process TTL cache so ``ncbi_identity_params`` (called once per NCBI
# request) does not hit the Table every time. 60 s is short enough that a key
# change in Settings takes effect promptly across sidecars.
_CACHE_TTL_SECONDS = 60.0
_CACHE_LOCK = threading.Lock()
_CACHE_VALUE: dict[str, Any] | None = None
_CACHE_AT: float = 0.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _table_backend_enabled() -> bool:
    return bool(
        os.environ.get(_TABLE_ENDPOINT_ENV, "").strip()
        and os.environ.get("CONTAINER_APP_NAME", "").strip()
    )


def _local_state_path() -> Path:
    base = os.environ.get(_LOCAL_STATE_ENV, "").strip() or ".local-state"
    return Path(base) / "ncbi_pref.json"


def _read_row() -> dict[str, Any]:
    """Return the raw stored row ({} when none) from Table or JSON file."""
    if _table_backend_enabled():
        try:
            client = _table_service().get_table_client(_TABLE_NAME)
            entity = client.get_entity(_PARTITION_KEY, _ROW_KEY)
            return dict(entity)
        except ResourceNotFoundError:
            return {}
        except Exception as exc:  # pragma: no cover - degrade to empty
            LOGGER.warning("ncbi_pref table read failed: %s", type(exc).__name__)
            return {}
    path = _local_state_path()
    try:
        if path.exists():
            return json.loads(path.read_text("utf-8")) or {}
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("ncbi_pref file read failed: %s", type(exc).__name__)
    return {}


def _write_row(row: dict[str, Any]) -> None:
    if _table_backend_enabled():
        client = _table_service().get_table_client(_TABLE_NAME)
        try:
            client.create_table()
        except Exception as exc:
            LOGGER.debug("ncbi_pref create_table skipped: %s", type(exc).__name__)
        entity = {
            "PartitionKey": _PARTITION_KEY,
            "RowKey": _ROW_KEY,
            **row,
        }
        client.upsert_entity(entity, mode=UpdateMode.REPLACE)
        return
    path = _local_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, indent=2), "utf-8")


def _table_service() -> TableServiceClient:
    endpoint = os.environ[_TABLE_ENDPOINT_ENV].strip()
    return TableServiceClient(endpoint=endpoint, credential=get_credential())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_ncbi_api_key() -> str | None:
    """Return the stored NCBI API key (cached), or None when unset."""
    global _CACHE_VALUE, _CACHE_AT
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE_VALUE is not None and (now - _CACHE_AT) < _CACHE_TTL_SECONDS:
            key = _CACHE_VALUE.get("api_key")
            return key or None
    row = _read_row()
    with _CACHE_LOCK:
        _CACHE_VALUE = row
        _CACHE_AT = time.monotonic()
    key = str(row.get("api_key") or "").strip()
    return key or None


def save_ncbi_api_key(key: str | None, *, owner_oid: str | None = None) -> dict[str, Any]:
    """Persist (or clear when falsy) the NCBI API key and return the masked view.

    A non-empty key must match ``_RE_API_KEY``; an empty/None value clears the
    stored key. Invalidates the TTL cache so the next NCBI call sees the change.
    """
    global _CACHE_VALUE, _CACHE_AT
    cleaned = str(key or "").strip()
    if cleaned and not _RE_API_KEY.match(cleaned):
        raise ValueError("NCBI API key must be 10-128 alphanumeric characters")
    row: dict[str, Any] = {
        "api_key": cleaned,
        "updated_at": _now_iso(),
        "updated_by": str(owner_oid or "").strip()[:64],
    }
    _write_row(row)
    with _CACHE_LOCK:
        _CACHE_VALUE = row
        _CACHE_AT = time.monotonic()
    return _public_view(row)


def ncbi_settings_public() -> dict[str, Any]:
    """Return the masked Settings view (never the plaintext key)."""
    return _public_view(_read_row())


def clear_ncbi_pref_cache() -> None:
    """Test/ops hook — drop the in-process TTL cache."""
    global _CACHE_VALUE, _CACHE_AT
    with _CACHE_LOCK:
        _CACHE_VALUE = None
        _CACHE_AT = 0.0


def _public_view(row: dict[str, Any]) -> dict[str, Any]:
    key = str(row.get("api_key") or "").strip()
    env_key = os.environ.get("NCBI_API_KEY", "").strip()
    if env_key:
        source = "env"
    elif key:
        source = "settings"
    else:
        source = "none"
    return {
        "has_key": bool(env_key or key),
        "last4": (env_key or key)[-4:] if (env_key or key) else None,
        "source": source,
        "env_locked": bool(env_key),
        "updated_at": row.get("updated_at"),
    }

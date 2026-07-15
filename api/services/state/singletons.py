"""Tiny key-value singleton store backed by Azure Table Storage.

Module docstring (natural):
Helper for the small number of dashboard "global state" rows that do not fit
the JobState schema — currently the public-HTTPS endpoint URL written by
`setup_openapi_public_https`. The store survives Container App revision
restarts (which wipe the in-revision Redis sidecar) so the SPA's
"Exposed / Not exposed" badge stays accurate across deploys without
needing the operator to click Enable again.

Responsibility: Provide best-effort and strict singleton payload operations
    backed by the dedicated `dashboardsingletons` Azure Table.
Edit boundaries: Pure storage primitive — no Azure-management or
    domain-specific logic. New singletons live in their own caller modules
    and just pass a unique ``key`` string.
Key entry points: ``save_singleton``, ``load_singleton``,
    ``load_singleton_strict``, ``clear_singleton``.
Risky contracts: ``key`` must be ASCII-safe (Azure RowKey forbids ``/ \\ # ?``
    and control chars). ``payload`` must be JSON-serialisable. Best-effort
    reads collapse SDK failures to missing; safety callers must use the strict
    read, which propagates every error except a confirmed missing row.
Validation: ``uv run pytest -q api/tests/test_state_singletons.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

_SINGLETON_TABLE_NAME = "dashboardsingletons"
_SINGLETON_PARTITION_KEY = "singleton"
_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_ROW_KEY_DISALLOWED_RE = re.compile(r"[/\\#?\u0000-\u001f\u007f-\u009f]")

_CLIENT: TableClient | None = None
_CLIENT_LOCK = threading.Lock()
_TABLE_ENSURED = False
_TABLE_ENSURED_LOCK = threading.Lock()


def _sanitise_row_key(key: str) -> str:
    return _ROW_KEY_DISALLOWED_RE.sub("-", key.strip())


def _endpoint() -> str:
    return os.environ.get(_TABLE_ENDPOINT_ENV, "").strip()


def _get_client() -> TableClient | None:
    """Return a pooled ``TableClient`` or ``None`` when not configured.

    Returns ``None`` (instead of raising) when ``AZURE_TABLE_ENDPOINT`` is
    unset — the caller should fall back to Redis-only storage in that
    case (typical for local dev where the dashboard runs without Azure
    Storage credentials).
    """
    global _CLIENT
    endpoint = _endpoint()
    if not endpoint:
        return None
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = TableClient(
                endpoint=endpoint,
                table_name=_SINGLETON_TABLE_NAME,
                credential=get_credential(),
            )
        return _CLIENT


def _ensure_table() -> bool:
    """Create the singleton table on first use, with per-process cache."""
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return True
    endpoint = _endpoint()
    if not endpoint:
        return False
    with _TABLE_ENSURED_LOCK:
        if _TABLE_ENSURED:
            return True
        try:
            service = TableServiceClient(endpoint=endpoint, credential=get_credential())
            try:
                service.create_table_if_not_exists(_SINGLETON_TABLE_NAME)
            except AttributeError:  # pragma: no cover - older SDK
                try:
                    service.create_table(_SINGLETON_TABLE_NAME)
                except Exception as create_exc:
                    LOGGER.debug(
                        "singleton create_table fallback failed: %s", create_exc
                    )
            _TABLE_ENSURED = True
            return True
        except Exception as exc:
            LOGGER.warning("singleton table ensure failed: %s", exc)
            return False


def save_singleton(key: str, payload: dict[str, Any]) -> bool:
    """Upsert ``payload`` for ``key``. Returns False on best-effort failure."""
    if not key:
        return False
    client = _get_client()
    if client is None:
        return False
    if not _ensure_table():
        return False
    try:
        body = json.dumps(payload, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        LOGGER.warning("singleton payload not JSON-serialisable for %s: %s", key, exc)
        return False
    entity = {
        "PartitionKey": _SINGLETON_PARTITION_KEY,
        "RowKey": _sanitise_row_key(key),
        "payload": body,
    }
    try:
        client.upsert_entity(entity)
        return True
    except Exception as exc:
        LOGGER.warning("singleton upsert failed for %s: %s", key, exc)
        return False


def load_singleton(key: str) -> dict[str, Any] | None:
    """Return the parsed payload for ``key`` or ``None`` when missing."""
    if not key:
        return None
    client = _get_client()
    if client is None:
        return None
    row_key = _sanitise_row_key(key)
    try:
        entity = client.get_entity(_SINGLETON_PARTITION_KEY, row_key)
    except Exception as exc:
        # ResourceNotFoundError is the common case; log only at debug.
        LOGGER.debug("singleton load miss for %s: %s", key, type(exc).__name__)
        return None
    raw = entity.get("payload")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def load_singleton_strict(key: str) -> dict[str, Any] | None:
    """Return one payload while distinguishing absence from storage failure.

    Most singleton consumers are informational and deliberately use the
    best-effort :func:`load_singleton`. Safety state such as AKS execution
    admission must fail closed instead: a transient Table error cannot be
    interpreted as "no lifecycle barrier". Missing rows still return ``None``;
    every other SDK or payload error propagates to the caller.
    """
    if not key:
        return None
    client = _get_client()
    if client is None:
        raise RuntimeError("singleton Table Storage is not configured")
    row_key = _sanitise_row_key(key)
    try:
        entity = client.get_entity(_SINGLETON_PARTITION_KEY, row_key)
    except ResourceNotFoundError:
        return None
    raw = entity.get("payload")
    if not isinstance(raw, str):
        raise ValueError(f"singleton payload is missing for {key}")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"singleton payload is not an object for {key}")
    return parsed


def clear_singleton(key: str) -> bool:
    """Delete ``key``. Returns True even when the row was already absent."""
    if not key:
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        client.delete_entity(_SINGLETON_PARTITION_KEY, _sanitise_row_key(key))
        return True
    except Exception as exc:
        LOGGER.debug("singleton delete swallowed for %s: %s", key, type(exc).__name__)
        return True


def _prefix_upper_bound(prefix: str) -> str:
    """Smallest string strictly greater than every string starting with ``prefix``.

    Increment the last code point of ``prefix`` so an Azure Table
    ``RowKey lt <upper>`` range covers exactly the rows whose key starts with
    ``prefix`` — independent of which characters appear in the suffix. A fixed
    trailing sentinel (e.g. a run of ``~``) is wrong: it silently drops keys
    whose suffix sorts at/above the sentinel, and RowKeys may legitimately
    contain characters above ``~`` (the sanitiser only strips ``/ \\ # ?`` and
    ``\\u0000-\\u001f`` / ``\\u007f-\\u009f``, so e.g. accented letters survive).
    Deriving the bound from the prefix itself is correct for any suffix charset.

    ``prefix`` is always non-empty here (the caller guards it), so indexing
    ``prefix[-1]`` is safe.
    """
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def list_singletons_by_prefix(prefix: str) -> list[tuple[str, dict[str, Any]]]:
    """Return every ``(row_key, payload)`` whose row key starts with ``prefix``.

    Best-effort: returns an empty list when the table is not configured
    or the query raises. The row key in the result is the *sanitised*
    form (the same value passed to ``save_singleton``), so callers that
    re-derive a typed key from the row key must apply the same sanitiser.

    Used by the openapi public-https reconciler to enumerate every
    per-cluster cache entry instead of being limited to a single
    ``openapi:runtime:public-base-url`` row.
    """
    if not prefix:
        return []
    client = _get_client()
    if client is None:
        return []
    sanitised_prefix = _sanitise_row_key(prefix)
    if not sanitised_prefix:
        return []
    # Azure Table range query: PartitionKey + (RowKey >= prefix AND
    # RowKey < prefix-upper-bound). The upper bound is the prefix with its last
    # code point incremented so the range covers EVERY key starting with the
    # prefix regardless of the suffix's characters (see _prefix_upper_bound).
    upper = _prefix_upper_bound(sanitised_prefix)
    query = (
        f"PartitionKey eq '{_SINGLETON_PARTITION_KEY}' "
        f"and RowKey ge '{sanitised_prefix}' "
        f"and RowKey lt '{upper}'"
    )
    results: list[tuple[str, dict[str, Any]]] = []
    try:
        for entity in client.query_entities(query_filter=query):
            row_key = entity.get("RowKey")
            raw = entity.get("payload")
            if not isinstance(row_key, str) or not isinstance(raw, str):
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                results.append((row_key, parsed))
    except Exception as exc:
        LOGGER.warning(
            "singleton list_by_prefix failed prefix=%s: %s",
            sanitised_prefix,
            type(exc).__name__,
        )
        return []
    return results


def reset_singleton_cache_for_tests() -> None:
    """Drop the process-wide client + ensured-table memo (tests only)."""
    global _CLIENT, _TABLE_ENSURED
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            try:
                _CLIENT.close()
            except Exception as exc:
                LOGGER.debug("singleton client close ignored: %s", exc)
        _CLIENT = None
    with _TABLE_ENSURED_LOCK:
        _TABLE_ENSURED = False


__all__ = [
    "clear_singleton",
    "list_singletons_by_prefix",
    "load_singleton",
    "load_singleton_strict",
    "reset_singleton_cache_for_tests",
    "save_singleton",
]

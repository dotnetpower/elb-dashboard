"""Persisted mapping of ACR run_id -> (image, tag) for in-progress builds.

Responsibility: Persist run_id -> image:tag for ACR scheduleRun builds so the
ACR card can surface "Building" rows after a browser refresh, even when the
ACR Run.output_images field is still empty (ACR only populates output_images
after the push step completes; Queued/Started/Running runs typically have an
empty list).
Edit boundaries: Keep reusable domain logic here; routes and tasks should call
this layer instead of duplicating Table SDK code.
Key entry points: `record_pending_build`, `load_pending_builds`,
`prune_terminal_builds`.
Risky contracts: Table writes are best-effort — callers must tolerate
``RuntimeError``/transient Azure failures (the ACR build itself still
succeeded; only the per-row "Building" hint is lost). Never raise out of
these helpers into a Celery task that already scheduled the build.
Validation: `uv run pytest -q api/tests/test_acr_build_state.py`.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

ACR_BUILDS_TABLE = os.environ.get("ACR_BUILD_STATE_TABLE", "acrbuildruns")

_TABLE_POOLED: TableClient | None = None
_TABLE_POOL_LOCK = threading.Lock()
_ENSURED_TABLES: set[tuple[str, str]] = set()
_ENSURED_TABLES_LOCK = threading.Lock()


def _table_endpoint() -> str:
    endpoint = os.environ.get("AZURE_TABLE_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError("AZURE_TABLE_ENDPOINT is not set")
    return endpoint


def _ensure_table(endpoint: str) -> None:
    key = (endpoint, ACR_BUILDS_TABLE)
    if key in _ENSURED_TABLES:
        return
    with _ENSURED_TABLES_LOCK:
        if key in _ENSURED_TABLES:
            return
        with TableServiceClient(endpoint=endpoint, credential=get_credential()) as service:
            try:
                service.create_table_if_not_exists(ACR_BUILDS_TABLE)
            except AttributeError:
                try:
                    service.create_table(ACR_BUILDS_TABLE)
                except ResourceExistsError:
                    pass
        _ENSURED_TABLES.add(key)


def _table_client() -> TableClient:
    """Return a process-shared pooled TableClient for the acrbuildruns table."""
    endpoint = _table_endpoint()
    _ensure_table(endpoint)
    global _TABLE_POOLED
    pool = _TABLE_POOLED
    if pool is not None:
        return pool
    with _TABLE_POOL_LOCK:
        if _TABLE_POOLED is None:
            from api.services.state_repo import _PooledTableClient

            _TABLE_POOLED = _PooledTableClient(  # type: ignore[assignment]
                TableClient(
                    endpoint=endpoint,
                    table_name=ACR_BUILDS_TABLE,
                    credential=get_credential(),
                )
            )
        return _TABLE_POOLED  # type: ignore[return-value]


def _reset_table_pool() -> None:
    """Test hook + safety valve for credential reset."""
    global _TABLE_POOLED
    with _TABLE_POOL_LOCK:
        pool = _TABLE_POOLED
        _TABLE_POOLED = None
    if pool is not None:
        close = getattr(pool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: S110 - close races are not fatal
                pass


def _partition_key(registry_name: str) -> str:
    # Azure Table PartitionKey rejects '/', '\\', '#', '?'. Registry names are
    # already alphanumeric per ACR naming rules, but lowercase for stability.
    return (registry_name or "").lower()


def _row_key(run_id: str) -> str:
    return (run_id or "").strip()


def record_pending_build(
    registry_name: str,
    run_id: str,
    image: str,
    tag: str,
) -> None:
    """Upsert a (registry, run_id) row recording the target image:tag.

    Best-effort. Logs and swallows exceptions — losing the row only degrades
    the per-row "Building" UI hint; the build itself is unaffected.
    """
    if not registry_name or not run_id or not image or not tag:
        return
    entity = {
        "PartitionKey": _partition_key(registry_name),
        "RowKey": _row_key(run_id),
        "image": image,
        "tag": tag,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        with _table_client() as t:
            t.upsert_entity(entity, mode=UpdateMode.REPLACE)
    except Exception as exc:
        LOGGER.warning(
            "acr_build_state: failed to record run_id=%s image=%s:%s (%s)",
            run_id,
            image,
            tag,
            type(exc).__name__,
        )


def load_pending_builds(registry_name: str) -> dict[str, dict[str, str]]:
    """Return ``{run_id: {"image": str, "tag": str, "created_at": str}}`` for the registry.

    Best-effort. Returns an empty dict on any failure (table missing,
    credential issue, transient Storage hiccup) so monitoring callers can
    silently fall back to the ``output_images`` path.
    """
    if not registry_name:
        return {}
    pkey = _partition_key(registry_name)
    out: dict[str, dict[str, str]] = {}
    try:
        with _table_client() as t:
            for ent in t.query_entities(f"PartitionKey eq '{pkey}'"):
                run_id = str(ent.get("RowKey") or "")
                image = str(ent.get("image") or "")
                tag = str(ent.get("tag") or "")
                if not run_id or not image or not tag:
                    continue
                out[run_id] = {
                    "image": image,
                    "tag": tag,
                    "created_at": str(ent.get("created_at") or ""),
                }
    except Exception as exc:
        LOGGER.debug(
            "acr_build_state: load_pending_builds failed (%s) — falling back to output_images only",
            type(exc).__name__,
        )
        return {}
    return out


def prune_terminal_builds(registry_name: str, terminal_run_ids: set[str]) -> None:
    """Delete recorded mappings whose run reached a terminal status.

    Best-effort. Skips silently on missing rows / transient failures.
    """
    if not registry_name or not terminal_run_ids:
        return
    pkey = _partition_key(registry_name)
    try:
        client = _table_client()
    except Exception as exc:
        LOGGER.debug(
            "acr_build_state: prune skipped (%s)", type(exc).__name__
        )
        return
    with client as t:
        for run_id in terminal_run_ids:
            row = _row_key(run_id)
            if not row:
                continue
            try:
                t.delete_entity(partition_key=pkey, row_key=row)
            except ResourceNotFoundError:
                continue
            except Exception as exc:
                LOGGER.debug(
                    "acr_build_state: failed to delete run_id=%s (%s)",
                    run_id,
                    type(exc).__name__,
                )


def _row_payload_for_test(registry_name: str, run_id: str) -> dict[str, Any] | None:
    """Test helper: read a single row directly. Returns None if missing."""
    pkey = _partition_key(registry_name)
    try:
        with _table_client() as t:
            return dict(t.get_entity(partition_key=pkey, row_key=_row_key(run_id)))
    except ResourceNotFoundError:
        return None

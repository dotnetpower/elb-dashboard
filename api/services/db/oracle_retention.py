"""Bounded mark-and-sweep retention for DB order-oracle run artifacts.

Responsibility: Plan and execute conservative age-based deletion of terminal
    oracle runs while preserving current, previous-ready, active, referenced,
    recent, malformed, and unknown-state runs.
Edit boundaries: Workload Storage oracle history only; task scheduling and
    preference inventory live outside this module.
Key entry points: `select_oracle_runs_for_retention`, `purge_oracle_history`.
Risky contracts: Default is dry-run; deletion acquires a create-only GC marker,
    rechecks current/active/references, and deletes at most 20 runs/200 blobs.
Validation: `uv run pytest -q api/tests/test_oracle_retention.py`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from itertools import islice
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from api.services.db.order_oracle import (
    ORACLE_PARTS_DIR,
    ORACLE_PREFIX_ROOT,
    ORACLE_REFERENCES_DIR,
    ORACLE_RUNS_DIR,
    oracle_active_blob_path,
    oracle_gc_marker_blob_path,
    oracle_retention_cursor_blob_path,
    oracle_status_blob_path,
)
from api.services.storage.blob_io import read_metadata_blob_text

LOGGER = logging.getLogger(__name__)

_MAX_RUN_SCAN = 50
_TERMINAL_STATUSES = frozenset({"ready", "failed", "superseded", "timeout"})


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def select_oracle_runs_for_retention(
    run_documents: dict[str, dict[str, Any]],
    *,
    current_run_id: str,
    active_run_id: str,
    referenced_run_ids: set[str],
    previous_run_id: str = "",
    preserve_ready_without_previous: bool = False,
    now: datetime,
    days: int = 14,
) -> list[str]:
    """Return oldest-first deletable run IDs without performing I/O."""
    cutoff = now - timedelta(days=max(1, days))
    protected = {
        current_run_id,
        previous_run_id,
        active_run_id,
        *referenced_run_ids,
    }
    ready_history: list[tuple[datetime, str]] = []
    for run_id, document in run_documents.items():
        finished = _parse_time(document.get("finished_at"))
        if document.get("status") == "ready" and finished is not None:
            ready_history.append((finished, run_id))
    if preserve_ready_without_previous and not previous_run_id:
        protected.update(run_id for _finished, run_id in ready_history)
    elif not previous_run_id:
        previous_ready = sorted(
            (item for item in ready_history if item[1] != current_run_id),
            reverse=True,
        )
        if previous_ready:
            protected.add(previous_ready[0][1])

    candidates: list[tuple[datetime, str]] = []
    for run_id, document in run_documents.items():
        finished = _parse_time(document.get("finished_at"))
        if (
            run_id in protected
            or str(document.get("status") or "") not in _TERMINAL_STATUSES
            or finished is None
            or finished > cutoff
        ):
            continue
        candidates.append((finished, run_id))
    return [run_id for _finished, run_id in sorted(candidates)]


def _read_optional_document(container: Any, path: str) -> dict[str, Any] | None:
    try:
        text = read_metadata_blob_text(
            container.get_blob_client(path),
            max_bytes=1024 * 1024,
            label="oracle-retention-state",
        )
    except ResourceNotFoundError:
        return None
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("oracle retention state must be an object")
    return value


def _listed_names(container: Any, prefix: str, limit: int) -> list[str]:
    return [
        str(blob.name) for blob in islice(container.list_blobs(name_starts_with=prefix), limit + 1)
    ]


def _listed_page(
    container: Any,
    prefix: str,
    limit: int,
    cursor: str,
) -> tuple[list[str], str]:
    """Read one bounded page; list fakes use a last-name cursor."""
    try:
        listing = container.list_blobs(
            name_starts_with=prefix,
            results_per_page=limit,
        )
    except TypeError:
        listing = container.list_blobs(name_starts_with=prefix)
    by_page = getattr(listing, "by_page", None)
    if callable(by_page):
        pager = by_page(continuation_token=cursor or None)
        try:
            page = next(iter(pager))
        except StopIteration:
            return [], ""
        names = [str(blob.name) for blob in page]
        return names, str(getattr(pager, "continuation_token", "") or "")

    names = sorted(str(blob.name) for blob in listing)
    if cursor.startswith("name:"):
        last_name = cursor.removeprefix("name:")
        names = [name for name in names if name > last_name]
    page_names = names[:limit]
    next_cursor = f"name:{page_names[-1]}" if len(names) > len(page_names) and page_names else ""
    return page_names, next_cursor


def _read_retention_cursor(container: Any, db_name: str) -> str:
    try:
        document = _read_optional_document(container, oracle_retention_cursor_blob_path(db_name))
    except Exception as exc:
        LOGGER.warning(
            "oracle retention cursor reset db=%s reason=%s",
            db_name,
            type(exc).__name__,
        )
        return ""
    return str((document or {}).get("continuation_token") or "")


def _write_retention_cursor(
    container: Any,
    *,
    db_name: str,
    cursor: str,
) -> None:
    try:
        container.get_blob_client(oracle_retention_cursor_blob_path(db_name)).upload_blob(
            json.dumps(
                {
                    "schema_version": 1,
                    "db_name": db_name,
                    "continuation_token": cursor,
                    "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
                sort_keys=True,
            ),
            overwrite=True,
        )
    except Exception as exc:
        LOGGER.warning(
            "oracle retention cursor write skipped db=%s reason=%s",
            db_name,
            type(exc).__name__,
        )


def _run_id_from_status_path(db_name: str, path: str) -> str:
    prefix = f"{ORACLE_PREFIX_ROOT}/{db_name}/{ORACLE_RUNS_DIR}/"
    suffix = path.removeprefix(prefix)
    parts = suffix.split("/")
    return parts[0] if len(parts) == 2 and parts[1] == "status.json" else ""


def _delete_marker(container: Any, path: str) -> None:
    try:
        container.get_blob_client(path).delete_blob()
    except Exception as exc:
        LOGGER.warning(
            "oracle retention marker cleanup skipped path=%s reason=%s",
            path,
            type(exc).__name__,
        )


def purge_oracle_history(
    container: Any,
    *,
    db_name: str,
    days: int = 14,
    dry_run: bool = True,
    max_runs: int = 20,
    max_blobs: int = 200,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Conservatively drain old unreferenced history for one database."""
    summary: dict[str, Any] = {
        "db_name": db_name,
        "dry_run": dry_run,
        "days": days,
        "status": "completed",
        "scanned_runs": 0,
        "scan_truncated": False,
        "planned_runs": [],
        "purged_runs": [],
        "deleted_blobs": 0,
        "errors": [],
    }
    run_prefix = f"{ORACLE_PREFIX_ROOT}/{db_name}/{ORACLE_RUNS_DIR}/"
    reference_prefix = f"{ORACLE_PREFIX_ROOT}/{db_name}/{ORACLE_REFERENCES_DIR}/"
    try:
        cursor = _read_retention_cursor(container, db_name)
        try:
            status_names, next_cursor = _listed_page(container, run_prefix, _MAX_RUN_SCAN, cursor)
        except Exception:
            if not cursor:
                raise
            LOGGER.warning(
                "oracle retention continuation reset db=%s",
                db_name,
            )
            status_names, next_cursor = _listed_page(container, run_prefix, _MAX_RUN_SCAN, "")
        summary["scan_truncated"] = bool(cursor or next_cursor)
        run_documents: dict[str, dict[str, Any]] = {}
        for path in status_names:
            run_id = _run_id_from_status_path(db_name, path)
            if not run_id:
                continue
            try:
                document = _read_optional_document(container, path)
            except Exception as exc:
                LOGGER.warning(
                    "oracle retention run skipped db=%s run_id=%s reason=%s",
                    db_name,
                    run_id,
                    type(exc).__name__,
                )
                summary["errors"].append({"run_id": run_id, "error": type(exc).__name__})
                continue
            if document is None:
                continue
            run_documents[run_id] = document
        current = _read_optional_document(container, oracle_status_blob_path(db_name))
        active = _read_optional_document(container, oracle_active_blob_path(db_name))
    except Exception as exc:
        LOGGER.warning(
            "oracle retention inventory failed db=%s reason=%s",
            db_name,
            type(exc).__name__,
        )
        summary.update(status="blocked", errors=["retention_inventory_failed"])
        return summary

    summary["scanned_runs"] = len(run_documents)
    current_run_id = str((current or {}).get("run_id") or "")
    previous_run_id = str((current or {}).get("previous_run_id") or "")
    active_run_id = str((active or {}).get("run_id") or "")
    raw_candidates = select_oracle_runs_for_retention(
        run_documents,
        current_run_id=current_run_id,
        active_run_id=active_run_id,
        referenced_run_ids=set(),
        previous_run_id=previous_run_id,
        preserve_ready_without_previous=bool(summary["scan_truncated"] and not previous_run_id),
        now=now or datetime.now(UTC),
        days=days,
    )
    candidates: list[str] = []
    for run_id in raw_candidates:
        if len(candidates) >= max(1, min(max_runs, 20)):
            break
        try:
            if _listed_names(container, f"{reference_prefix}{run_id}/", 1):
                continue
        except Exception as exc:
            summary["errors"].append({"run_id": run_id, "error": type(exc).__name__})
            # Keep marker_path after complete deletion. It is a permanent
            # tombstone for a stale resolver that selected this run before its
            # status and parts disappeared but has not written its reference yet.
            continue
        candidates.append(run_id)
    summary["planned_runs"] = list(candidates)
    if dry_run:
        return summary

    blob_budget = max(1, min(max_blobs, 200))
    for run_id in candidates:
        if summary["deleted_blobs"] >= blob_budget:
            break
        marker_path = oracle_gc_marker_blob_path(db_name, run_id)
        marker = container.get_blob_client(marker_path)
        claimed_now = True
        try:
            marker.upload_blob(
                json.dumps(
                    {
                        "schema_version": 1,
                        "db_name": db_name,
                        "run_id": run_id,
                        "status": "retiring",
                        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                    sort_keys=True,
                ),
                overwrite=False,
            )
        except ResourceExistsError:
            claimed_now = False
        except Exception as exc:
            summary["errors"].append({"run_id": run_id, "error": type(exc).__name__})
            continue

        try:
            fresh_refs = _listed_names(container, f"{reference_prefix}{run_id}/", 1)
            fresh_current = _read_optional_document(container, oracle_status_blob_path(db_name))
            fresh_active = _read_optional_document(container, oracle_active_blob_path(db_name))
        except Exception as exc:
            summary["errors"].append({"run_id": run_id, "error": type(exc).__name__})
            if claimed_now:
                _delete_marker(container, marker_path)
            continue
        if (
            fresh_refs
            or str((fresh_current or {}).get("run_id") or "") == run_id
            or str((fresh_active or {}).get("run_id") or "") == run_id
        ):
            if claimed_now:
                _delete_marker(container, marker_path)
            continue

        parts_prefix = f"{ORACLE_PREFIX_ROOT}/{db_name}/{ORACLE_PARTS_DIR}/{run_id}/"
        remaining = blob_budget - int(summary["deleted_blobs"])
        part_names = _listed_names(container, parts_prefix, remaining)
        part_error = False
        for path in part_names[:remaining]:
            try:
                container.get_blob_client(path).delete_blob()
                summary["deleted_blobs"] += 1
            except ResourceNotFoundError:
                continue
            except Exception as exc:
                part_error = True
                summary["errors"].append({"run_id": run_id, "error": type(exc).__name__})
                break
        if part_error or len(part_names) > remaining:
            continue
        if _listed_names(container, f"{reference_prefix}{run_id}/", 1):
            continue
        if _listed_names(container, parts_prefix, 1):
            continue
        if summary["deleted_blobs"] >= blob_budget:
            continue
        try:
            container.get_blob_client(f"{run_prefix}{run_id}/status.json").delete_blob()
            summary["deleted_blobs"] += 1
            summary["purged_runs"].append(run_id)
        except ResourceNotFoundError:
            summary["purged_runs"].append(run_id)
        except Exception as exc:
            summary["errors"].append({"run_id": run_id, "error": type(exc).__name__})
    if summary["errors"]:
        summary["status"] = "partial"
    if not dry_run:
        _write_retention_cursor(
            container,
            db_name=db_name,
            cursor=next_cursor,
        )
    return summary


__all__ = ["purge_oracle_history", "select_oracle_runs_for_retention"]

"""Backfill the ``jobstateidx`` secondary index from the ``jobstate`` main table.

Responsibility: One-shot idempotent migration that reads every non-deleted row
    from the ``jobstate`` Table and upserts a corresponding entry into the
    ``jobstateidx`` secondary index without touching the main table.

Edit boundaries: This script is a standalone migration tool.  Do NOT import it
    from ``api/`` at runtime.  It reads the same environment variables that
    ``api/`` uses (``AZURE_STORAGE_ACCOUNT_NAME``, ``AZURE_CLIENT_ID`` for MI
    or the ``DefaultAzureCredential`` chain).

Key entry points:
    main() — CLI entry point; call with ``uv run python scripts/db/backfill_jobstate_idx.py``

Risky contracts:
    * Uses ``upsert_entity(mode=REPLACE)`` so re-runs are safe.
    * Reads the main table with ``results_per_page=1000`` pages; the SDK handles
      multi-page walks automatically.  The Azure Table Storage hard cap is 1000
      per page.
    * Does NOT delete orphaned index entries (e.g. from soft-deleted jobs) —
      run with ``--clean`` to remove index rows whose main-table PartitionKey is
      absent (or whose status is 'deleted').

Validation:
    uv run python scripts/db/backfill_jobstate_idx.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
LOGGER = logging.getLogger("backfill_jobstate_idx")

_JOBSTATE_TABLE = "jobstate"
_JOBSTATEIDX_TABLE = "jobstateidx"
_IDX_EPOCH_OFFSET = 10**13


def _idx_row_key(created_at: str, job_id: str) -> str:
    """Inverted-time RowKey for newest-first ordering within an index partition."""
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        epoch_ms = int(dt.timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        epoch_ms = 0
    inverted = max(0, _IDX_EPOCH_OFFSET - epoch_ms)
    return f"{inverted:013d}_{job_id}"


def _get_storage_endpoint() -> str:
    acct = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME") or os.environ.get(
        "STORAGE_ACCOUNT_NAME"
    )
    if not acct:
        raise SystemExit(
            "Set AZURE_STORAGE_ACCOUNT_NAME (or STORAGE_ACCOUNT_NAME) before running."
        )
    return f"https://{acct}.table.core.windows.net"


def _get_credential() -> Any:
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def _summary_fields(e: dict[str, Any]) -> set[str]:
    """Fields copied from the main-table entity into the index row."""
    # Keep in sync with _JOBSTATE_SUMMARY_SELECT_SET in repository.py.
    return {
        "created_at",
        "updated_at",
        "status",
        "phase",
        "error_code",
        "job_title",
        "program",
        "db",
        "query_label",
        "type",
        "subscription_id",
        "resource_group",
        "cluster_name",
        "storage_account",
        "owner_oid",
        "schema_version",
    }


def backfill(*, dry_run: bool = False, clean: bool = False) -> None:  # noqa: C901
    from azure.data.tables import TableClient, UpdateMode

    endpoint = _get_storage_endpoint()
    cred = _get_credential()

    main_client = TableClient(
        endpoint=endpoint, table_name=_JOBSTATE_TABLE, credential=cred
    )
    idx_client = TableClient(
        endpoint=endpoint, table_name=_JOBSTATEIDX_TABLE, credential=cred
    )

    LOGGER.info("Reading all rows from %s …", _JOBSTATE_TABLE)
    seen_job_ids: set[str] = set()
    upserted = skipped = errors = 0

    for e in main_client.query_entities(
        "RowKey eq 'current'", results_per_page=1000
    ):
        row = dict(e)
        job_id = row.get("PartitionKey", "")
        owner_oid = row.get("owner_oid") or ""
        created_at = row.get("created_at") or ""
        status = row.get("status") or ""

        if not job_id or not created_at:
            LOGGER.debug("Skipping row with missing job_id or created_at: %r", job_id)
            skipped += 1
            continue

        if status == "deleted":
            LOGGER.debug("Skipping deleted job %s", job_id)
            skipped += 1
            continue

        seen_job_ids.add(job_id)
        rk = _idx_row_key(created_at, job_id)
        summary = _summary_fields(row)
        entity: dict[str, Any] = {k: v for k, v in row.items() if k in summary}
        entity["PartitionKey"] = owner_oid
        entity["RowKey"] = rk
        entity["job_id"] = job_id

        if dry_run:
            LOGGER.info(
                "[dry-run] would upsert index row PK=%r RK=%r job_id=%s status=%s",
                owner_oid,
                rk,
                job_id,
                status,
            )
            upserted += 1
            continue

        try:
            idx_client.upsert_entity(entity, mode=UpdateMode.REPLACE)
            upserted += 1
        except Exception as exc:
            LOGGER.warning("Failed to upsert index for job_id=%s: %s", job_id, exc)
            errors += 1

    LOGGER.info(
        "Done. upserted=%d skipped=%d errors=%d",
        upserted,
        skipped,
        errors,
    )

    if clean and not dry_run:
        LOGGER.info("--clean: scanning %s for orphaned rows …", _JOBSTATEIDX_TABLE)
        removed = 0
        for idx_e in idx_client.list_entities(results_per_page=1000):
            idx_row = dict(idx_e)
            job_id = idx_row.get("job_id", "")
            if not job_id or job_id not in seen_job_ids:
                try:
                    idx_client.delete_entity(
                        partition_key=idx_row["PartitionKey"],
                        row_key=idx_row["RowKey"],
                    )
                    removed += 1
                    LOGGER.info("Removed orphaned index row job_id=%s", job_id)
                except Exception as exc:
                    LOGGER.warning(
                        "Failed to remove orphaned index row job_id=%s: %s",
                        job_id,
                        exc,
                    )
        LOGGER.info("Clean done. removed=%d", removed)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill jobstateidx secondary index from jobstate main table."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without making any changes.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Also remove index rows whose main-table job is absent or deleted.",
    )
    args = parser.parse_args()
    backfill(dry_run=args.dry_run, clean=args.clean)


if __name__ == "__main__":
    main()

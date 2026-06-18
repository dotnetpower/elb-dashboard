#!/usr/bin/env python3
"""Idempotent backfill for the ``jobstate`` time-ordered index (#50).

Responsibility: One-shot migration that populates the ``jobstateindex`` table
for every pre-existing non-deleted ``jobstate`` row, so the time-ordered index
read path (``JobStateRepository.list_owner_page``) returns the genuinely
most-recent N without scanning beyond the page. MUST be run (and verified to
complete) BEFORE flipping ``JOBSTATE_TIME_INDEX_ENABLED=true`` — an
un-backfilled index would under-report old jobs.
Edit boundaries: Read-only against ``jobstate``; upsert-only against
``jobstateindex``. Never deletes or mutates a ``jobstate`` row.
Key entry points: ``main``, ``backfill``.
Risky contracts: Idempotent — re-running upserts the SAME RowKey per job
(derived from the immutable ``owner_oid`` + ``created_at``), so a partial run
can be safely resumed by re-running from the start.
Validation: ``uv run python scripts/dev/backfill_jobstate_time_index.py --dry-run``
against an env with ``AZURE_TABLE_ENDPOINT`` set; the live run prints a per-batch
progress line and a final ``backfilled=<n>`` summary.
"""

from __future__ import annotations

import argparse
import sys

EXIT_OK = 0
EXIT_BAD_ENV = 2


def backfill(*, dry_run: bool = False, batch_log_every: int = 500) -> int:
    """Upsert an index row for every non-deleted ``jobstate`` row.

    Returns the number of index rows written (or that WOULD be written in
    ``--dry-run``). Streams the table so memory stays bounded regardless of
    history size.
    """
    from api.services.state.repository import get_state_repo
    from api.services.state.time_index import INDEX_TABLE_NAME, build_index_entity
    from azure.core.exceptions import ResourceNotFoundError

    repo = get_state_repo()
    written = 0
    scanned = 0

    if not dry_run:
        repo._ensure_table(INDEX_TABLE_NAME)

    # Read only the columns needed to build the index key; skip the large
    # payload. ``status ne 'deleted'`` mirrors the listing filter so tombstones
    # are not indexed.
    select = ["PartitionKey", "RowKey", "owner_oid", "created_at", "status"]
    with repo._state_client() as state_t:
        try:
            entities = state_t.query_entities(
                "RowKey eq 'current' and status ne 'deleted'",
                results_per_page=1000,
                select=select,
            )
        except ResourceNotFoundError:
            print("jobstate table does not exist yet; nothing to backfill")
            return EXIT_OK

        index_t = None if dry_run else repo._index_client()
        try:
            for entity in entities:
                scanned += 1
                job_id = str(entity.get("PartitionKey") or "")
                if not job_id:
                    continue
                index_entity = build_index_entity(
                    job_id=job_id,
                    owner_oid=entity.get("owner_oid"),
                    created_at=entity.get("created_at"),
                )
                if not dry_run and index_t is not None:
                    index_t.upsert_entity(index_entity)
                written += 1
                if written % batch_log_every == 0:
                    print(f"... progress scanned={scanned} backfilled={written}")
        finally:
            if index_t is not None:
                index_t.close()

    mode = "DRY-RUN " if dry_run else ""
    print(f"{mode}done scanned={scanned} backfilled={written}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows that would be indexed without writing anything.",
    )
    args = parser.parse_args(argv)

    import os

    if not os.environ.get("AZURE_TABLE_ENDPOINT"):
        print(
            "AZURE_TABLE_ENDPOINT is not set; point it at "
            "https://<account>.table.core.windows.net",
            file=sys.stderr,
        )
        return EXIT_BAD_ENV

    try:
        return backfill(dry_run=args.dry_run)
    except Exception as exc:
        print(f"backfill failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""One-shot recovery for an orphaned `prepare-db` daemon.

Use when the api sidecar that initiated `start_copy_from_url` for a BLAST DB
was restarted before its polling thread could promote the metadata. Azure
Storage continues the server-side copy regardless of the sidecar, so this
script only re-runs the **polling + promotion + sharding** tail of
`api.routes.storage.prepare_db._do_copies` against the existing in-flight
blobs. It does NOT re-initiate any copy.

Usage (local-debug, against the deployed Storage account):

    eval "$(azd env get-values | sed 's/^/export /')"
    uv run python scripts/dev/recover-prepare-db.py \\
        --account "$STORAGE_ACCOUNT_NAME" \\
        --db core_nt

Safety:
  * Idempotent. Re-running after completion is a no-op.
  * Will refuse to act if any blob's `copy.status` is `failed` / `aborted` —
    those need human investigation, not a silent retry.
  * Updates the same metadata.json blob via the existing ETag-aware mutator
    so concurrent writers (e.g. the operator clicking Prepare-DB) cannot
    clobber unrelated fields.

This is a recovery helper, not a production code path. Do not import from
production modules; do not schedule via Celery. It must stay a one-shot
script with a single clear entry point.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger("recover-prepare-db")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="workload Storage account name")
    parser.add_argument("--db", required=True, help="BLAST DB name (e.g. core_nt)")
    parser.add_argument(
        "--poll-interval", type=int, default=30, help="seconds between status polls"
    )
    parser.add_argument(
        "--max-wait-minutes",
        type=int,
        default=240,
        help="give up after this many minutes (default 4h)",
    )
    args = parser.parse_args()

    # Late imports so `--help` works without the api venv on sys.path.
    from api.routes.storage.prepare_db import _poll_copy_completion, _update_metadata
    from api.services import get_credential
    from api.services.db.sharding import (
        PRESET_SHARD_SETS,
        derive_volumes_from_keys,
        upload_shard_set,
    )
    from api.services.storage.data import _blob_service

    cred = get_credential()
    svc = _blob_service(cred, args.account)
    container = svc.get_container_client("blast-db")

    # 1. Inspect current state.
    meta_blob = container.get_blob_client(f"{args.db}-metadata.json")
    import json

    meta_raw = meta_blob.download_blob(max_concurrency=1).readall().decode("utf-8")
    metadata = json.loads(meta_raw)
    LOGGER.info(
        "metadata snapshot: phase=%s update_in_progress=%s success=%s pending=%s",
        (metadata.get("copy_status") or {}).get("phase"),
        metadata.get("update_in_progress"),
        (metadata.get("copy_status") or {}).get("success"),
        (metadata.get("copy_status") or {}).get("pending"),
    )

    if not metadata.get("update_in_progress"):
        LOGGER.info("metadata already shows update_in_progress=false; nothing to recover")
        return 0

    target_version = str(metadata.get("updating_to_source_version") or "")
    if not target_version:
        LOGGER.error("metadata missing updating_to_source_version; cannot recover safely")
        return 2

    # 2. Discover staged blobs from current container listing — these are the
    #    files prepare_db actually started copying.
    all_blobs = list(
        container.list_blobs(name_starts_with=f"{args.db}/", include=["copy"])
    )
    if not all_blobs:
        LOGGER.error(
            "no blobs under %s/ — cannot recover without the staged blob list",
            args.db,
        )
        return 2

    failed = [
        b for b in all_blobs if (b.copy or None) and b.copy.status in ("failed", "aborted")
    ]
    if failed:
        LOGGER.error(
            "%d blob(s) have copy.status in {failed, aborted}; refusing to "
            "auto-promote. Investigate and either re-initiate prepare-db or "
            "delete the failed blobs:",
            len(failed),
        )
        for b in failed[:10]:
            LOGGER.error("  failed/aborted: %s status=%s", b.name, b.copy.status)
        return 3

    staged_blob_names = [b.name for b in all_blobs]
    LOGGER.info(
        "staged: %d blobs (already success: %d, pending: %d)",
        len(staged_blob_names),
        sum(
            1
            for b in all_blobs
            if (b.copy or None) and b.copy.status == "success"
        ),
        sum(
            1
            for b in all_blobs
            if (b.copy or None) and b.copy.status == "pending"
        ),
    )

    # 3. Phase 2 — poll until all blobs reach terminal status. Mirror
    #    `_do_copies`' on_progress callback so the SPA sees live progress.
    def _record_progress(snapshot: dict[str, int]) -> None:
        def _mut(meta: dict[str, Any]) -> dict[str, Any]:
            meta["copy_status"] = {
                "phase": "copying",
                "total_files": len(staged_blob_names),
                **snapshot,
            }
            return meta

        try:
            _update_metadata(container, args.db, args.account, _mut)
        except Exception as exc:
            LOGGER.warning("metadata progress write skipped: %s", type(exc).__name__)

    LOGGER.info(
        "polling %d blobs (interval=%ds, max wait=%dm)",
        len(staged_blob_names),
        args.poll_interval,
        args.max_wait_minutes,
    )
    poll_summary = _poll_copy_completion(
        container,
        staged_blob_names,
        db_name=args.db,
        on_progress=_record_progress,
    )
    LOGGER.info(
        "poll done: success=%d failed=%d aborted=%d pending=%d timed_out=%s",
        poll_summary["success"],
        poll_summary["failed"],
        poll_summary["aborted"],
        poll_summary["pending"],
        poll_summary["timed_out"],
    )

    all_succeeded = (
        poll_summary["failed"] == 0
        and poll_summary["aborted"] == 0
        and not poll_summary["timed_out"]
        and poll_summary["success"] >= len(staged_blob_names)
    )
    if not all_succeeded:
        LOGGER.error("not all copies succeeded; leaving metadata as `partial`")

        def _partial(meta: dict[str, Any]) -> dict[str, Any]:
            meta["update_in_progress"] = False
            meta["update_error"] = (
                f"recover-prepare-db: {poll_summary['failed']} failed, "
                f"{poll_summary['aborted']} aborted, {poll_summary['pending']} pending"
            )
            meta["update_failed_at"] = datetime.now(UTC).isoformat()
            meta["failed_files"] = poll_summary["failed_files"]
            meta["copy_status"] = {
                "phase": "partial",
                "total_files": len(staged_blob_names),
                "success": poll_summary["success"],
                "failed": poll_summary["failed"],
                "aborted": poll_summary["aborted"],
                "pending": poll_summary["pending"],
                "timed_out": poll_summary["timed_out"],
            }
            return meta

        _update_metadata(container, args.db, args.account, _partial)
        return 4

    # 4. Phase 3 — auto-shard + promote. Mirror `_do_copies` exactly.
    LOGGER.info("all copies succeeded; building shard sets…")
    shard_sets_created: list[int] = []
    try:
        volumes = derive_volumes_from_keys(args.db, staged_blob_names)
        for n in PRESET_SHARD_SETS:
            if n > len(volumes):
                continue
            try:
                upload_shard_set(cred, args.account, args.db, n, volumes)
                shard_sets_created.append(n)
                LOGGER.info("  shard set N=%d uploaded", n)
            except Exception as exc:
                LOGGER.warning("  shard set N=%d failed: %s", n, type(exc).__name__)
    except LookupError:
        LOGGER.info("auto-shard skipped: no volumes detected")
    except Exception as exc:
        LOGGER.warning("auto-shard failed: %s", type(exc).__name__)

    new_signature_etag: str | None = None
    new_composite_signature: str | None = None
    try:
        from api.services.ncbi_catalogue import database_update_signature

        sig = database_update_signature(args.db)
        new_signature_etag = sig.get("signature_etag")
        new_composite_signature = sig.get("composite_signature")
    except Exception as exc:
        LOGGER.debug("signature lookup skipped: %s", type(exc).__name__)

    previous_source_version = str(metadata.get("source_version") or "")

    def _promote(meta: dict[str, Any]) -> dict[str, Any]:
        meta["db_name"] = args.db
        meta["source_version"] = target_version
        if new_signature_etag:
            meta["signature_etag"] = new_signature_etag
        if new_composite_signature:
            meta["composite_signature"] = new_composite_signature
        meta["downloaded_at"] = datetime.now(UTC).isoformat()
        meta["file_count"] = poll_summary["success"]
        meta["update_in_progress"] = False
        meta["update_completed_at"] = datetime.now(UTC).isoformat()
        meta.pop("updating_to_source_version", None)
        meta.pop("update_error", None)
        meta.pop("update_failed_at", None)
        meta.pop("failed_files", None)
        meta["copy_status"] = {
            "phase": "completed",
            "total_files": len(staged_blob_names),
            "success": poll_summary["success"],
            "failed": 0,
            "aborted": 0,
            "pending": 0,
            "timed_out": False,
        }
        if previous_source_version and previous_source_version != target_version:
            meta["updated_from_source_version"] = previous_source_version
        if shard_sets_created:
            meta["sharded"] = True
            meta["shard_sets"] = shard_sets_created
            meta["shard_source_version"] = target_version
            meta["sharded_at"] = datetime.now(UTC).isoformat()
            meta.pop("sharding_error", None)
        else:
            meta["sharded"] = False
            meta["shard_sets"] = []
            meta["shard_source_version"] = None
            meta["sharding_error"] = "preset shard layout generation failed"
        meta["sharding_in_progress"] = False
        if isinstance(meta.get("db_order_oracle"), dict):
            oracle = dict(meta["db_order_oracle"])
            if (
                oracle.get("source_version")
                and oracle.get("source_version") != target_version
            ):
                oracle["status"] = "stale"
            meta["db_order_oracle"] = oracle
        return meta

    _update_metadata(container, args.db, args.account, _promote)
    LOGGER.info(
        "promoted: source_version=%s file_count=%d shard_sets=%s",
        target_version,
        poll_summary["success"],
        shard_sets_created,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""/api/blast database sharding route.

Async sharding trigger for an already-downloaded BLAST database. Split out of
`api/routes/blast/databases.py` so the catalogue, sharding, and order-oracle
concerns each own a single-responsibility route module under the shared
`blast_router`.

Responsibility: Accept `POST /databases/{db}/shard`, validate input, serialise
    concurrent triggers per `(account, db)`, write the in-progress marker, and
    spawn the background `ensure_shard_sets` daemon.
Edit boundaries: HTTP validation + dispatch only; the shard math lives in
    `api/services/db/sharding.py` and the ETag-aware metadata write in
    `api/services/storage/prepare_db_metadata.py`.
Key entry points: `blast_database_shard`.
Risky contracts: Every non-health `/api/*` route must enforce `require_caller`.
    The shared per-`(account, db)` lock and ETag-owned sharding marker MUST stay
    so prepare-db, warmup, and manual shard producers cannot overlap.
Validation: `uv run pytest -q api/tests/test_blast_database_shard_route.py
    api/tests/test_db_sharding.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _maybe_open_local_storage_access,
)
from api.routes.blast.databases import (
    _DB_NAME_RE,
    _RESOURCE_GROUP_RE,
    _STORAGE_ACCOUNT_RE,
    _SUBSCRIPTION_RE,
)
from api.services.sanitise import redact_oid

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.post("/databases/{db_name}/shard")
def blast_database_shard(
    db_name: str,
    body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Run prepare-db's sharding step against an already-downloaded DB.

    **Async** — returns 202 immediately and runs ``ensure_shard_sets`` in
    a daemon thread (mirrors ``/api/storage/prepare-db``). Sharding for
    large DBs like ``core_nt`` does ~150+ small SDK round-trips and
    cannot complete inside an HTTP request window. Progress is published
    by writing ``sharding_in_progress`` / ``sharding_started_at`` /
    ``sharding_error`` into ``{db_name}-metadata.json`` so the SPA's
    ``GET /api/blast/databases`` poll renders the in-flight state
    (and survives a page reload).

    Hardening:
            * The shared per-``(account, db)`` lock prevents concurrent local
                prepare/shard workers from thrashing the metadata blob.
            * The ETag-owned marker rejects cross-process prepare/shard overlap and
                permits takeover only after the shared stale threshold.
      * All error strings are passed through ``sanitise()`` before
        landing in the metadata blob or the response.
    """
    import threading
    import uuid
    from datetime import UTC, datetime

    from api.services import get_credential
    from api.services.db.sharding import (
        DEFAULT_CONTAINER,
    )
    from api.services.sanitise import sanitise
    from api.services.storage.data import _blob_service

    sub = body.get("subscription_id", "")
    storage_rg = body.get("resource_group", "")
    account_name = body.get("account_name", "")
    if not all([sub, storage_rg, account_name]):
        raise HTTPException(
            400,
            "subscription_id, resource_group, account_name required in body",
        )
    # Mirror the validation in /api/storage/prepare-db. Keep it tight —
    # `db_name` flows straight to a blob path. Patterns are module-level
    # (imported from databases.py) so they are compiled once per process.
    if not _DB_NAME_RE.match(db_name):
        raise HTTPException(400, "invalid db_name")
    if not _SUBSCRIPTION_RE.match(sub):
        raise HTTPException(400, "invalid subscription_id")
    if not _RESOURCE_GROUP_RE.match(storage_rg):
        raise HTTPException(400, "invalid resource_group")
    if not _STORAGE_ACCOUNT_RE.match(account_name):
        raise HTTPException(400, "invalid account_name")

    cred = get_credential()
    # Local-debug auto-open mirrors /api/storage/prepare-db so this call
    # also works from a developer laptop. No-op inside the Container App.
    _maybe_open_local_storage_access(
        cred,
        sub,
        storage_rg,
        account_name,
        context="blast_database_shard",
    )

    from api.services.storage.prepare_db_locks import prepare_db_lock

    lock = prepare_db_lock(account_name, db_name)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "another database operation is in progress for this DB")

    svc = _blob_service(cred, account_name)
    cc = svc.get_container_client(DEFAULT_CONTAINER)
    started_at = datetime.now(UTC).isoformat()
    operation_id = uuid.uuid4().hex
    # ETag-aware metadata write. Concurrent prepare-db / warmup writers can
    # not race the same metadata blob anymore — `_update_metadata` retries on
    # 412 instead of blindly overwriting.
    try:
        from api.services.storage.prepare_db_metadata import (
            DatabaseOperationInProgressError,
            is_stale_sharding_marker,
        )
        from api.services.storage.prepare_db_metadata import (
            update_metadata as _update_md,
        )

        def _pre_mutator(meta: dict[str, Any]) -> dict[str, Any]:
            if meta.get("update_in_progress"):
                raise DatabaseOperationInProgressError("prepare-db is in progress for this DB")
            if meta.get("sharding_in_progress") and not is_stale_sharding_marker(meta):
                raise DatabaseOperationInProgressError(
                    "sharding is already in progress for this DB"
                )
            meta["db_name"] = db_name
            meta["sharding_in_progress"] = True
            meta["sharding_started_at"] = started_at
            meta["sharding_operation_id"] = operation_id
            meta.pop("sharding_error", None)
            return meta

        started_metadata = _update_md(cc, db_name, account_name, _pre_mutator)
        source_version = str(started_metadata.get("source_version") or "")
    except DatabaseOperationInProgressError as exc:
        lock.release()
        raise HTTPException(409, sanitise(str(exc))[:200]) from exc
    except Exception as exc:
        lock.release()
        LOGGER.warning(
            "blast_database_shard: pre-state write failed db=%s: %s",
            db_name,
            type(exc).__name__,
        )
        raise HTTPException(502, f"metadata pre-write failed: {type(exc).__name__}") from exc

    # Audit — records the sharding action against the caller so /api/audit/log
    # surfaces it alongside BLAST / warmup operations.
    try:
        from api.services.db.ops_audit import record_db_op

        record_db_op(
            op="shard",
            caller=caller,
            account_name=account_name,
            db_name=db_name,
        )
    except Exception as exc:
        LOGGER.debug("shard audit record skipped: %s", type(exc).__name__)

    LOGGER.info(
        "blast_database_shard accepted oid=%s db=%s account=%s",
        redact_oid(caller.object_id),
        db_name,
        account_name,
    )

    def _do_shard() -> None:
        """Background worker — owns the lock for the lifetime of the call."""
        from api.services import get_credential as _get_cred
        from api.services.db.sharding import require_complete_shard_summary
        from api.services.storage.prepare_db_metadata import (
            invalidate_shard_publication,
        )
        from api.services.storage.prepare_db_metadata import (
            update_metadata as _update_md,
        )

        try:
            local_cred = _get_cred()
            # Full consistency reconcile: prune ghost volumes left from a
            # previous (larger) NCBI generation, then regenerate the shard alias
            # layout for the TRUE volume set. This makes the "shard" action also
            # the manual repair for a drifted DB (the 3-way generation mismatch
            # that fails BLAST with "vol does not match lmdb vol"). A healthy DB
            # has no ghosts, so prune is a no-op and only the shard layout is
            # (re)built — identical to the old ensure_shard_sets behaviour.
            from api.services.db.consistency import reconcile_db_consistency

            recon = reconcile_db_consistency(local_cred, account_name, db_name, force_reshard=True)
            if not recon.get("resharded"):
                raise RuntimeError(
                    "shard consistency reconcile did not complete "
                    f"(status={recon.get('status')}, error={recon.get('shard_error')})"
                )
            summary = recon.get("shard") or {}
            complete_sets = require_complete_shard_summary(summary)
            svc2 = _blob_service(local_cred, account_name)
            cc2 = svc2.get_container_client(DEFAULT_CONTAINER)

            def _ok_mut(meta: dict[str, Any]) -> dict[str, Any]:
                if meta.get("sharding_operation_id") != operation_id:
                    raise RuntimeError("shard marker ownership changed")
                if meta.get("update_in_progress"):
                    raise RuntimeError("prepare-db started during sharding")
                if str(meta.get("source_version") or "") != source_version:
                    raise RuntimeError("database source version changed during sharding")
                meta["sharding_in_progress"] = False
                meta.pop("sharding_operation_id", None)
                meta.pop("sharding_error", None)
                meta["sharded"] = True
                meta["shard_sets"] = complete_sets
                meta["shard_layout_schema"] = int(summary["layout_schema"])
                if source_version:
                    meta["shard_source_version"] = source_version
                meta["sharded_at"] = datetime.now(UTC).isoformat()
                if summary.get("total_bytes"):
                    meta["total_bytes"] = summary["total_bytes"]
                for key in (
                    "total_letters",
                    "total_sequences",
                    "bytes_to_cache",
                    "bytes_total",
                ):
                    if summary.get(key):
                        meta[key] = summary[key]
                return meta

            _update_md(cc2, db_name, account_name, _ok_mut)
            # Sharding rewrote {db}-metadata.json (sharded / shard_sets /
            # shard_source_version). Invalidate the display + catalogue
            # listing caches so New Search reflects the new chip state on the
            # next read instead of waiting out the TTL. Best-effort: a failed
            # invalidate must not fail the shard.
            try:
                from api.services.blast.db_metadata import (
                    notify_blast_db_metadata_changed,
                )

                notify_blast_db_metadata_changed(account_name, db_name)
            except Exception as exc_inv:
                LOGGER.debug(
                    "blast_database_shard cache invalidate skipped db=%s: %s",
                    db_name,
                    type(exc_inv).__name__,
                )
            LOGGER.info(
                "blast_database_shard daemon ok db=%s shard_sets=%s",
                db_name,
                summary.get("shard_sets"),
            )
        except Exception as exc:
            LOGGER.warning(
                "blast_database_shard daemon failed db=%s: %s",
                db_name,
                type(exc).__name__,
            )
            err_msg = sanitise(f"{type(exc).__name__}: {exc}")[:300]
            try:
                local_cred = _get_cred()
                svc2 = _blob_service(local_cred, account_name)
                cc2 = svc2.get_container_client(DEFAULT_CONTAINER)

                def _err_mut(meta: dict[str, Any]) -> dict[str, Any]:
                    if meta.get("sharding_operation_id") != operation_id:
                        return meta
                    meta["sharding_in_progress"] = False
                    meta.pop("sharding_operation_id", None)
                    return invalidate_shard_publication(meta, error=err_msg)

                _update_md(cc2, db_name, account_name, _err_mut)
            except Exception as inner:
                LOGGER.warning(
                    "blast_database_shard error-state write failed db=%s: %s",
                    db_name,
                    type(inner).__name__,
                )
        finally:
            lock.release()

    shard_thread = threading.Thread(
        target=_do_shard,
        daemon=True,
        name=f"shard-{db_name}",
    )
    try:
        shard_thread.start()
    except Exception as exc:

        def _thread_start_failed(meta: dict[str, Any]) -> dict[str, Any]:
            if meta.get("sharding_operation_id") != operation_id:
                raise RuntimeError("shard marker ownership changed")
            meta["sharding_in_progress"] = False
            meta.pop("sharding_operation_id", None)
            meta["sharding_error"] = "shard background worker failed to start"
            return meta

        try:
            _update_md(cc, db_name, account_name, _thread_start_failed)
        except Exception as marker_exc:
            LOGGER.error(
                "blast_database_shard thread-start rollback failed db=%s: %s",
                db_name,
                type(marker_exc).__name__,
            )
        finally:
            lock.release()
        raise HTTPException(502, "shard background worker failed to start") from exc

    return {
        "accepted": True,
        "db_name": db_name,
        "sharding_started_at": started_at,
        "output": (
            "Sharding started in background. Poll /api/blast/databases for "
            "progress (look at sharding_in_progress / sharded / shard_sets)."
        ),
    }

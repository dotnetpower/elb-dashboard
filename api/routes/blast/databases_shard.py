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
    The per-`(account, db)` lock + `_SHARD_STALE_SECONDS` stale recovery MUST
    stay so two daemons never race the metadata blob.
Validation: `uv run pytest -q api/tests/test_route_contracts.py
    api/tests/test_blast_results_routes.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _SHARD_LOCK_REGISTRY,
    _SHARD_LOCK_REGISTRY_GUARD,
    _SHARD_STALE_SECONDS,
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
      * Per-``(account, db)`` lock prevents concurrent daemons from
        thrashing the metadata blob.
      * If a previous daemon's ``sharding_in_progress`` flag is older
        than ``_SHARD_STALE_SECONDS`` we treat it as crashed and allow
        re-trigger.
      * All error strings are passed through ``sanitise()`` before
        landing in the metadata blob or the response.
    """
    import json
    import threading
    from datetime import UTC, datetime

    from azure.core.exceptions import ResourceNotFoundError

    from api.services import get_credential
    from api.services.db.sharding import (
        DEFAULT_CONTAINER,
        ensure_shard_sets,
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

    # Per-(account, db) lock — prevents the user double-clicking a chip
    # from spawning two daemons that race the metadata write. Lock is
    # acquired non-blocking; if it's already held we return 409 so the
    # SPA shows "already running" instead of starting a second writer.
    lock_key = f"{account_name.lower()}|{db_name}"
    with _SHARD_LOCK_REGISTRY_GUARD:
        lock = _SHARD_LOCK_REGISTRY.setdefault(lock_key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "sharding already in progress for this DB")

    # Read the current metadata so we can preserve unrelated fields
    # (source_version, downloaded_at, …) and detect a stale in-progress
    # marker from a crashed previous daemon.
    svc = _blob_service(cred, account_name)
    cc = svc.get_container_client(DEFAULT_CONTAINER)
    bc = cc.get_blob_client(f"{db_name}-metadata.json")
    existing: dict[str, Any] = {}
    try:
        from api.services.storage.data import read_metadata_blob_text

        existing = json.loads(
            read_metadata_blob_text(bc, max_bytes=4 * 1024 * 1024, label="db-metadata.json")
        )
    except ResourceNotFoundError:
        existing = {"db_name": db_name}
    except Exception:
        existing = {"db_name": db_name}

    # Stale-flag recovery — if the previous daemon crashed the metadata
    # could be left with sharding_in_progress=true forever. Treat
    # markers older than _SHARD_STALE_SECONDS as crashed.
    if existing.get("sharding_in_progress"):
        started = existing.get("sharding_started_at") or ""
        try:
            started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            age = (datetime.now(UTC) - started_dt).total_seconds()
        except Exception:
            age = float("inf")  # parse failure → treat as stale
        if age < _SHARD_STALE_SECONDS:
            lock.release()
            raise HTTPException(409, "sharding already in progress for this DB")
        LOGGER.info(
            "blast_database_shard: clearing stale in-progress flag for %s (age=%.0fs)",
            db_name,
            age,
        )

    started_at = datetime.now(UTC).isoformat()
    # ETag-aware metadata write. Concurrent prepare-db / warmup writers can
    # not race the same metadata blob anymore — `_update_metadata` retries on
    # 412 instead of blindly overwriting.
    try:
        from api.services.storage.prepare_db_metadata import (
            update_metadata as _update_md,
        )

        def _pre_mutator(meta: dict[str, Any]) -> dict[str, Any]:
            meta["db_name"] = db_name
            meta["sharding_in_progress"] = True
            meta["sharding_started_at"] = started_at
            meta.pop("sharding_error", None)
            return meta

        _update_md(cc, db_name, account_name, _pre_mutator)
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
        from api.services.storage.prepare_db_metadata import (
            update_metadata as _update_md,
        )

        try:
            local_cred = _get_cred()
            summary = ensure_shard_sets(local_cred, account_name, db_name)
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
                    meta["sharding_in_progress"] = False
                    meta["sharding_error"] = err_msg
                    return meta

                _update_md(cc2, db_name, account_name, _err_mut)
            except Exception as inner:
                LOGGER.warning(
                    "blast_database_shard error-state write failed db=%s: %s",
                    db_name,
                    type(inner).__name__,
                )
            finally:
                lock.release()
            return

        # Success — merge the summary into metadata via ETag-aware writer
        # so a concurrent prepare-db / warmup writer cannot clobber the
        # shard fields.
        try:
            local_cred = _get_cred()
            svc2 = _blob_service(local_cred, account_name)
            cc2 = svc2.get_container_client(DEFAULT_CONTAINER)

            def _ok_mut(meta: dict[str, Any]) -> dict[str, Any]:
                meta["sharding_in_progress"] = False
                meta.pop("sharding_error", None)
                meta["sharded"] = bool(summary.get("shard_sets"))
                meta["shard_sets"] = summary.get("shard_sets", [])
                if meta.get("source_version"):
                    meta["shard_source_version"] = meta.get("source_version")
                meta["sharded_at"] = datetime.now(UTC).isoformat()
                if summary.get("total_bytes"):
                    meta.setdefault("total_bytes", summary["total_bytes"])
                for key in (
                    "total_letters",
                    "total_sequences",
                    "bytes_to_cache",
                    "bytes_total",
                ):
                    if summary.get(key):
                        meta.setdefault(key, summary[key])
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
                "blast_database_shard final-state write failed db=%s: %s",
                db_name,
                type(exc).__name__,
            )
        finally:
            lock.release()

    threading.Thread(
        target=_do_shard,
        daemon=True,
        name=f"shard-{db_name}",
    ).start()

    return {
        "accepted": True,
        "db_name": db_name,
        "sharding_started_at": started_at,
        "output": (
            "Sharding started in background. Poll /api/blast/databases for "
            "progress (look at sharding_in_progress / sharded / shard_sets)."
        ),
    }

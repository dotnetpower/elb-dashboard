"""Storage prepare-db route for NCBI BLAST database copies.

Responsibility: Storage prepare-db route for NCBI BLAST database copies
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_read_db_metadata`, `_write_db_metadata`, `prepare_db`
Risky contracts: Never issue browser SAS URLs; local public Storage access remains debug-only
and IP-allowlisted.
Validation: `uv run pytest -q api/tests/test_storage_data.py
api/tests/test_storage_public_access.py`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from threading import Thread
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.routes.storage.common import (
    _NCBI_S3_BASE,
    _RE_DB_NAME,
    _RE_RG,
    _RE_STORAGE_ACCOUNT,
    _RE_SUB,
    _check,
    _list_keys,
    _resolve_latest_dir,
)
from api.services import get_credential
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _read_db_metadata(container: Any, db_name: str) -> dict[str, Any]:
    metadata_blob = container.get_blob_client(f"{db_name}-metadata.json")
    try:
        payload = metadata_blob.download_blob().readall().decode("utf-8")
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        LOGGER.debug("DB metadata read skipped for %s: %s", db_name, type(exc).__name__)
    return {"db_name": db_name}


def _write_db_metadata(
    container: Any,
    db_name: str,
    payload: dict[str, Any],
    *,
    account_name: str,
) -> None:
    metadata_blob = container.get_blob_client(f"{db_name}-metadata.json")
    metadata_blob.upload_blob(
        json.dumps(payload, sort_keys=True).encode("utf-8"),
        overwrite=True,
    )
    # Drop the merged display-metadata cache so /api/blast/jobs/{id} picks up
    # the new title / sequence count / sharded badge on the next read instead
    # of waiting up to ``BLAST_DB_METADATA_CACHE_TTL`` (default 24 h). Invoked
    # for every prepare-db write — start, success, failure — so the UI never
    # shows stale state after an admin action. Best-effort: cache invalidation
    # must not fail the metadata write. ``notify_*`` also publishes to the
    # Redis pub/sub channel so the worker / beat sidecars (and any peer api
    # replica) drop their copies too.
    try:
        from api.services.blast_db_metadata import notify_blast_db_metadata_changed

        notify_blast_db_metadata_changed(account_name, db_name)
    except Exception as exc:
        LOGGER.debug(
            "db metadata cache invalidate skipped db=%s: %s",
            db_name,
            type(exc).__name__,
        )


@router.post("/prepare-db")
def prepare_db(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Begin a server-side copy of a BLAST DB from NCBI to the workload
    Storage account's ``blast-db`` container.

    Returns immediately. Per-file ``start_copy_from_url`` calls run in a
    daemon thread; the SPA observes progress by polling
    ``GET /api/blast/databases``.
    """
    sub = body.get("subscription_id", "")
    storage_rg = body.get("storage_resource_group", "")
    account_name = body.get("account_name", "")
    db_name = body.get("db_name", "")
    if not all([sub, storage_rg, account_name, db_name]):
        raise HTTPException(
            400,
            "subscription_id, storage_resource_group, account_name, db_name required",
        )
    _check(sub, _RE_SUB, "subscription_id")
    _check(storage_rg, _RE_RG, "storage_resource_group")
    _check(account_name, _RE_STORAGE_ACCOUNT, "account_name")
    _check(db_name, _RE_DB_NAME, "db_name")

    cred = get_credential()

    # Local-debug only: when LOCAL_DEBUG_AUTO_OPEN_STORAGE=true is set on a
    # developer laptop (NOT in a Container App), open the workload Storage
    # account's public network surface to this caller's IP so the server-side
    # copy below can actually reach the data plane. In production the api
    # sidecar already reaches Storage via the private endpoint and this is a
    # no-op. See api/services/storage_public_access.py and project policy §9.
    from api.services.storage_public_access import ensure_local_storage_access

    access = ensure_local_storage_access(cred, sub, storage_rg, account_name)
    if access.get("action") == "failed":
        LOGGER.warning(
            "prepare_db: local-debug auto-open failed for %s: %s",
            account_name,
            access.get("error"),
        )

    try:
        latest_dir = _resolve_latest_dir()
    except Exception as exc:
        LOGGER.warning("NCBI latest-dir lookup failed: %s", type(exc).__name__)
        raise HTTPException(502, f"could not contact NCBI: {sanitise(str(exc))[:200]}") from exc

    try:
        all_keys = _list_keys(latest_dir, db_name)
    except Exception as exc:
        LOGGER.warning("NCBI key list failed for %s: %s", db_name, type(exc).__name__)
        raise HTTPException(
            502, f"could not list NCBI database keys: {sanitise(str(exc))[:200]}"
        ) from exc

    if not all_keys:
        raise HTTPException(
            404,
            f"No files found for database '{db_name}' in NCBI S3 (dir: {latest_dir})",
        )

    # Build the destination container client. The api sidecar reaches the
    # storage account over the private endpoint via the shared MI; no SAS
    # is involved, no public network toggle is performed.
    from azure.storage.blob import BlobServiceClient

    from api.services.storage_endpoint import blob_account_url

    blob_svc = BlobServiceClient(
        account_url=blob_account_url(account_name),
        credential=cred,
    )
    container = blob_svc.get_container_client("blast-db")

    previous_metadata = _read_db_metadata(container, db_name)
    previous_source_version = str(previous_metadata.get("source_version") or "")
    try:
        start_metadata = dict(previous_metadata)
        start_metadata["db_name"] = db_name
        start_metadata["update_in_progress"] = True
        start_metadata["update_started_at"] = datetime.now(UTC).isoformat()
        start_metadata["updating_to_source_version"] = latest_dir
        start_metadata.pop("update_error", None)
        start_metadata.pop("update_failed_at", None)
        if previous_source_version and previous_source_version != latest_dir:
            start_metadata["previous_source_version"] = previous_source_version
        _write_db_metadata(container, db_name, start_metadata, account_name=account_name)
    except Exception as exc:
        LOGGER.warning(
            "prepare_db update-start metadata write failed for %s: %s",
            db_name,
            sanitise(str(exc))[:200],
        )

    def _do_copies() -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _copy_one(key: str) -> tuple[str, str]:
            source_url = f"{_NCBI_S3_BASE}/{key}"
            # Layout MUST match `elastic-blast` upstream
            # `util.py:get_blastdb_info`: it calls `os.path.dirname(db_url)`
            # and runs `azcopy list`, then filters lines containing
            # `os.path.basename(db)`. That requires files to live in a
            # subfolder named after the DB (`blast-db/<db>/<files>`). A
            # flat layout makes `azcopy list` of the parent return wrong
            # results and elastic-blast reports
            # "BLAST database … was not found".
            file_basename = key.split("/")[-1]
            blob_name = f"{db_name}/{file_basename}"
            try:
                container.get_blob_client(blob_name).start_copy_from_url(source_url)
                return (blob_name, "started")
            except Exception as e:
                if "PendingCopyOperation" in str(e):
                    return (blob_name, "skipped")
                LOGGER.warning("Copy failed for %s: %s", blob_name, sanitise(str(e))[:200])
                return (blob_name, "error")

        started = skipped = errors = 0
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = [ex.submit(_copy_one, k) for k in all_keys]
            for f in as_completed(futures):
                _, status = f.result()
                if status == "started":
                    started += 1
                elif status == "skipped":
                    skipped += 1
                else:
                    errors += 1

        LOGGER.info(
            "DB prepare done for %s: %d started, %d skipped, %d errors",
            db_name,
            started,
            skipped,
            errors,
        )

        successful_files = started + skipped
        if errors > 0 or successful_files <= 0:
            try:
                failed_metadata = _read_db_metadata(container, db_name)
                failed_metadata["db_name"] = db_name
                failed_metadata["update_in_progress"] = False
                failed_metadata["updating_to_source_version"] = latest_dir
                failed_metadata["update_error"] = (
                    f"copy initiation failed for {errors} of {len(all_keys)} files"
                )
                failed_metadata["update_failed_at"] = datetime.now(UTC).isoformat()
                _write_db_metadata(container, db_name, failed_metadata, account_name=account_name)
            except Exception as exc:
                LOGGER.warning(
                    "prepare_db update-failure metadata write failed for %s: %s",
                    db_name,
                    sanitise(str(exc))[:200],
                )
            return

        # Auto-shard step: as soon as the NCBI key enumeration is in hand,
        # we know every volume name and can publish the manifest+.nal alias
        # files for every preset shard count. The blobs they reference do
        # not have to exist yet — the AKS init script will download them
        # lazily at job runtime. ~50 KB total per DB across all presets.
        from api.services.db_sharding import (
            PRESET_SHARD_SETS,
            derive_volumes_from_keys,
            upload_shard_set,
        )

        shard_sets_created: list[int] = []
        try:
            volumes = derive_volumes_from_keys(db_name, all_keys)
            for n in PRESET_SHARD_SETS:
                if n > len(volumes):
                    continue  # small DB, fewer volumes than this preset
                try:
                    upload_shard_set(
                        cred,
                        account_name,
                        db_name,
                        n,
                        volumes,
                    )
                    shard_sets_created.append(n)
                except Exception as exc:
                    LOGGER.warning(
                        "shard set N=%d failed for %s: %s",
                        n,
                        db_name,
                        sanitise(str(exc))[:200],
                    )
        except LookupError:
            # No volumes detected (e.g. key list was empty or unfamiliar
            # extension layout). Leave sharded=False.
            LOGGER.info("auto-shard skipped for %s: no volumes detected", db_name)
        except Exception as exc:
            LOGGER.warning(
                "auto-shard failed for %s: %s",
                db_name,
                sanitise(str(exc))[:200],
            )

        # Drop a metadata blob alongside the DB so the dashboard can show
        # source_version / downloaded_at / sharding state without
        # contacting NCBI again.
        try:
            final_metadata = _read_db_metadata(container, db_name)
            final_metadata["db_name"] = db_name
            final_metadata["source_version"] = latest_dir
            final_metadata["downloaded_at"] = datetime.now(UTC).isoformat()
            final_metadata["file_count"] = successful_files
            final_metadata["update_in_progress"] = False
            final_metadata["update_completed_at"] = datetime.now(UTC).isoformat()
            final_metadata.pop("updating_to_source_version", None)
            final_metadata.pop("update_error", None)
            final_metadata.pop("update_failed_at", None)
            if previous_source_version and previous_source_version != latest_dir:
                final_metadata["updated_from_source_version"] = previous_source_version
            if shard_sets_created:
                final_metadata["sharded"] = True
                final_metadata["shard_sets"] = shard_sets_created
                final_metadata["shard_source_version"] = latest_dir
                final_metadata["sharded_at"] = datetime.now(UTC).isoformat()
                final_metadata.pop("sharding_error", None)
            else:
                final_metadata["sharded"] = False
                final_metadata["shard_sets"] = []
                final_metadata["shard_source_version"] = None
                final_metadata["sharding_error"] = "preset shard layout generation failed"
            final_metadata["sharding_in_progress"] = False
            if isinstance(final_metadata.get("db_order_oracle"), dict):
                oracle = dict(final_metadata["db_order_oracle"])
                if oracle.get("source_version") and oracle.get("source_version") != latest_dir:
                    oracle["status"] = "stale"
                final_metadata["db_order_oracle"] = oracle
            _write_db_metadata(container, db_name, final_metadata, account_name=account_name)
        except Exception as e:
            LOGGER.warning("metadata write failed for %s: %s", db_name, sanitise(str(e))[:200])

    Thread(target=_do_copies, daemon=True, name=f"prepare-db-{db_name}").start()

    LOGGER.info(
        "prepare_db started oid=%s db=%s files=%d source=%s access=%s",
        caller.object_id,
        db_name,
        len(all_keys),
        latest_dir,
        access.get("action"),
    )
    response: dict[str, Any] = {
        "ok": True,
        "db_name": db_name,
        # Async — actual progress is observed by polling /api/blast/databases.
        "files_copied": 0,
        "files_total": len(all_keys),
        "source_version": latest_dir,
        "output": (
            f"Started background copy of {len(all_keys)} files from {latest_dir}. "
            "Poll /api/blast/databases for progress."
        ),
        "async": True,
    }
    if access.get("action") in ("opened", "ip_added"):
        response["local_debug_storage_opened"] = {
            "ip": access.get("ip"),
            "previous_public": access.get("previous_public"),
            "off_hint": access.get("off_hint"),
        }
        response["output"] += (
            f" Local-debug: temporarily opened Storage to {access.get('ip')} "
            f"(was publicNetworkAccess={access.get('previous_public')}). Run "
            f"`{access.get('off_hint')}` when done."
        )
    return response


# ---------------------------------------------------------------------------
# Local-debug helpers — surface the Storage public-access toggle in the UI
# but only when the api process is NOT running inside a Container App. The
# Container-App guard lives in api.services.storage_public_access and is the
# load-bearing safety check; do not bypass it.
# ---------------------------------------------------------------------------

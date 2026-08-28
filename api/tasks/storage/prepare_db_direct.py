"""NCBI Direct generation staging and promotion task.

Responsibility: Submit and monitor the Direct Indexed Job, verify every archive
    completion marker and staged file, build generation-scoped shard layouts,
    and atomically promote the active DB pointer.
Edit boundaries: Long-running orchestration and metadata checkpoints only;
    discovery belongs to `api.services.ncbi_direct`, pure manifests to the K8s
    builder, and HTTP validation/dispatch to the storage service.
Key entry points: `prepare_db_via_ncbi_direct` Celery task.
Risky contracts: Every side-effect boundary revalidates `prepare_operation_id`;
    promotion requires the pinned transfer hash, all archive markers, all staged
    files, and complete shard layouts. Failure never changes `active_prefix`.
Validation: `uv run pytest -q api/tests/test_prepare_db_direct_task.py`.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from celery import shared_task

import api.tasks.storage as _facade
from api.services.db.generations import generation_db_prefix
from api.services.db.sharding import ensure_shard_sets, require_complete_shard_summary
from api.services.feature_events import TERMINAL_STATUSES, record_feature_event
from api.services.k8s.prepare_db_direct_jobs import (
    build_direct_job_manifest,
    build_direct_scripts_configmap,
    direct_prepare_job_name,
)
from api.services.k8s.prepare_db_jobs import (
    delete_prepare_db_job,
    get_prepare_db_job,
    submit_prepare_db_job,
)
from api.services.storage.prepare_db_metadata import (
    _PREPARE_DB_STALE_SECONDS,
    DatabaseOperationOwnershipError,
    require_prepare_operation_owner,
)

LOGGER = logging.getLogger(__name__)
_POLL_INTERVAL = 30.0
_POLL_INITIAL = 5.0
_POLL_MAX_SECONDS = 8 * 60 * 60 + 15 * 60
_SOFT_LIMIT = _POLL_MAX_SECONDS + 20 * 60
_HARD_LIMIT = _POLL_MAX_SECONDS + 30 * 60
if _HARD_LIMIT >= _PREPARE_DB_STALE_SECONDS:
    raise ValueError("prepare-db metadata stale window must exceed Direct task limit")


def _update_state(job_id: str, phase: str, status: str = "running", **extra: Any) -> None:
    _facade._update_state(job_id, phase, status, **extra)
    if status in TERMINAL_STATUSES:
        record_feature_event(
            "prepare_db_direct",
            status=status,
            job_id=job_id,
            phase=phase,
            outcome=extra.get("outcome"),
            error_code=extra.get("error_code"),
        )


def _poll_job(
    credential: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str,
    job_name: str,
    archive_count: int,
    on_progress: Any,
) -> dict[str, Any]:
    deadline = time.monotonic() + _POLL_MAX_SECONDS
    interval = _POLL_INITIAL
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            status: dict[str, Any] = dict(
                get_prepare_db_job(
                    credential,
                    subscription_id,
                    resource_group,
                    cluster_name,
                    namespace=namespace,
                    job_name=job_name,
                )
            )
        except Exception as exc:
            LOGGER.warning(
                "NCBI Direct Job status poll failed job=%s: %s",
                job_name,
                type(exc).__name__,
            )
            time.sleep(interval)
            interval = min(interval * 2, _POLL_INTERVAL)
            continue
        latest = status
        on_progress(status)
        succeeded = int(status.get("succeeded") or 0)
        if succeeded >= archive_count:
            return dict(status)
        for condition in status.get("conditions") or []:
            if (
                str(condition.get("type") or "").lower() == "failed"
                and str(condition.get("status") or "").lower() == "true"
            ):
                return dict(status)
        time.sleep(interval)
        interval = min(interval * 2, _POLL_INTERVAL)
    return {**latest, "timed_out": True}


def _verify_generation(
    container: Any,
    *,
    data_prefix: str,
    archive_count: int,
    transfer_sha: str,
) -> tuple[list[str], int]:
    from api.services.storage.blob_io import read_metadata_blob_text

    expected_markers = {
        f"{data_prefix}/.manifests/{index:02d}.json" for index in range(archive_count)
    }
    present: dict[str, Any] = {}
    blob_sizes: dict[str, int] = {}
    for blob in container.list_blobs(name_starts_with=f"{data_prefix}/"):
        name = str(getattr(blob, "name", "") or "")
        blob_sizes[name] = int(getattr(blob, "size", 0) or 0)
        if name in expected_markers:
            present[name] = blob
    if set(present) != expected_markers:
        raise RuntimeError(f"Direct generation markers incomplete ({len(present)}/{archive_count})")

    files: dict[str, int] = {}
    for marker_name in sorted(expected_markers):
        raw = read_metadata_blob_text(
            container.get_blob_client(marker_name),
            max_bytes=1024 * 1024,
            label="direct-generation-marker",
        )
        marker = json.loads(raw)
        if marker.get("transfer_manifest_sha256") != transfer_sha:
            raise RuntimeError("Direct generation marker transfer hash mismatch")
        entries = marker.get("files")
        if not isinstance(entries, list) or not entries:
            raise RuntimeError("Direct generation marker had no extracted files")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError("Direct generation marker file entry was invalid")
            name = str(entry.get("name") or "")
            size = int(entry.get("size") or 0)
            if not name or "/" in name or size <= 0 or name in files:
                raise RuntimeError("Direct generation marker file identity was invalid")
            files[name] = size

    missing = []
    for name, size in files.items():
        blob_name = f"{data_prefix}/{name}"
        if blob_sizes.get(blob_name) != size:
            missing.append(name)
    if missing:
        raise RuntimeError(
            f"Direct generation files incomplete ({len(missing)} missing/mismatched)"
        )
    return sorted(files), sum(files.values())


@shared_task(
    name="api.tasks.storage.prepare_db_via_ncbi_direct",
    bind=True,
    soft_time_limit=_SOFT_LIMIT,
    time_limit=_HARD_LIMIT,
)
def prepare_db_via_ncbi_direct(
    self: Any,
    *,
    job_id: str,
    prepare_operation_id: str,
    subscription_id: str,
    storage_account: str,
    db_name: str,
    generation_id: str,
    data_prefix: str,
    release: dict[str, Any],
    archives: list[dict[str, Any]],
    transfer_manifest_sha256: str,
    aks_resource_group: str,
    cluster_name: str,
    image: str,
    namespace: str = "default",
    parallelism: int = 4,
    active_deadline_seconds: int = 8 * 60 * 60,
    caller_oid: str = "",
) -> dict[str, Any]:
    """Stage and atomically promote one pinned NCBI Direct DB generation."""
    from api.services.ncbi_direct import NcbiDirectArchive
    from api.services.storage.data import _blob_service
    from api.services.storage.prepare_db_metadata import update_metadata

    started = time.monotonic()
    credential = _facade.get_credential()
    container = _blob_service(credential, storage_account).get_container_client("blast-db")
    from api.services.ncbi_direct_lock import refresh_direct_lock

    try:
        if not refresh_direct_lock(prepare_operation_id):
            raise RuntimeError("NCBI Direct transfer lock ownership was lost")
    except Exception as exc:
        _update_state(
            job_id,
            "failed",
            status="failed",
            outcome="failed",
            error_code="prepare_db_direct_lock_unavailable",
        )
        return {"ok": False, "mode": "ncbi-direct", "error": str(exc)}

    def _verify_owner(meta: dict[str, Any]) -> dict[str, Any]:
        require_prepare_operation_owner(meta, prepare_operation_id)
        return meta

    try:
        update_metadata(container, db_name, storage_account, _verify_owner)
    except Exception as exc:
        _update_state(
            job_id,
            "failed",
            status="failed",
            outcome="failed",
            error_code="prepare_db_direct_owner_lost",
        )
        return {"ok": False, "mode": "ncbi-direct", "error": str(exc)}
    pinned = tuple(NcbiDirectArchive(**entry) for entry in archives)
    job_name = direct_prepare_job_name(db_name, generation_id)
    configmap_name = job_name
    configmap = build_direct_scripts_configmap(
        archives=pinned,
        name=configmap_name,
        namespace=namespace,
    )
    job = build_direct_job_manifest(
        job_name=job_name,
        db_name=db_name,
        storage_account=storage_account,
        generation_id=generation_id,
        destination_prefix=data_prefix,
        transfer_manifest_sha256=transfer_manifest_sha256,
        archive_count=len(pinned),
        scripts_configmap=configmap_name,
        image=image,
        namespace=namespace,
        parallelism=min(parallelism, len(pinned)),
        active_deadline_seconds=active_deadline_seconds,
        max_archive_size=max(item.size for item in pinned),
    )
    try:
        summary = submit_prepare_db_job(
            credential,
            subscription_id,
            aks_resource_group,
            cluster_name,
            configmap_manifest=configmap,
            job_manifest=job,
        )
        if summary.get("status") not in {"created", "existing"}:
            raise RuntimeError("NCBI Direct Kubernetes Job submission failed")
    except Exception as exc:
        _update_state(
            job_id,
            "failed",
            status="failed",
            outcome="failed",
            error_code="prepare_db_direct_dispatch_failed",
        )
        LOGGER.error("NCBI Direct Job submit failed db=%s: %s", db_name, type(exc).__name__)
        return {"ok": False, "mode": "ncbi-direct", "error": str(exc)}

    _update_state(job_id, "downloading", mode="ncbi-direct", archive_count=len(pinned))

    def _progress(status: dict[str, Any]) -> None:
        if not refresh_direct_lock(prepare_operation_id):
            raise RuntimeError("NCBI Direct transfer lock ownership was lost")
        update_metadata(container, db_name, storage_account, _verify_owner)

        def _mut(meta: dict[str, Any]) -> dict[str, Any]:
            require_prepare_operation_owner(meta, prepare_operation_id)
            pending = dict(meta.get("pending_generation") or {})
            pending.update(
                {
                    "phase": "downloading",
                    "active_pods": int(status.get("active") or 0),
                    "succeeded_archives": int(status.get("succeeded") or 0),
                    "failed_pods": int(status.get("failed") or 0),
                    "archive_count": len(pinned),
                }
            )
            meta["pending_generation"] = pending
            return meta

        update_metadata(container, db_name, storage_account, _mut)

    try:
        job_status = _poll_job(
            credential,
            subscription_id,
            aks_resource_group,
            cluster_name,
            namespace=namespace,
            job_name=job_name,
            archive_count=len(pinned),
            on_progress=_progress,
        )
        if int(job_status.get("succeeded") or 0) < len(pinned):
            raise RuntimeError("NCBI Direct Kubernetes Job did not complete")
        files, staged_bytes = _verify_generation(
            container,
            data_prefix=data_prefix,
            archive_count=len(pinned),
            transfer_sha=transfer_manifest_sha256,
        )
        active_prefix = generation_db_prefix(db_name, generation_id)
        layout_prefix = f"{data_prefix}/shards"
        shard_summary = ensure_shard_sets(
            credential,
            storage_account,
            db_name,
            db_prefix=active_prefix,
            layout_prefix=layout_prefix,
        )
        shard_sets = require_complete_shard_summary(shard_summary)

        def _promote(meta: dict[str, Any]) -> dict[str, Any]:
            require_prepare_operation_owner(meta, prepare_operation_id)
            previous = meta.get("active_generation")
            if previous:
                meta["previous_generation"] = previous
            now = datetime.now(UTC).isoformat()
            active = {
                "id": generation_id,
                "prefix": active_prefix,
                "data_prefix": data_prefix,
                "source_provider": "ncbi-direct",
                "source_release_at": release["released_at"],
                "release_fingerprint": release["release_fingerprint"],
                "transfer_manifest_sha256": transfer_manifest_sha256,
                "activated_at": now,
            }
            meta["active_generation"] = active
            meta["active_prefix"] = active_prefix
            meta["shard_layout_prefix"] = layout_prefix
            meta["source_provider"] = "ncbi-direct"
            meta["source_release_at"] = release["released_at"]
            meta["release_fingerprint"] = release["release_fingerprint"]
            meta["transfer_manifest_sha256"] = transfer_manifest_sha256
            meta["taxonomy_release_at"] = release.get("taxonomy_release_at")
            meta["taxonomy_release_fingerprint"] = release.get("taxonomy_release_fingerprint")
            meta["source_version"] = generation_id
            meta["downloaded_at"] = now
            meta["file_count"] = len(files)
            meta["total_bytes"] = staged_bytes
            meta["total_letters"] = int(release["number_of_letters"])
            meta["total_sequences"] = int(release["number_of_sequences"])
            meta["sharded"] = True
            meta["shard_sets"] = shard_sets
            meta["shard_source_version"] = generation_id
            meta["shard_layout_schema"] = int(shard_summary["layout_schema"])
            meta["sharded_at"] = now
            meta["update_in_progress"] = False
            meta["update_completed_at"] = now
            meta["copy_status"] = {
                "phase": "completed",
                "mode": "ncbi-direct",
                "total_files": len(files),
                "success": len(files),
                "failed": 0,
                "pending": 0,
            }
            meta.pop("pending_generation", None)
            meta.pop("prepare_operation_id", None)
            meta.pop("updating_to_source_version", None)
            meta.pop("update_error", None)
            meta.pop("update_failed_at", None)
            meta.pop("aks_job_ref", None)
            if isinstance(meta.get("db_order_oracle"), dict):
                oracle = dict(meta["db_order_oracle"])
                oracle["status"] = "stale"
                meta["db_order_oracle"] = oracle
            return meta

        update_metadata(container, db_name, storage_account, _promote)
        _update_state(job_id, "completed", status="completed", outcome="promoted")
        return {
            "ok": True,
            "mode": "ncbi-direct",
            "db_name": db_name,
            "generation_id": generation_id,
            "files_total": len(files),
            "bytes_total": staged_bytes,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    except DatabaseOperationOwnershipError:
        raise
    except Exception as exc:
        reason = f"NCBI Direct prepare failed: {type(exc).__name__}: {exc}"

        def _fail(meta: dict[str, Any]) -> dict[str, Any]:
            require_prepare_operation_owner(meta, prepare_operation_id)
            pending = dict(meta.get("pending_generation") or {})
            pending.update({"phase": "failed", "error": reason})
            meta["pending_generation"] = pending
            meta["update_in_progress"] = False
            meta["update_error"] = reason[:500]
            meta["update_failed_at"] = datetime.now(UTC).isoformat()
            meta.pop("prepare_operation_id", None)
            meta.pop("updating_to_source_version", None)
            meta.pop("aks_job_ref", None)
            return meta

        update_metadata(container, db_name, storage_account, _fail)
        _update_state(
            job_id,
            "failed",
            status="failed",
            outcome="failed",
            error_code="prepare_db_direct_failed",
        )
        return {"ok": False, "mode": "ncbi-direct", "error": reason}
    finally:
        from api.services.ncbi_direct_lock import release_direct_lock

        try:
            delete_prepare_db_job(
                credential,
                subscription_id,
                aks_resource_group,
                cluster_name,
                namespace=namespace,
                job_name=job_name,
                configmap_name=configmap_name,
            )
        except Exception as cleanup_exc:
            LOGGER.warning(
                "NCBI Direct Job cleanup failed job=%s: %s",
                job_name,
                type(cleanup_exc).__name__,
            )
        try:
            release_direct_lock(prepare_operation_id)
        except Exception as lock_exc:
            LOGGER.warning(
                "NCBI Direct lock release failed owner=%s: %s",
                prepare_operation_id,
                type(lock_exc).__name__,
            )

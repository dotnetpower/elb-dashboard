"""Dispatch NCBI Direct database generations after AKS preflight succeeds.

Responsibility: Build the pinned Direct manifest, atomically claim one DB
    operation, persist the pending generation and deterministic Job reference,
    and enqueue the Direct Celery task.
Edit boundaries: Dispatch and metadata claim only; cluster/RBAC preflight stays
    in `prepare_db_aks_dispatch`, transfer execution in the task, and HTTP
    response shaping in the route.
Key entry points: `dispatch_ncbi_direct`.
Risky contracts: Feature is explicit opt-in, never falls back to stale S3,
    metadata is claimed before enqueue, and enqueue failure rolls back only the
    matching operation owner.
Validation: `uv run pytest -q api/tests/test_prepare_db_direct_dispatch.py`.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from api.auth import CallerIdentity
from api.services.db.generations import generation_data_prefix
from api.services.k8s.prepare_db_direct_jobs import direct_prepare_job_name
from api.services.ncbi_direct import build_direct_manifest, transfer_manifest_sha256
from api.services.storage.prepare_db_metadata import (
    DatabaseOperationInProgressError,
    download_blob_with_etag,
    is_stale_prepare_marker,
    is_stale_sharding_marker,
    require_prepare_operation_owner,
    update_metadata,
)


class DirectDispatchError(RuntimeError):
    """Structured failure translated to HTTP by the owning route."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def direct_enabled() -> bool:
    return os.environ.get("PREPARE_DB_NCBI_DIRECT_ENABLED", "false").lower() == "true"


def dispatch_ncbi_direct(
    *,
    caller: CallerIdentity,
    credential: Any,
    subscription_id: str,
    storage_account: str,
    db_name: str,
    aks_resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Claim and enqueue one explicit NCBI Direct generation update."""
    if not direct_enabled():
        raise DirectDispatchError(
            409,
            "NCBI Direct updates are disabled for this deployment.",
        )
    manifest = build_direct_manifest(db_name)
    taxonomy = None
    if (
        db_name != "taxdb"
        and os.environ.get("PREPARE_DB_INCLUDE_TAXONOMY", "true").lower() != "false"
    ):
        taxonomy = build_direct_manifest("taxdb")
    archives = manifest.archives + (taxonomy.archives if taxonomy else ())
    transfer_sha = transfer_manifest_sha256(archives)
    data_prefix = generation_data_prefix(db_name, manifest.generation_id)

    from api.routes._blast_shared import _safe_send_task
    from api.services.storage.data import _blob_service

    container = _blob_service(credential, storage_account).get_container_client("blast-db")
    current, _etag = download_blob_with_etag(container, db_name)
    active = current.get("active_generation")
    if (
        isinstance(active, dict)
        and active.get("release_fingerprint") == manifest.release_fingerprint
    ):
        raise DirectDispatchError(409, "This NCBI database release is already active.")

    operation_id = uuid.uuid4().hex
    from api.services.ncbi_direct_lock import acquire_direct_lock

    try:
        lock_acquired = acquire_direct_lock(operation_id)
    except Exception as exc:
        raise DirectDispatchError(503, "NCBI Direct transfer lock is unavailable") from exc
    if not lock_acquired:
        raise DirectDispatchError(
            409,
            "Another NCBI Direct database transfer is already running.",
        )
    started_at = datetime.now(UTC).isoformat()
    namespace = os.environ.get("PREPARE_DB_AKS_NAMESPACE", "default")
    job_name = direct_prepare_job_name(db_name, manifest.generation_id)
    job_ref = {
        "subscription_id": subscription_id,
        "resource_group": aks_resource_group,
        "cluster_name": cluster_name,
        "namespace": namespace,
        "job_name": job_name,
        "configmap_name": job_name,
        "started_at": started_at,
        "source_provider": "ncbi-direct",
    }

    def _claim(meta: dict[str, Any]) -> dict[str, Any]:
        if meta.get("update_in_progress") and not is_stale_prepare_marker(meta):
            raise DatabaseOperationInProgressError("prepare-db is already running for this DB")
        if meta.get("sharding_in_progress") and not is_stale_sharding_marker(meta):
            raise DatabaseOperationInProgressError("sharding is already running for this DB")
        meta["db_name"] = db_name
        meta["update_in_progress"] = True
        meta["update_started_at"] = started_at
        meta["prepare_operation_id"] = operation_id
        meta["updating_to_source_version"] = manifest.generation_id
        meta["pending_generation"] = {
            "id": manifest.generation_id,
            "data_prefix": data_prefix,
            "source_provider": "ncbi-direct",
            "source_release_at": manifest.released_at,
            "release_fingerprint": manifest.release_fingerprint,
            "transfer_manifest_sha256": transfer_sha,
            "taxonomy_release_at": taxonomy.released_at if taxonomy else None,
            "taxonomy_release_fingerprint": (taxonomy.release_fingerprint if taxonomy else None),
            "phase": "queued",
            "archive_count": len(archives),
            "bytes_total_compressed": sum(item.size for item in archives),
            "started_at": started_at,
        }
        meta["aks_job_ref"] = job_ref
        meta.pop("update_error", None)
        meta.pop("update_failed_at", None)
        return meta

    try:
        update_metadata(container, db_name, storage_account, _claim)
    except DatabaseOperationInProgressError as exc:
        from api.services.ncbi_direct_lock import release_direct_lock

        release_direct_lock(operation_id)
        raise DirectDispatchError(409, str(exc)) from exc
    except Exception as exc:
        from api.services.ncbi_direct_lock import release_direct_lock

        release_direct_lock(operation_id)
        raise DirectDispatchError(502, "NCBI Direct metadata claim failed") from exc

    configured_image = os.environ.get("PREPARE_DB_AKS_AZCOPY_IMAGE", "").strip()
    if not configured_image:
        from api.services.ncbi_direct_lock import release_direct_lock

        def _rollback_missing_image(meta: dict[str, Any]) -> dict[str, Any]:
            require_prepare_operation_owner(meta, operation_id)
            meta["update_in_progress"] = False
            meta.pop("prepare_operation_id", None)
            meta.pop("pending_generation", None)
            meta.pop("updating_to_source_version", None)
            meta.pop("aks_job_ref", None)
            meta["update_error"] = "NCBI Direct requires the prebuilt prepare-db image"
            meta["update_failed_at"] = datetime.now(UTC).isoformat()
            return meta

        update_metadata(container, db_name, storage_account, _rollback_missing_image)
        release_direct_lock(operation_id)
        raise DirectDispatchError(
            409,
            "NCBI Direct requires the prebuilt elb-prepare-db image; complete postprovision first.",
        )
    parallelism = max(
        1,
        min(int(os.environ.get("PREPARE_DB_NCBI_DIRECT_PARALLELISM", "4")), 8),
    )
    deadline = max(
        3600,
        int(os.environ.get("PREPARE_DB_NCBI_DIRECT_TIMEOUT_SECONDS", str(8 * 60 * 60))),
    )
    job_id = f"prepare-db-direct-{db_name}-{int(time.time())}"
    task_kwargs = {
        "job_id": job_id,
        "prepare_operation_id": operation_id,
        "subscription_id": subscription_id,
        "storage_account": storage_account,
        "db_name": db_name,
        "generation_id": manifest.generation_id,
        "data_prefix": data_prefix,
        "release": {
            "released_at": manifest.released_at,
            "release_fingerprint": manifest.release_fingerprint,
            "number_of_letters": manifest.number_of_letters,
            "number_of_sequences": manifest.number_of_sequences,
            "bytes_total": manifest.bytes_total,
            "taxonomy_release_at": taxonomy.released_at if taxonomy else None,
            "taxonomy_release_fingerprint": (taxonomy.release_fingerprint if taxonomy else None),
        },
        "archives": [
            {
                "url": item.url,
                "md5_url": item.md5_url,
                "md5": item.md5,
                "size": item.size,
                "member_prefix": item.member_prefix,
            }
            for item in archives
        ],
        "transfer_manifest_sha256": transfer_sha,
        "aks_resource_group": aks_resource_group,
        "cluster_name": cluster_name,
        "namespace": namespace,
        "image": configured_image,
        "parallelism": min(parallelism, len(archives)),
        "active_deadline_seconds": deadline,
        "caller_oid": caller.object_id,
    }
    try:
        result = _safe_send_task(
            "api.tasks.storage.prepare_db_via_ncbi_direct",
            queue="storage",
            **task_kwargs,
        )
    except Exception as exc:
        from api.services.ncbi_direct_lock import release_direct_lock

        def _rollback(meta: dict[str, Any]) -> dict[str, Any]:
            require_prepare_operation_owner(meta, operation_id)
            meta["update_in_progress"] = False
            meta.pop("prepare_operation_id", None)
            meta.pop("pending_generation", None)
            meta.pop("updating_to_source_version", None)
            meta.pop("aks_job_ref", None)
            meta["update_error"] = "NCBI Direct task dispatch failed"
            meta["update_failed_at"] = datetime.now(UTC).isoformat()
            return meta

        update_metadata(container, db_name, storage_account, _rollback)
        release_direct_lock(operation_id)
        raise DirectDispatchError(503, "NCBI Direct task dispatch failed") from exc

    return {
        "ok": True,
        "async": True,
        "mode": "ncbi-direct",
        "db_name": db_name,
        "source_version": manifest.generation_id,
        "generation_id": manifest.generation_id,
        "source_release_at": manifest.released_at,
        "release_fingerprint": manifest.release_fingerprint,
        "files_total": len(archives),
        "bytes_total": sum(item.size for item in archives),
        "task_id": result.id,
        "output": "NCBI Direct generation queued",
    }


__all__ = ["DirectDispatchError", "direct_enabled", "dispatch_ncbi_direct"]

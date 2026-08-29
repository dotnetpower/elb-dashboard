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
    A cancel route intentionally replaces the operation owner; that ownership
    loss terminates this task as cancelled without rewriting the cancel metadata.
Validation: `uv run pytest -q api/tests/test_prepare_db_direct_task.py`.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from celery import shared_task

import api.tasks.storage as _facade
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
from api.services.storage.direct_promotion import promote_direct_generation
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


def _ownership_loss_outcome(metadata: dict[str, Any], expected_owner: str) -> str | None:
    """Classify a replaced owner as cancelled/superseded, or return None."""
    if str(metadata.get("prepare_operation_id") or "") == expected_owner:
        return None
    pending = metadata.get("pending_generation")
    phase = str(pending.get("phase") or "") if isinstance(pending, dict) else ""
    if not metadata.get("update_in_progress") and phase == "cancelled":
        return "cancelled"
    return "superseded"


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
    from api.services.ncbi_direct_lock import (
        claim_or_refresh_direct_lock,
        release_direct_lock,
    )
    from api.services.storage.prepare_db_metadata import download_blob_with_etag

    def _verify_owner(meta: dict[str, Any]) -> dict[str, Any]:
        require_prepare_operation_owner(meta, prepare_operation_id)
        return meta

    def _finish_owner_loss(current: dict[str, Any]) -> dict[str, Any]:
        outcome = _ownership_loss_outcome(current, prepare_operation_id) or "superseded"
        _update_state(job_id, outcome, status="cancelled", outcome=outcome)
        return {
            "ok": False,
            "mode": "ncbi-direct",
            "db_name": db_name,
            "generation_id": generation_id,
            "cancelled": True,
            "outcome": outcome,
        }

    def _release_lock_quietly() -> None:
        try:
            release_direct_lock(prepare_operation_id)
        except Exception as exc:
            LOGGER.warning(
                "NCBI Direct lock release failed owner=%s: %s",
                prepare_operation_id,
                type(exc).__name__,
            )

    try:
        update_metadata(container, db_name, storage_account, _verify_owner)
        if not claim_or_refresh_direct_lock(prepare_operation_id):
            raise RuntimeError("NCBI Direct transfer lock ownership was lost")
        # Close the cross-store race: cancellation may commit to Blob between
        # the first owner check and reclaiming an absent Redis lock.
        update_metadata(container, db_name, storage_account, _verify_owner)
    except DatabaseOperationOwnershipError:
        try:
            current, _etag = download_blob_with_etag(container, db_name)
        except Exception:
            current = {}
        _release_lock_quietly()
        return _finish_owner_loss(current)
    except Exception as exc:
        _update_state(
            job_id,
            "failed",
            status="failed",
            outcome="failed",
            error_code="prepare_db_direct_owner_lost",
        )
        _release_lock_quietly()
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
        # Durable metadata is the authority; Redis can disappear on a revision
        # replacement. Verify the owner before and after reclaiming/refreshing
        # the ephemeral lock so a concurrent cancel cannot be mistaken for an
        # infrastructure failure or have its terminal metadata overwritten.
        update_metadata(container, db_name, storage_account, _verify_owner)
        if not claim_or_refresh_direct_lock(prepare_operation_id):
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
        promoted = promote_direct_generation(
            credential=credential,
            storage_account=storage_account,
            db_name=db_name,
            operation_id=prepare_operation_id,
            generation_id=generation_id,
            data_prefix=data_prefix,
            release=release,
            transfer_sha=transfer_manifest_sha256,
            archive_count=len(pinned),
            container=container,
        )
        _update_state(job_id, "completed", status="completed", outcome="promoted")
        return {
            "ok": True,
            "mode": "ncbi-direct",
            "db_name": db_name,
            "generation_id": generation_id,
            "files_total": promoted["files_total"],
            "bytes_total": promoted["bytes_total"],
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    except DatabaseOperationOwnershipError:
        # Cancellation takes ownership under the metadata ETag before deleting
        # the Job. The old worker must stop without touching that committed
        # terminal state or emitting an unexpected task failure. A rapid
        # cancel/resubmit may already show a fresh owner; classify that as
        # superseded for the old JobState while preserving the new operation.
        try:
            current, _etag = download_blob_with_etag(container, db_name)
        except Exception as read_exc:
            LOGGER.warning(
                "NCBI Direct ownership-loss state read failed db=%s: %s",
                db_name,
                type(read_exc).__name__,
            )
            current = {}
        return _finish_owner_loss(current)
    except Exception as exc:
        # Redis lock loss and other failures can race a cancellation after the
        # last metadata checkpoint. Re-read before writing failure state: when
        # the durable owner changed, the new owner has already committed the
        # authoritative terminal/new-operation decision.
        try:
            current, _etag = download_blob_with_etag(container, db_name)
            owner_outcome = _ownership_loss_outcome(current, prepare_operation_id)
        except Exception:
            current = {}
            owner_outcome = None
        if owner_outcome is not None:
            return _finish_owner_loss(current)
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
        _release_lock_quietly()

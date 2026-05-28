"""`prepare_db_via_aks` Celery task — fan-out prepare-db across an AKS Job.

Responsibility: Plan shards, submit the Indexed K8s Job through
    `api.services.k8s.prepare_db_jobs`, poll Job + per-blob completion, and
    write the same `<db>-metadata.json` state machine the server-side
    [api/routes/storage/prepare_db.py](../../routes/storage/prepare_db.py)
    route uses (start → copying → partial / promote). Issue #7 Phase 1.
Edit boundaries: This task owns the AKS-fanout path only. The HTTP route
    keeps validation and 409 handling; per-pod manifest / script builders
    live in `api.services.k8s.prepare_db_jobs`. State + auditing + shard
    derivation are reused from the route module (deferred imports).
Key entry points: `prepare_db_via_aks` Celery task (registered as
    `api.tasks.storage.prepare_db_via_aks`).
Risky contracts: Task name is referenced by the route's `_safe_send_task`
    + `api/tests/test_prepare_db_aks_route.py` and must not rename. The
    final metadata shape MUST match the server-side path so SPA polling +
    test fixtures stay shape-stable. The HTTP route's `threading.Lock`
    does not propagate to this worker process — cross-process
    serialisation is the metadata blob's `update_in_progress=true` flag
    (written by the route before enqueue) and the
    `_PREPARE_DB_STALE_SECONDS` stale-flag recovery window if the worker
    crashes mid-task.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_task.py
    api/tests/test_prepare_db_aks_route.py`.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from celery import shared_task

import api.tasks.storage as _facade
from api.services.k8s.prepare_db_jobs import (
    DEFAULT_ACTIVE_DEADLINE_SECONDS,
    DEFAULT_AZCOPY_CONCURRENCY,
    DEFAULT_AZCOPY_IMAGE,
    DEFAULT_BACKOFF_LIMIT,
    DEFAULT_FILES_PER_POD,
    DEFAULT_MAX_PARALLELISM,
    DEFAULT_NAMESPACE,
    DEFAULT_TTL_SECONDS_AFTER_FINISHED,
    build_prepare_db_job_manifest,
    build_prepare_db_scripts_configmap,
    delete_prepare_db_job,
    get_prepare_db_job,
    plan_prepare_db_shards,
    prepare_db_job_name,
    submit_prepare_db_job,
)

LOGGER = logging.getLogger(__name__)


def _update_state(job_id: str, phase: str, status: str = "running", **extra: Any) -> None:
    _facade._update_state(job_id, phase, status, **extra)


def _record_task_progress(task: Any, phase: str, **meta: Any) -> None:
    _facade._record_task_progress(task, phase, **meta)


def get_credential() -> Any:
    return _facade.get_credential()


# Step-wise poll cadence in seconds. The task polls every
# ``_JOB_POLL_INTERVAL_SECONDS`` and times out at ``_JOB_POLL_MAX_SECONDS``
# regardless of `activeDeadlineSeconds` so the Celery side never gets stuck
# even if the K8s API stops responding.
_JOB_POLL_INTERVAL_SECONDS = 30.0
_JOB_POLL_MAX_SECONDS = 4 * 60 * 60


@shared_task(
    name="api.tasks.storage.prepare_db_via_aks",
    bind=True,
    max_retries=1,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def prepare_db_via_aks(
    self: Any,
    *,
    job_id: str,
    subscription_id: str,
    storage_resource_group: str,
    storage_account: str,
    db_name: str,
    source_version: str,
    file_keys: list[str],
    aks_resource_group: str,
    cluster_name: str,
    file_sizes: dict[str, int] | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    image: str = DEFAULT_AZCOPY_IMAGE,
    max_pods: int = DEFAULT_MAX_PARALLELISM,
    files_per_pod: int = DEFAULT_FILES_PER_POD,
    azcopy_concurrency: int = DEFAULT_AZCOPY_CONCURRENCY,
    backoff_limit: int = DEFAULT_BACKOFF_LIMIT,
    ttl_seconds_after_finished: int = DEFAULT_TTL_SECONDS_AFTER_FINISHED,
    active_deadline_seconds: int = DEFAULT_ACTIVE_DEADLINE_SECONDS,
    caller_oid: str = "",
) -> dict[str, Any]:
    """Submit + poll the AKS-fanout prepare-db Job for `(db_name, source_version)`.

    Cross-process serialisation is provided by the metadata blob's
    `update_in_progress=true` flag (written by the HTTP route before this
    task is enqueued) + the stale-flag recovery window
    (`_PREPARE_DB_STALE_SECONDS`). The route's in-process `threading.Lock`
    does not propagate to the worker process, and there's no benefit in
    acquiring a fresh worker-side lock here — Celery already guarantees
    one execution per task message, and a duplicate enqueue would re-trip
    the route-level metadata-flag check.
    """
    from api.routes.storage.prepare_db import (
        _poll_copy_completion,
        _update_metadata,
    )
    from api.services.storage.data import _blob_service

    started_monotonic = time.monotonic()
    LOGGER.info(
        "prepare_db_via_aks start oid=%s db=%s source=%s files=%d",
        caller_oid,
        db_name,
        source_version,
        len(file_keys),
    )

    if not file_keys:
        raise ValueError("file_keys must not be empty")
    if not source_version:
        raise ValueError("source_version is required for AKS-fanout prepare-db")

    cred = get_credential()
    blob_svc = _blob_service(cred, storage_account)
    container = blob_svc.get_container_client("blast-db")

    sizes = file_sizes or {}
    shards = plan_prepare_db_shards(
        file_keys,
        sizes=sizes,
        max_pods=max_pods,
        files_per_pod=files_per_pod,
    )
    shard_count = len(shards)
    if shard_count < 1:
        raise ValueError("shard planner returned zero shards")

    job_name = prepare_db_job_name(db_name, source_version)
    configmap_name = job_name  # 1:1 keyed name; deterministic & deletable together

    _update_state(
        job_id,
        "dispatching",
        status="running",
        mode="aks",
        job_name=job_name,
        shard_count=shard_count,
        total_files=len(file_keys),
    )
    _record_task_progress(
        self,
        "dispatching",
        mode="aks",
        job_name=job_name,
        shard_count=shard_count,
        total_files=len(file_keys),
    )

    configmap_manifest = build_prepare_db_scripts_configmap(
        shards=shards,
        name=configmap_name,
        namespace=namespace,
    )
    job_manifest = build_prepare_db_job_manifest(
        job_name=job_name,
        db_name=db_name,
        storage_account=storage_account,
        source_version=source_version,
        shard_count=shard_count,
        scripts_configmap=configmap_name,
        image=image,
        namespace=namespace,
        azcopy_concurrency=azcopy_concurrency,
        backoff_limit=backoff_limit,
        ttl_seconds_after_finished=ttl_seconds_after_finished,
        active_deadline_seconds=active_deadline_seconds,
    )

    try:
        submit_summary = submit_prepare_db_job(
            cred,
            subscription_id,
            aks_resource_group,
            cluster_name,
            configmap_manifest=configmap_manifest,
            job_manifest=job_manifest,
        )
    except Exception as exc:
        _mark_partial(
            container,
            db_name,
            storage_account,
            _update_metadata,
            reason=f"AKS dispatch failed: {type(exc).__name__}",
            failed_files=[],
            mode="aks",
            stage="dispatch",
        )
        raise

    if submit_summary.get("status") not in {"created", "existing"}:
        # Don't promote, leave metadata in partial state. Do not delete the
        # ConfigMap — it may belong to a peer's in-flight Job.
        _mark_partial(
            container,
            db_name,
            storage_account,
            _update_metadata,
            reason=(
                f"AKS Job submit error: {submit_summary.get('stage')}/"
                f"{submit_summary.get('status')}"
            ),
            failed_files=[],
            mode="aks",
            stage="dispatch",
            submit_summary=submit_summary,
        )
        return {
            "ok": False,
            "mode": "aks",
            "reason": "submit_failed",
            "summary": submit_summary,
        }

    _update_state(
        job_id,
        "copying",
        status="running",
        mode="aks",
        job_name=job_name,
        shard_count=shard_count,
    )
    _record_task_progress(
        self,
        "copying",
        mode="aks",
        job_name=job_name,
        shard_count=shard_count,
    )

    # Staged blob names mirror what the server-side path expects: one blob
    # per source file at `<db>/<basename>`. The per-pod script writes there.
    staged_blob_names = [
        f"{db_name}/{key.rsplit('/', 1)[-1]}" for key in file_keys
    ]

    job_result: dict[str, Any] = {}
    job_timed_out = False
    try:
        job_result = _poll_job_until_done(
            cred,
            subscription_id,
            aks_resource_group,
            cluster_name,
            namespace=namespace,
            job_name=job_name,
            on_progress=lambda snap: _on_job_progress(
                container,
                db_name,
                storage_account,
                file_keys,
                snap,
                mode_label="aks",
                update_metadata=_update_metadata,
            ),
        )
        job_timed_out = bool(job_result.get("timed_out"))
    except Exception as exc:
        _mark_partial(
            container,
            db_name,
            storage_account,
            _update_metadata,
            reason=f"AKS Job poll failed: {type(exc).__name__}",
            failed_files=[],
            mode="aks",
            stage="poll",
        )
        _safe_delete_job(
            cred,
            subscription_id,
            aks_resource_group,
            cluster_name,
            namespace,
            job_name,
            configmap_name,
        )
        raise

    # The Job + per-blob views can disagree (eg pod marked complete with
    # 1 fail, but blob copy is still pending). Confirm via the same
    # `_poll_copy_completion` the server-side path uses so the resulting
    # metadata.json is byte-shape identical.
    poll_summary = _poll_copy_completion(
        container,
        staged_blob_names,
        db_name=db_name,
    )

    job_succeeded = bool(job_result.get("succeeded_pods", 0) >= shard_count)
    all_blobs_succeeded = (
        poll_summary["failed"] == 0
        and poll_summary["aborted"] == 0
        and not poll_summary["timed_out"]
        and poll_summary["success"] >= len(staged_blob_names)
    )

    try:
        if job_succeeded and all_blobs_succeeded:
            _promote_success(
                container,
                db_name,
                storage_account,
                source_version,
                file_keys,
                poll_summary,
                _update_metadata,
                credential=cred,
                mode="aks",
            )
            outcome = "promoted"
        else:
            reason_bits: list[str] = []
            if job_timed_out:
                reason_bits.append(
                    f"AKS Job poll timed out after {_JOB_POLL_MAX_SECONDS}s"
                )
            if not job_succeeded:
                reason_bits.append(
                    f"Job pods succeeded={job_result.get('succeeded_pods', 0)}/"
                    f"{shard_count} failed={job_result.get('failed_pods', 0)}"
                )
            if not all_blobs_succeeded:
                reason_bits.append(
                    f"blobs success={poll_summary['success']}/"
                    f"{len(staged_blob_names)} failed={poll_summary['failed']} "
                    f"pending={poll_summary['pending']}"
                )
            reason = "; ".join(reason_bits) or "AKS prepare-db did not complete"
            _mark_partial(
                container,
                db_name,
                storage_account,
                _update_metadata,
                reason=reason,
                failed_files=poll_summary.get("failed_files", []),
                mode="aks",
                stage="post-job",
                copy_summary={
                    "phase": "partial",
                    "total_files": len(file_keys),
                    "success": poll_summary["success"],
                    "failed": poll_summary["failed"],
                    "aborted": poll_summary["aborted"],
                    "pending": poll_summary["pending"],
                    "timed_out": poll_summary["timed_out"],
                },
            )
            outcome = "partial"
    finally:
        _safe_delete_job(
            cred,
            subscription_id,
            aks_resource_group,
            cluster_name,
            namespace,
            job_name,
            configmap_name,
        )

    elapsed = round(time.monotonic() - started_monotonic, 2)
    _update_state(
        job_id,
        "completed" if outcome == "promoted" else "partial",
        status="completed" if outcome == "promoted" else "failed",
        mode="aks",
        outcome=outcome,
        success_files=poll_summary["success"],
        failed_files=poll_summary["failed"],
        elapsed_seconds=elapsed,
    )
    LOGGER.info(
        "prepare_db_via_aks done oid=%s db=%s outcome=%s elapsed=%.1fs",
        caller_oid,
        db_name,
        outcome,
        elapsed,
    )
    try:
        _facade._publish_db_metadata_invalidate(storage_account, db_name)
    except Exception as exc:
        LOGGER.debug(
            "metadata invalidate publish skipped db=%s: %s",
            db_name,
            type(exc).__name__,
        )
    return {
        "ok": outcome == "promoted",
        "mode": "aks",
        "db_name": db_name,
        "source_version": source_version,
        "files_total": len(file_keys),
        "files_succeeded": poll_summary["success"],
        "files_failed": poll_summary["failed"],
        "shard_count": shard_count,
        "outcome": outcome,
        "elapsed_seconds": elapsed,
    }


def _on_job_progress(
    container: Any,
    db_name: str,
    storage_account: str,
    file_keys: list[str],
    snapshot: dict[str, Any],
    *,
    mode_label: str,
    update_metadata: Any,
) -> None:
    """Mirror the server-side `_record_progress` shape so the SPA can render."""

    def _mut(meta: dict[str, Any]) -> dict[str, Any]:
        meta["copy_status"] = {
            "phase": "copying",
            "mode": mode_label,
            "total_files": len(file_keys),
            "active_pods": int(snapshot.get("active_pods") or 0),
            "succeeded_pods": int(snapshot.get("succeeded_pods") or 0),
            "failed_pods": int(snapshot.get("failed_pods") or 0),
            "shard_count": int(snapshot.get("shard_count") or 0),
        }
        return meta

    try:
        update_metadata(container, db_name, storage_account, _mut)
    except Exception as exc:
        LOGGER.debug(
            "AKS poll progress metadata write skipped db=%s: %s",
            db_name,
            type(exc).__name__,
        )


def _poll_job_until_done(
    credential: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str,
    job_name: str,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Poll the Job's `.status.active/.succeeded/.failed` until completion or timeout."""
    deadline = time.monotonic() + _JOB_POLL_MAX_SECONDS
    last_snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            status = get_prepare_db_job(
                credential,
                subscription_id,
                resource_group,
                cluster_name,
                namespace=namespace,
                job_name=job_name,
            )
        except Exception as exc:
            LOGGER.warning(
                "AKS Job status poll failed job=%s: %s",
                job_name,
                type(exc).__name__,
            )
            time.sleep(_JOB_POLL_INTERVAL_SECONDS)
            continue
        if status.get("missing"):
            # Job has been GC'd (TTL controller, peer delete). Treat as
            # done — the per-blob poll downstream is the source of truth
            # for whether the data actually landed.
            return {
                "succeeded_pods": int(status.get("succeeded") or 0),
                "failed_pods": int(status.get("failed") or 0),
                "active_pods": 0,
                "completions": int(status.get("completions") or 0),
                "shard_count": int(status.get("parallelism") or 0),
                "missing": True,
            }
        succeeded = int(status.get("succeeded") or 0)
        failed = int(status.get("failed") or 0)
        active = int(status.get("active") or 0)
        completions = int(status.get("completions") or 0)
        last_snapshot = {
            "succeeded_pods": succeeded,
            "failed_pods": failed,
            "active_pods": active,
            "completions": completions,
            "shard_count": int(status.get("parallelism") or 0),
            "conditions": status.get("conditions") or [],
        }
        if callable(on_progress):
            try:
                on_progress(last_snapshot)
            except Exception as exc:
                LOGGER.debug(
                    "AKS poll on_progress callback failed: %s", type(exc).__name__
                )
        terminal = _job_is_terminal(status, succeeded, completions)
        if terminal:
            return last_snapshot
        time.sleep(_JOB_POLL_INTERVAL_SECONDS)
    last_snapshot["timed_out"] = True
    return last_snapshot


def _job_is_terminal(
    status: dict[str, Any], succeeded: int, completions: int
) -> bool:
    """Return True if the Job has reached a terminal state.

    K8s sets `conditions: [{type: Complete}]` once `succeeded >= completions`
    and `{type: Failed}` once backoffLimit is exceeded. We check both the
    summed counts and the conditions list because the conditions array can
    lag behind the counter by one poll cycle.
    """
    if completions > 0 and succeeded >= completions:
        return True
    for cond in status.get("conditions") or []:
        if str(cond.get("type", "")).lower() in {"complete", "failed"}:
            if str(cond.get("status", "")).lower() == "true":
                return True
    return False


def _mark_partial(
    container: Any,
    db_name: str,
    storage_account: str,
    update_metadata: Any,
    *,
    reason: str,
    failed_files: list[dict[str, str]] | None = None,
    mode: str = "aks",
    stage: str = "post-job",
    submit_summary: dict[str, Any] | None = None,
    copy_summary: dict[str, Any] | None = None,
) -> None:
    """Write the same partial-completion shape the server-side path writes."""

    def _mut(meta: dict[str, Any]) -> dict[str, Any]:
        meta["db_name"] = db_name
        meta["update_in_progress"] = False
        meta["update_error"] = reason
        meta["update_failed_at"] = datetime.now(UTC).isoformat()
        if failed_files:
            meta["failed_files"] = failed_files
        if copy_summary is not None:
            meta["copy_status"] = copy_summary
        else:
            meta["copy_status"] = {
                "phase": "partial",
                "mode": mode,
                "stage": stage,
                "reason": reason,
            }
        if submit_summary is not None:
            meta["aks_submit_summary"] = submit_summary
        return meta

    try:
        update_metadata(container, db_name, storage_account, _mut)
    except Exception as exc:
        LOGGER.warning(
            "AKS prepare-db partial metadata write failed db=%s: %s",
            db_name,
            type(exc).__name__,
        )


def _promote_success(
    container: Any,
    db_name: str,
    storage_account: str,
    source_version: str,
    file_keys: list[str],
    poll_summary: dict[str, Any],
    update_metadata: Any,
    *,
    credential: Any,
    mode: str,
) -> None:
    """Run auto-shard + promote `source_version`, byte-shape identical to server-side."""
    # Deferred import keeps the import graph free of cycles (sharding
    # imports storage data helpers that pull in the route).
    from api.services.db.sharding import (
        PRESET_SHARD_SETS,
        derive_volumes_from_keys,
        upload_shard_set,
    )

    shard_sets_created: list[int] = []
    try:
        volumes = derive_volumes_from_keys(db_name, file_keys)
        for n in PRESET_SHARD_SETS:
            if n > len(volumes):
                continue
            try:
                upload_shard_set(credential, storage_account, db_name, n, volumes)
                shard_sets_created.append(n)
            except Exception as exc:
                LOGGER.warning(
                    "AKS prepare-db shard set N=%d failed for %s: %s",
                    n,
                    db_name,
                    type(exc).__name__,
                )
    except LookupError:
        LOGGER.info(
            "AKS prepare-db auto-shard skipped for %s: no volumes detected",
            db_name,
        )
    except Exception as exc:
        LOGGER.warning(
            "AKS prepare-db auto-shard failed for %s: %s",
            db_name,
            type(exc).__name__,
        )

    new_signature_etag: str | None = None
    new_composite_signature: str | None = None
    try:
        from api.services.ncbi_catalogue import database_update_signature

        sig = database_update_signature(db_name)
        new_signature_etag = sig.get("signature_etag")
        new_composite_signature = sig.get("composite_signature")
    except Exception as exc:
        LOGGER.debug(
            "AKS post-prepare signature lookup skipped db=%s: %s",
            db_name,
            type(exc).__name__,
        )

    def _mut(meta: dict[str, Any]) -> dict[str, Any]:
        previous_source_version = str(meta.get("source_version") or "")
        meta["db_name"] = db_name
        meta["source_version"] = source_version
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
            "mode": mode,
            "total_files": len(file_keys),
            "success": poll_summary["success"],
            "failed": 0,
            "aborted": 0,
            "pending": 0,
            "timed_out": False,
        }
        if previous_source_version and previous_source_version != source_version:
            meta["updated_from_source_version"] = previous_source_version
        if shard_sets_created:
            meta["sharded"] = True
            meta["shard_sets"] = shard_sets_created
            meta["shard_source_version"] = source_version
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
                and oracle.get("source_version") != source_version
            ):
                oracle["status"] = "stale"
            meta["db_order_oracle"] = oracle
        return meta

    try:
        update_metadata(container, db_name, storage_account, _mut)
    except Exception as exc:
        LOGGER.warning(
            "AKS prepare-db promotion metadata write failed db=%s: %s",
            db_name,
            type(exc).__name__,
        )


def _safe_delete_job(
    credential: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    job_name: str,
    configmap_name: str,
) -> None:
    """Best-effort delete of Job + ConfigMap. K8s TTL is the safety net."""
    try:
        delete_prepare_db_job(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            namespace=namespace,
            job_name=job_name,
            configmap_name=configmap_name,
        )
    except Exception as exc:
        LOGGER.warning(
            "AKS prepare-db cleanup failed job=%s: %s",
            job_name,
            type(exc).__name__,
        )

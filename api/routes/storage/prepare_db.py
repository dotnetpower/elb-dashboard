"""Storage prepare-db route for NCBI BLAST database copies.

Responsibility: Storage prepare-db route for NCBI BLAST database copies
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_read_db_metadata`, `_write_db_metadata`, `_update_metadata`,
`_poll_copy_completion`, `prepare_db`
Risky contracts: Never issue browser SAS URLs; local public Storage access remains debug-only
and IP-allowlisted. Concurrent prepare_db calls for the same (account, db) MUST be serialised
by `_PREPARE_DB_LOCK_REGISTRY` so the metadata.json is never raced. `source_version` is
promoted ONLY when every server-side copy reaches `success`; partial copies must leave the
previous generation's `source_version` intact.
Validation: `uv run pytest -q api/tests/test_storage_data.py
api/tests/test_storage_public_access.py api/tests/test_prepare_db_hardening.py`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from threading import Thread
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
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
    shared_taxonomy_keys,
)
from api.services import get_credential
from api.services.sanitise import redact_oid, sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()


# Per-(account, db) lock registry. Mirrors the pattern used by
# /api/blast/databases/{db}/shard so a re-clicked Download cannot spawn two
# daemons that race the same metadata.json blob.
#
# Soft-GC: the registry caps at ``_PREPARE_DB_LOCK_REGISTRY_MAX`` entries and
# evicts any currently-unlocked entry when full. This keeps memory bounded
# even for deployments that prepare hundreds of custom databases over time.
_PREPARE_DB_LOCK_REGISTRY: dict[str, threading.Lock] = {}
_PREPARE_DB_LOCK_REGISTRY_GUARD = threading.Lock()
_PREPARE_DB_LOCK_REGISTRY_MAX = 256
# Cooperative shutdown event — checked by ``_poll_copy_completion`` between
# batches so the api sidecar can exit cleanly on SIGTERM instead of waiting
# the full poll interval. The event stays unset in normal operation; the
# main process can call ``_SHUTDOWN_EVENT.set()`` from its lifespan shutdown
# hook (not yet wired — opt-in).
_SHUTDOWN_EVENT = threading.Event()
# An older `update_in_progress=true` marker than this is treated as a crashed
# previous daemon. Large enough to cover the worst-case copy initiation for
# `nt`/`sra` but small enough that real crashes recover same-hour.
_PREPARE_DB_STALE_SECONDS = 2 * 60 * 60
# Copy-status poll cadence and cap. The api sidecar wakes once a minute to
# poll BlobProperties.copy.status for every staged file. Bounded so that an
# orphaned daemon cannot run indefinitely; on hitting the cap we mark the
# update failed with timed_out reason so the SPA shows an honest state.
_COPY_POLL_INTERVAL_SECONDS = 60.0
_COPY_POLL_MAX_SECONDS = 24 * 60 * 60
_COPY_POLL_BATCH_SIZE = max(32, int(os.environ.get("PREPARE_DB_COPY_POLL_BATCH_SIZE", "256")))
# When true (default), prepare-db also stages the snapshot-root taxonomy
# files (`taxdb.btd`, `taxdb.bti`, `taxonomy4blast.sqlite3`) under
# `blast-db/<db>/` so the warmup script finds them in the same folder. Set
# to "false" to skip — useful only when the workload's blastn invocations
# do not request taxonomy columns AND the dataset is v5-only (per-DB
# `.nhi/.ntf/.nto` are still copied via `_list_keys`).
_INCLUDE_SHARED_TAXONOMY = (
    os.environ.get("PREPARE_DB_INCLUDE_TAXONOMY", "true").lower() != "false"
)


def _prepare_db_lock(account_name: str, db_name: str) -> threading.Lock:
    key = f"{account_name.lower()}|{db_name}"
    with _PREPARE_DB_LOCK_REGISTRY_GUARD:
        lock = _PREPARE_DB_LOCK_REGISTRY.get(key)
        if lock is not None:
            return lock
        # Soft GC — evict any free locks if we're at the cap. Locked entries
        # are kept so a live daemon's lock is never silently lost.
        if len(_PREPARE_DB_LOCK_REGISTRY) >= _PREPARE_DB_LOCK_REGISTRY_MAX:
            for stale_key in list(_PREPARE_DB_LOCK_REGISTRY):
                candidate = _PREPARE_DB_LOCK_REGISTRY[stale_key]
                if candidate.acquire(blocking=False):
                    candidate.release()
                    _PREPARE_DB_LOCK_REGISTRY.pop(stale_key, None)
                    if (
                        len(_PREPARE_DB_LOCK_REGISTRY)
                        < _PREPARE_DB_LOCK_REGISTRY_MAX
                    ):
                        break
        lock = threading.Lock()
        _PREPARE_DB_LOCK_REGISTRY[key] = lock
        return lock


def _is_stale_prepare_marker(metadata: dict[str, Any]) -> bool:
    """Return True if the metadata's update_in_progress flag is old enough to
    be treated as a crashed previous daemon."""
    if not metadata.get("update_in_progress"):
        return True
    started = str(metadata.get("update_started_at") or "")
    if not started:
        return True
    try:
        started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        age = (datetime.now(UTC) - started_dt).total_seconds()
    except Exception:
        return True
    return age >= _PREPARE_DB_STALE_SECONDS


def _try_dispatch_aks_mode(
    *,
    body: dict[str, Any],
    caller: CallerIdentity,
    cred: Any,
    sub: str,
    storage_rg: str,
    account_name: str,
    db_name: str,
    mode: str,
) -> dict[str, Any] | None:
    """Issue #7 Phase 1: dispatch the AKS-fanout prepare-db Celery task.

    Returns the HTTP response dict on a successful dispatch. Returns
    ``None`` when ``mode == "auto"`` and AKS is unavailable so the caller
    transparently falls back to the existing server-side path. Raises
    ``HTTPException`` for explicit ``mode == "aks"`` failures (no silent
    fallback per acceptance criterion #3) and for input validation
    problems.
    """
    aks_rg = str(body.get("aks_resource_group") or "").strip()
    cluster_name = str(body.get("cluster_name") or "").strip()
    if mode == "aks" and (not aks_rg or not cluster_name):
        raise HTTPException(
            400,
            "aks_resource_group and cluster_name are required when mode=aks",
        )
    if not aks_rg or not cluster_name:
        # mode=auto with no AKS coords → fall through to server-side.
        return None
    _check(aks_rg, _RE_RG, "aks_resource_group")
    _check(cluster_name, _RE_RG, "cluster_name")

    min_idle_env = os.environ.get("PREPARE_DB_AKS_MIN_IDLE_NODES", "3")
    try:
        min_idle_nodes = max(1, int(min_idle_env))
    except ValueError:
        min_idle_nodes = 3

    # Lazy imports — k8s helpers pull in the Azure SDK transitively.
    from api.services.cluster_health import get_cluster_health
    from api.services.k8s.nodes import k8s_ready_warmup_node_names

    # ARM-level powerState check first — a stopped cluster yields a cleaner
    # error than letting the K8s API time out (~10 s) per dispatch attempt.
    try:
        health = get_cluster_health(cred, sub, aks_rg, cluster_name)
    except Exception as exc:
        LOGGER.debug(
            "cluster_health probe raised for AKS prepare-db dispatch: %s",
            type(exc).__name__,
        )
        health = None
    if health is not None and not health.get("healthy", True):
        reason = health.get("reason")
        power_state = health.get("power_state")
        if mode == "aks":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "aks_unavailable",
                    "message": (
                        "AKS cluster is not Running "
                        f"(reason={reason}, power_state={power_state}). "
                        "Start the cluster from the dashboard or use mode=server-side."
                    ),
                    "ready_nodes": 0,
                    "required_nodes": min_idle_nodes,
                    "cluster_reason": reason,
                    "cluster_power_state": power_state,
                },
            )
        LOGGER.info(
            "prepare_db mode=auto AKS cluster not healthy (%s); falling back",
            reason,
        )
        return None

    try:
        ready_nodes = k8s_ready_warmup_node_names(cred, sub, aks_rg, cluster_name)
    except Exception as exc:
        if mode == "aks":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "aks_unavailable",
                    "message": (
                        "Could not probe AKS cluster for ready workers: "
                        f"{type(exc).__name__}"
                    ),
                    "ready_nodes": 0,
                    "required_nodes": min_idle_nodes,
                },
            ) from exc
        LOGGER.info(
            "prepare_db mode=auto AKS probe failed (%s); falling back to server-side",
            type(exc).__name__,
        )
        return None

    if len(ready_nodes) < min_idle_nodes:
        if mode == "aks":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "aks_unavailable",
                    "message": (
                        f"AKS cluster has {len(ready_nodes)} ready worker nodes; "
                        f"need at least {min_idle_nodes}. Retry once warmup "
                        "scales the pool up or use mode=server-side."
                    ),
                    "ready_nodes": len(ready_nodes),
                    "required_nodes": min_idle_nodes,
                },
            )
        return None

    # RBAC pre-flight: confirm the kubelet identity already carries
    # Storage Blob Data Contributor (or a superset role) on the workload
    # storage account. Without it every pod's `azcopy login --identity`
    # succeeds but every PUT returns 403, surfacing as a generic
    # azcopy exit 3 only ~30 s into the Job. The probe is best-effort —
    # a "probe_failed" outcome (e.g. caller lacks
    # Microsoft.Authorization/roleAssignments/read) falls through so the
    # operator still gets the existing post-dispatch error path.
    from api.services.k8s.prepare_db_preflight import kubelet_storage_blob_data_access

    rbac = kubelet_storage_blob_data_access(
        cred,
        subscription_id=sub,
        resource_group=aks_rg,
        cluster_name=cluster_name,
        storage_resource_group=storage_rg,
        storage_account=account_name,
    )
    if rbac.should_block:
        message = (
            "AKS kubelet identity is missing 'Storage Blob Data Contributor' "
            f"on storage account {account_name}; prepare-db pods would 403. "
            "Run warmup (which grants this role) or assign it manually before "
            "retrying."
            if rbac.status == "missing"
            else (
                f"AKS cluster {cluster_name} has no kubelet managed identity "
                "(service-principal mode?); the AKS-fanout prepare-db path "
                "requires a managed identity. Use mode=server-side."
            )
        )
        if mode == "aks":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "kubelet_rbac_missing",
                    "message": message,
                    "kubelet_object_id": rbac.kubelet_object_id,
                    "storage_account": account_name,
                },
            )
        LOGGER.info(
            "prepare_db mode=auto kubelet RBAC pre-flight blocked "
            "(%s); falling back to server-side",
            rbac.status,
        )
        return None
    if rbac.status == "probe_failed":
        LOGGER.info(
            "prepare_db: kubelet RBAC pre-flight indeterminate (%s); "
            "proceeding optimistically",
            rbac.reason,
        )

    from api.routes._blast_shared import _safe_send_task
    from api.routes.storage.common import (
        NcbiAccessDenied,
        _list_keys_with_sizes,
    )
    from api.services.k8s.prepare_db_jobs import (
        DEFAULT_NAMESPACE as _AKS_DEFAULT_NAMESPACE,
    )
    from api.services.k8s.prepare_db_jobs import (
        prepare_db_job_name as _prepare_db_job_name,
    )
    from api.services.storage.data import _blob_service
    from api.services.storage.public_access import ensure_local_storage_access

    access = ensure_local_storage_access(cred, sub, storage_rg, account_name)
    if access.get("action") == "failed":
        LOGGER.warning(
            "prepare_db AKS local-debug auto-open failed for %s: %s",
            account_name,
            access.get("error"),
        )

    try:
        latest_dir = _resolve_latest_dir()
    except Exception as exc:
        LOGGER.warning(
            "NCBI latest-dir lookup failed for AKS prepare-db: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            502, f"could not contact NCBI: {sanitise(str(exc))[:200]}"
        ) from exc

    try:
        sized_keys = _list_keys_with_sizes(latest_dir, db_name)
    except NcbiAccessDenied as exc:
        LOGGER.warning("NCBI 403 listing %s (AKS path)", db_name)
        raise HTTPException(
            502,
            "NCBI bucket refused the request (rate-limited); retry shortly.",
        ) from exc
    except Exception as exc:
        LOGGER.warning(
            "NCBI key list failed for %s (AKS path): %s",
            db_name,
            type(exc).__name__,
        )
        raise HTTPException(
            502,
            f"could not list NCBI database keys: {sanitise(str(exc))[:200]}",
        ) from exc

    if not sized_keys:
        raise HTTPException(
            404,
            (
                f"No files found for database '{db_name}' in NCBI S3 (snapshot: "
                f"{latest_dir})."
            ),
        )

    if _INCLUDE_SHARED_TAXONOMY:
        try:
            tax_keys = shared_taxonomy_keys(latest_dir)
        except Exception as exc:
            LOGGER.warning(
                "shared taxonomy HEAD probe failed (AKS path) %s: %s — "
                "proceeding without taxdb staging",
                latest_dir,
                type(exc).__name__,
            )
            tax_keys = []
        if tax_keys:
            # dedupe with existing list (preserve order)
            existing_keys = {k for k, _ in sized_keys}
            for tk in tax_keys:
                if tk not in existing_keys:
                    sized_keys.append((tk, 0))

    file_keys = [k for k, _ in sized_keys]
    file_sizes = {k: s for k, s in sized_keys if s > 0}

    # Acquire route-side lock + write start metadata. The Celery worker
    # process owns the rest of the lifecycle; cross-process serialisation
    # is the metadata's ``update_in_progress=true`` flag.
    lock = _prepare_db_lock(account_name, db_name)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "another prepare-db is in progress for this DB")

    try:
        blob_svc = _blob_service(cred, account_name)
        container = blob_svc.get_container_client("blast-db")
        previous_metadata, _ = _download_blob_with_etag(container, db_name)
        if previous_metadata.get("update_in_progress") and not _is_stale_prepare_marker(
            previous_metadata
        ):
            raise HTTPException(
                409,
                "prepare-db is already running for this DB (check the dashboard)",
            )

        previous_source_version = str(previous_metadata.get("source_version") or "")
        started_at = datetime.now(UTC).isoformat()
        aks_namespace = os.environ.get(
            "PREPARE_DB_AKS_NAMESPACE", _AKS_DEFAULT_NAMESPACE
        )
        aks_job_name = _prepare_db_job_name(db_name, latest_dir)
        # Persisted so the cancel route + a future reconciler can find the
        # in-flight Job after the api/worker revision restarts (Redis is
        # ephemeral, so the Celery task id alone is not enough).
        aks_job_ref = {
            "subscription_id": sub,
            "resource_group": aks_rg,
            "cluster_name": cluster_name,
            "namespace": aks_namespace,
            "job_name": aks_job_name,
            "configmap_name": aks_job_name,
            "started_at": started_at,
        }

        def _start_mutator(meta: dict[str, Any]) -> dict[str, Any]:
            meta["db_name"] = db_name
            meta["update_in_progress"] = True
            meta["update_started_at"] = started_at
            meta["updating_to_source_version"] = latest_dir
            meta["updating_signature_etag"] = None
            meta.pop("update_error", None)
            meta.pop("update_failed_at", None)
            meta.pop("failed_files", None)
            meta["copy_status"] = {
                "phase": "queued",
                "mode": "aks",
                "total_files": len(file_keys),
            }
            meta["aks_job_ref"] = aks_job_ref
            if previous_source_version and previous_source_version != latest_dir:
                meta["previous_source_version"] = previous_source_version
            return meta

        try:
            _update_metadata(container, db_name, account_name, _start_mutator)
        except Exception as exc:
            LOGGER.warning(
                "AKS prepare-db update-start metadata write failed for %s: %s",
                db_name,
                sanitise(str(exc))[:200],
            )

        max_pods_env = os.environ.get("PREPARE_DB_AKS_MAX_PARALLELISM", "10")
        files_per_pod_env = os.environ.get("PREPARE_DB_AKS_FILES_PER_POD", "50")
        image_env = os.environ.get(
            "PREPARE_DB_AKS_AZCOPY_IMAGE", "mcr.microsoft.com/azure-cli:2.81.0"
        )
        timeout_env = os.environ.get("PREPARE_DB_AKS_JOB_TIMEOUT_SECONDS", str(4 * 60 * 60))
        # New since Phase 1.5: per-shard azcopy buffer concurrency, K8s
        # backoffLimit override, and ttlSecondsAfterFinished. Defaults
        # come from `api.services.k8s.prepare_db_jobs` if the env var
        # is unset / unparsable, so omitting them keeps the existing
        # behaviour.
        azcopy_concurrency_env = os.environ.get(
            "PREPARE_DB_AKS_AZCOPY_CONCURRENCY", ""
        )
        backoff_limit_env = os.environ.get("PREPARE_DB_AKS_BACKOFF_LIMIT", "")
        ttl_seconds_env = os.environ.get("PREPARE_DB_AKS_TTL_SECONDS", "")
        try:
            max_pods = max(1, int(max_pods_env))
        except ValueError:
            max_pods = 10
        try:
            files_per_pod = max(1, int(files_per_pod_env))
        except ValueError:
            files_per_pod = 50
        try:
            active_deadline = max(60, int(timeout_env))
        except ValueError:
            active_deadline = 4 * 60 * 60
        try:
            azcopy_concurrency: int | None = (
                max(1, min(512, int(azcopy_concurrency_env)))
                if azcopy_concurrency_env
                else None
            )
        except ValueError:
            azcopy_concurrency = None
        try:
            backoff_limit: int | None = (
                max(0, int(backoff_limit_env)) if backoff_limit_env else None
            )
        except ValueError:
            backoff_limit = None
        try:
            ttl_seconds_after_finished: int | None = (
                max(60, int(ttl_seconds_env)) if ttl_seconds_env else None
            )
        except ValueError:
            ttl_seconds_after_finished = None

        try:
            task_kwargs: dict[str, Any] = dict(
                job_id=f"prepare-db-aks-{db_name}-{int(time.time())}",
                subscription_id=sub,
                storage_resource_group=storage_rg,
                storage_account=account_name,
                db_name=db_name,
                source_version=latest_dir,
                file_keys=file_keys,
                file_sizes=file_sizes,
                aks_resource_group=aks_rg,
                cluster_name=cluster_name,
                namespace=aks_namespace,
                max_pods=max_pods,
                files_per_pod=files_per_pod,
                image=image_env,
                active_deadline_seconds=active_deadline,
                caller_oid=caller.object_id,
            )
            if azcopy_concurrency is not None:
                task_kwargs["azcopy_concurrency"] = azcopy_concurrency
            if backoff_limit is not None:
                task_kwargs["backoff_limit"] = backoff_limit
            if ttl_seconds_after_finished is not None:
                task_kwargs["ttl_seconds_after_finished"] = ttl_seconds_after_finished
            result = _safe_send_task(
                "api.tasks.storage.prepare_db_via_aks",
                queue="storage",
                **task_kwargs,
            )
        except HTTPException:
            raise
        except Exception:
            # Roll back the start marker so the SPA does not show a
            # phantom in-progress with no live worker.
            try:
                def _rollback(meta: dict[str, Any]) -> dict[str, Any]:
                    meta["update_in_progress"] = False
                    meta["update_error"] = "AKS dispatch failed (enqueue error)"
                    meta["update_failed_at"] = datetime.now(UTC).isoformat()
                    meta["copy_status"] = {
                        "phase": "init_failed",
                        "mode": "aks",
                        "stage": "enqueue",
                    }
                    meta.pop("aks_job_ref", None)
                    return meta

                _update_metadata(container, db_name, account_name, _rollback)
            except Exception as exc:
                LOGGER.debug(
                    "AKS rollback metadata write skipped db=%s: %s",
                    db_name,
                    type(exc).__name__,
                )
            raise
    finally:
        # Release the route-side lock immediately — the worker process has
        # no visibility into this threading.Lock anyway, and the
        # metadata.update_in_progress flag is the real cross-process gate.
        try:
            lock.release()
        except RuntimeError:
            pass

    try:
        from api.services.db.ops_audit import record_db_op

        audit_job_id = record_db_op(
            op="prepare_db_aks",
            caller=caller,
            account_name=account_name,
            db_name=db_name,
            extra={
                "source_version": latest_dir,
                "files_total": len(file_keys),
                "subscription_id": sub,
                "storage_resource_group": storage_rg,
                "aks_resource_group": aks_rg,
                "cluster_name": cluster_name,
                "ready_nodes": len(ready_nodes),
                "task_id": result.id,
            },
        )
    except Exception as exc:
        LOGGER.debug(
            "prepare_db_aks audit record skipped: %s", type(exc).__name__
        )
        audit_job_id = ""

    LOGGER.info(
        "prepare_db mode=aks dispatched oid=%s db=%s task=%s files=%d nodes=%d audit=%s",
        redact_oid(caller.object_id),
        db_name,
        result.id,
        len(file_keys),
        len(ready_nodes),
        audit_job_id or "n/a",
    )

    response: dict[str, Any] = {
        "ok": True,
        "mode": "aks",
        "db_name": db_name,
        "task_id": result.id,
        "instance_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "files_total": len(file_keys),
        "source_version": latest_dir,
        "ready_nodes": len(ready_nodes),
        "async": True,
        "output": (
            f"Dispatched AKS-fanout prepare-db for {db_name} "
            f"({len(file_keys)} files, {len(ready_nodes)} worker nodes). "
            "Poll /api/blast/databases for progress."
        ),
    }
    if access.get("action") in ("opened", "ip_added"):
        response["local_debug_storage_opened"] = {
            "ip": access.get("ip"),
            "previous_public": access.get("previous_public"),
            "off_hint": access.get("off_hint"),
        }
    return response


def _read_db_metadata(container: Any, db_name: str) -> dict[str, Any]:
    metadata_blob = container.get_blob_client(f"{db_name}-metadata.json")
    try:
        from api.services.storage.data import read_metadata_blob_text

        payload = read_metadata_blob_text(
            metadata_blob, max_bytes=4 * 1024 * 1024, label="db-metadata.json"
        )
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        LOGGER.debug("DB metadata read skipped for %s: %s", db_name, type(exc).__name__)
    return {"db_name": db_name}


def _download_blob_with_etag(container: Any, db_name: str) -> tuple[dict[str, Any], str]:
    """Read metadata + its current ETag for optimistic concurrency writes."""
    blob = container.get_blob_client(f"{db_name}-metadata.json")
    try:
        # ``length=`` here caps the download server-side; we accept the
        # `etag` lookup may run twice (download then properties) but the
        # blob is small (<4 MiB) so latency is fine.
        max_bytes = 4 * 1024 * 1024
        stream = blob.download_blob(offset=0, length=max_bytes + 1)
        payload_bytes = stream.readall()
        if len(payload_bytes) > max_bytes:
            LOGGER.warning(
                "db-metadata.json blob exceeds %d bytes (got %d); treating as missing",
                max_bytes,
                len(payload_bytes),
            )
            return {"db_name": db_name}, ""
        payload = payload_bytes.decode("utf-8")
        try:
            parsed = json.loads(payload) if payload else {}
            if not isinstance(parsed, dict):
                parsed = {"db_name": db_name}
        except json.JSONDecodeError:
            parsed = {"db_name": db_name}
        etag = ""
        try:
            etag = getattr(stream, "properties", None).etag if stream.properties else ""  # type: ignore[union-attr]
        except Exception:
            etag = ""
        return parsed, etag or ""
    except ResourceNotFoundError:
        return {"db_name": db_name}, ""
    except Exception as exc:
        LOGGER.debug("DB metadata read skipped for %s: %s", db_name, type(exc).__name__)
        return {"db_name": db_name}, ""


def _write_db_metadata(
    container: Any,
    db_name: str,
    payload: dict[str, Any],
    *,
    account_name: str,
    etag: str | None = None,
) -> str:
    """Write metadata.json. When ``etag`` is set, the upload uses
    ``If-Match`` so a concurrent writer cannot clobber unrelated fields.
    Returns the resulting blob's new ETag (or empty string)."""
    metadata_blob = container.get_blob_client(f"{db_name}-metadata.json")
    kwargs: dict[str, Any] = {"overwrite": True}
    if etag:
        kwargs["etag"] = etag
        kwargs["match_condition"] = MatchConditions.IfNotModified
    result = metadata_blob.upload_blob(
        json.dumps(payload, sort_keys=True).encode("utf-8"),
        **kwargs,
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
        from api.services.blast.db_metadata import notify_blast_db_metadata_changed

        notify_blast_db_metadata_changed(account_name, db_name)
    except Exception as exc:
        LOGGER.debug(
            "db metadata cache invalidate skipped db=%s: %s",
            db_name,
            type(exc).__name__,
        )
    if isinstance(result, dict):
        return str(result.get("etag", "") or "").strip('"')
    return ""


def _update_metadata(
    container: Any,
    db_name: str,
    account_name: str,
    mutator: Any,
    *,
    max_attempts: int = 5,
) -> dict[str, Any]:
    """Read-modify-write metadata.json with ETag retry.

    ``mutator(meta_copy) -> dict`` must be a pure function over the snapshot
    (it should not depend on external state). On 412 Precondition Failed
    (concurrent writer) we re-read and retry up to ``max_attempts``. Final
    fall-back is a blind overwrite — only reached when the concurrent writer
    is itself in a loop, which we accept rather than failing the caller.
    """
    last: dict[str, Any] = {}
    for attempt in range(max_attempts):
        current, etag = _download_blob_with_etag(container, db_name)
        try:
            mutated = mutator(dict(current))
        except Exception:
            raise
        try:
            _write_db_metadata(
                container,
                db_name,
                mutated,
                account_name=account_name,
                etag=etag or None,
            )
            return mutated
        except ResourceModifiedError:
            LOGGER.debug(
                "metadata ETag retry db=%s attempt=%d",
                db_name,
                attempt + 1,
            )
            last = mutated
            continue
        except Exception:
            raise
    # Final blind write — we already exhausted retries; better to land the
    # mutation and accept that a peer's interleaved field may be lost than to
    # leave the metadata silently un-updated.
    _write_db_metadata(container, db_name, last, account_name=account_name)
    return last


def _poll_copy_completion(
    container: Any,
    blob_names: list[str],
    *,
    db_name: str,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Poll each staged blob's copy.status until every copy reaches a terminal
    state. Returns ``{success, failed, aborted, pending, failed_files,
    timed_out, elapsed_seconds}``.

    Why this exists: ``start_copy_from_url`` only ACKs that Azure accepted the
    copy job; the actual transfer happens asynchronously and can fail with
    ``copy.status='failed'`` (NCBI throttling, source 404 mid-snapshot,
    transient network blip). The pre-hardening UI inferred completion from
    the file_count >= 90% heuristic which let partial successes show as
    "Ready" — see Critique items 6 & 7.
    """
    pending = set(blob_names)
    success = 0
    failed = 0
    aborted = 0
    failed_files: list[dict[str, str]] = []
    deadline = time.monotonic() + _COPY_POLL_MAX_SECONDS
    while pending and time.monotonic() < deadline:
        prefix = f"{db_name}/"
        copy_include_supported = True
        try:
            try:
                blobs = container.list_blobs(name_starts_with=prefix, include=["copy"])
            except TypeError:
                copy_include_supported = False
                blobs = container.list_blobs(name_starts_with=prefix)
            copy_by_name = {str(blob.name): getattr(blob, "copy", None) for blob in blobs}
        except Exception as exc:
            LOGGER.debug(
                "copy status batch probe failed db=%s: %s",
                db_name,
                type(exc).__name__,
            )
            copy_by_name = None
        # Iterate in deterministic order so logs stay diff-friendly.
        for name in sorted(pending)[:_COPY_POLL_BATCH_SIZE]:
            if copy_by_name is None:
                continue
            if name not in copy_by_name:
                # The destination blob disappeared (eg an admin deleted the
                # container mid-flight). Surface as a copy failure rather
                # than hanging forever.
                failed += 1
                failed_files.append(
                    {"blob": name, "status": "missing", "reason": "blob not found"}
                )
                pending.discard(name)
                continue
            copy = copy_by_name[name]
            if copy is None and not copy_include_supported:
                try:
                    copy = getattr(
                        container.get_blob_client(name).get_blob_properties(),
                        "copy",
                        None,
                    )
                except ResourceNotFoundError:
                    failed += 1
                    failed_files.append(
                        {"blob": name, "status": "missing", "reason": "blob not found"}
                    )
                    pending.discard(name)
                    continue
                except Exception as exc:
                    LOGGER.debug(
                        "copy status fallback probe failed db=%s blob=%s: %s",
                        db_name,
                        name,
                        type(exc).__name__,
                    )
                    continue
            status = ""
            description = ""
            if copy is not None:
                status = str(getattr(copy, "status", "") or "").lower()
                description = str(getattr(copy, "status_description", "") or "")
            if not status:
                # No copy metadata = the blob existed before this prepare_db
                # call (e.g. shard alias). Treat as success.
                success += 1
                pending.discard(name)
                continue
            if status == "success":
                success += 1
                pending.discard(name)
            elif status == "failed":
                failed += 1
                failed_files.append(
                    {"blob": name, "status": "failed", "reason": description[:200]}
                )
                pending.discard(name)
            elif status == "aborted":
                aborted += 1
                failed_files.append(
                    {"blob": name, "status": "aborted", "reason": description[:200]}
                )
                pending.discard(name)
            # "pending" stays in the set for the next sweep.
        if pending:
            if callable(on_progress):
                try:
                    on_progress(
                        {
                            "success": success,
                            "failed": failed,
                            "aborted": aborted,
                            "pending": len(pending),
                        }
                    )
                except Exception as exc:
                    LOGGER.debug(
                        "copy progress callback raised db=%s: %s",
                        db_name,
                        type(exc).__name__,
                    )
            # Cooperative sleep — Event.wait returns True if shutdown was
            # signalled, in which case we exit the poll loop early so the
            # api sidecar can drain before its grace period ends. The next
            # process restart picks the work up via stale-flag recovery.
            if _SHUTDOWN_EVENT.wait(timeout=_COPY_POLL_INTERVAL_SECONDS):
                LOGGER.info(
                    "copy poll exiting early on shutdown db=%s pending=%d",
                    db_name,
                    len(pending),
                )
                break
    timed_out = bool(pending) and not _SHUTDOWN_EVENT.is_set()
    return {
        "success": success,
        "failed": failed,
        "aborted": aborted,
        "pending": len(pending),
        "failed_files": failed_files[:50],
        "timed_out": timed_out,
        "elapsed_seconds": int(time.monotonic() - (deadline - _COPY_POLL_MAX_SECONDS)),
    }


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

    Hardening:
      * Per-(account, db) lock — a re-clicked Download returns 409 instead
        of spawning a second daemon that races the metadata.json blob.
      * Stale-flag recovery — a previous daemon's ``update_in_progress`` flag
        older than 2 h is treated as crashed and the new call proceeds.
      * Copy.status polling — every staged blob is polled until terminal
        status. Partial successes record ``failed_files`` and DO NOT promote
        ``source_version`` (atomic generation cut-over).
      * ETag-aware metadata writes — concurrent writers (shard daemon, warmup
        task) cannot clobber unrelated fields via blind read-modify-write.
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

    mode_default = os.environ.get("PREPARE_DB_AKS_MODE_DEFAULT", "server-side").strip().lower()
    raw_mode = str(body.get("mode") or mode_default).strip().lower()
    if raw_mode not in {"server-side", "aks", "auto"}:
        raise HTTPException(
            400,
            f"invalid mode: {raw_mode!r} (must be server-side, aks, or auto)",
        )

    cred = get_credential()

    if raw_mode in {"aks", "auto"}:
        outcome = _try_dispatch_aks_mode(
            body=body,
            caller=caller,
            cred=cred,
            sub=sub,
            storage_rg=storage_rg,
            account_name=account_name,
            db_name=db_name,
            mode=raw_mode,
        )
        if outcome is not None:
            return outcome
        # auto + AKS not available → fall through to the existing
        # server-side path. ``aks`` mode with unavailable AKS raises
        # HTTP 409 inside the helper, so we never reach here on that branch.

    # Local-debug only: when LOCAL_DEBUG_AUTO_OPEN_STORAGE=true is set on a
    # developer laptop (NOT in a Container App), open the workload Storage
    # account's public network surface to this caller's IP so the server-side
    # copy below can actually reach the data plane. In production the api
    # sidecar already reaches Storage via the private endpoint and this is a
    # no-op. See api/services/storage/public_access.py and project policy §9.
    from api.services.storage.public_access import ensure_local_storage_access

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
        # Tell apart 403 (NCBI throttling) vs other 5xx so the SPA can show
        # the right hint instead of a generic "could not list".
        from api.routes.storage.common import NcbiAccessDenied

        if isinstance(exc, NcbiAccessDenied):
            LOGGER.warning("NCBI 403 listing %s", db_name)
            raise HTTPException(
                502,
                "NCBI bucket refused the request (rate-limited); retry shortly.",
            ) from exc
        LOGGER.warning("NCBI key list failed for %s: %s", db_name, type(exc).__name__)
        raise HTTPException(
            502, f"could not list NCBI database keys: {sanitise(str(exc))[:200]}"
        ) from exc

    if not all_keys:
        raise HTTPException(
            404,
            (
                f"No files found for database '{db_name}' in NCBI S3 (snapshot: "
                f"{latest_dir}). The DB may be FTP-only or NCBI may still be "
                "publishing the snapshot — wait a few minutes and retry."
            ),
        )

    # Append the snapshot-root taxonomy files (`taxdb.btd`, `taxdb.bti`,
    # `taxonomy4blast.sqlite3`) so the warmup script finds them inside the
    # per-DB folder. Without these, `blastn -outfmt '... staxid ssciname
    # scomname sblastname'` returns N/A for the taxonomy columns and v4
    # DBs miss their entire taxonomy lookup path. NCBI 502 / 403 here is
    # logged but non-fatal — the rest of the DB still goes through; the
    # warmup script already tolerates a `TAXDB_SKIP` outcome.
    if _INCLUDE_SHARED_TAXONOMY:
        try:
            tax_keys = shared_taxonomy_keys(latest_dir)
        except Exception as exc:
            LOGGER.warning(
                "shared taxonomy HEAD probe failed for snapshot %s: %s — "
                "proceeding without taxdb staging",
                latest_dir,
                type(exc).__name__,
            )
            tax_keys = []
        if tax_keys:
            # dict.fromkeys preserves order and drops accidental duplicates
            # (e.g. a hypothetical custom db_name="taxdb" would otherwise
            # have its volumes listed twice — once by _list_keys, once by
            # the shared-taxonomy probe).
            all_keys = list(dict.fromkeys(list(all_keys) + tax_keys))
            LOGGER.info(
                "prepare_db staging %d shared taxonomy files for db=%s: %s",
                len(tax_keys),
                db_name,
                ", ".join(key.rsplit("/", 1)[-1] for key in tax_keys),
            )

    # Acquire the per-(account, db) lock BEFORE building any clients so a
    # 409 is fast for the second-clicker. If a stale flag is in the metadata
    # we clear it and proceed; otherwise we refuse so two daemons can never
    # race the metadata blob simultaneously.
    lock = _prepare_db_lock(account_name, db_name)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "another prepare-db is in progress for this DB")

    # Build the destination container client. The api sidecar reaches the
    # storage account over the private endpoint via the shared MI; no SAS
    # is involved, no public network toggle is performed. ``_blob_service``
    # returns a pooled BlobServiceClient keyed by (credential id, account)
    # so we don't pay credential re-validation per request.
    try:
        from api.services.storage.data import _blob_service

        blob_svc = _blob_service(cred, account_name)
        container = blob_svc.get_container_client("blast-db")

        previous_metadata, _ = _download_blob_with_etag(container, db_name)
        if previous_metadata.get("update_in_progress") and not _is_stale_prepare_marker(
            previous_metadata
        ):
            # Another process (different api replica, peer worker) is mid-flight.
            # Release our in-process lock; the live writer keeps the metadata
            # flag and the SPA poll will show the existing in-progress state.
            lock.release()
            raise HTTPException(
                409,
                "prepare-db is already running for this DB (check the dashboard)",
            )

        previous_source_version = str(previous_metadata.get("source_version") or "")
        started_at = datetime.now(UTC).isoformat()

        def _start_mutator(meta: dict[str, Any]) -> dict[str, Any]:
            meta["db_name"] = db_name
            meta["update_in_progress"] = True
            meta["update_started_at"] = started_at
            meta["updating_to_source_version"] = latest_dir
            meta["updating_signature_etag"] = None
            meta.pop("update_error", None)
            meta.pop("update_failed_at", None)
            meta.pop("failed_files", None)
            meta.pop("copy_status", None)
            if previous_source_version and previous_source_version != latest_dir:
                meta["previous_source_version"] = previous_source_version
            return meta

        try:
            _update_metadata(container, db_name, account_name, _start_mutator)
        except Exception as exc:
            LOGGER.warning(
                "prepare_db update-start metadata write failed for %s: %s",
                db_name,
                sanitise(str(exc))[:200],
            )
    except HTTPException:
        raise
    except Exception:
        lock.release()
        raise

    def _do_copies() -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Build a closure-local handle to the container so the daemon survives
        # the request scope going away.
        try:
            from azure.storage.blob import BlobServiceClient as _BSC

            from api.services.storage.endpoint import blob_account_url as _url

            local_svc = _BSC(account_url=_url(account_name), credential=cred)
            local_container = local_svc.get_container_client("blast-db")

            def _copy_one(key: str) -> tuple[str, str]:
                source_url = f"{_NCBI_S3_BASE}/{key}"
                # Layout MUST match `elastic-blast` upstream
                # `util.py:get_blastdb_info`: files live in a subfolder named
                # after the DB (`blast-db/<db>/<files>`). A flat layout makes
                # `azcopy list` of the parent return wrong results and
                # elastic-blast reports "BLAST database … was not found".
                file_basename = key.split("/")[-1]
                blob_name = f"{db_name}/{file_basename}"
                try:
                    local_container.get_blob_client(blob_name).start_copy_from_url(
                        source_url
                    )
                    return (blob_name, "started")
                except Exception as e:
                    if "PendingCopyOperation" in str(e):
                        return (blob_name, "skipped")
                    LOGGER.warning(
                        "Copy failed for %s: %s",
                        blob_name,
                        sanitise(str(e))[:200],
                    )
                    return (blob_name, "error")

            started = skipped = errors = 0
            staged_blob_names: list[str] = []
            with ThreadPoolExecutor(max_workers=20) as ex:
                futures = {ex.submit(_copy_one, k): k for k in all_keys}
                for f in as_completed(futures):
                    name, status = f.result()
                    if status in ("started", "skipped"):
                        staged_blob_names.append(name)
                    if status == "started":
                        started += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        errors += 1

            LOGGER.info(
                "DB prepare initiation done for %s: %d started, %d skipped, %d errors",
                db_name,
                started,
                skipped,
                errors,
            )

            successful_inits = started + skipped
            if errors > 0 or successful_inits <= 0:
                # Initiation failed for at least one file — record honest
                # state and DO NOT promote source_version. The previous
                # generation (if any) stays the active version.
                def _init_fail(meta: dict[str, Any]) -> dict[str, Any]:
                    meta["db_name"] = db_name
                    meta["update_in_progress"] = False
                    meta["updating_to_source_version"] = latest_dir
                    meta["update_error"] = (
                        f"copy initiation failed for {errors} of {len(all_keys)} files"
                    )
                    meta["update_failed_at"] = datetime.now(UTC).isoformat()
                    meta["copy_status"] = {
                        "phase": "init_failed",
                        "initiation_started": started,
                        "initiation_skipped": skipped,
                        "initiation_errors": errors,
                        "total_files": len(all_keys),
                    }
                    return meta

                try:
                    _update_metadata(local_container, db_name, account_name, _init_fail)
                except Exception as exc:
                    LOGGER.warning(
                        "prepare_db init-failure metadata write failed for %s: %s",
                        db_name,
                        sanitise(str(exc))[:200],
                    )
                return

            # Phase 2: poll each staged blob's copy.status until terminal.
            def _record_progress(snapshot: dict[str, int]) -> None:
                def _mut(meta: dict[str, Any]) -> dict[str, Any]:
                    meta["copy_status"] = {
                        "phase": "copying",
                        "total_files": len(all_keys),
                        **snapshot,
                    }
                    return meta

                try:
                    _update_metadata(local_container, db_name, account_name, _mut)
                except Exception as exc:
                    LOGGER.debug(
                        "copy progress metadata write skipped db=%s: %s",
                        db_name,
                        type(exc).__name__,
                    )

            poll_summary = _poll_copy_completion(
                local_container,
                staged_blob_names,
                db_name=db_name,
                on_progress=_record_progress,
            )

            all_succeeded = (
                poll_summary["failed"] == 0
                and poll_summary["aborted"] == 0
                and not poll_summary["timed_out"]
                and poll_summary["success"] >= len(staged_blob_names)
            )

            if not all_succeeded:
                # Partial completion or timeout — DO NOT promote source_version.
                def _partial(meta: dict[str, Any]) -> dict[str, Any]:
                    meta["db_name"] = db_name
                    meta["update_in_progress"] = False
                    if poll_summary["timed_out"]:
                        reason = (
                            f"timed out polling copy.status after "
                            f"{_COPY_POLL_MAX_SECONDS}s; "
                            f"{poll_summary['pending']} blobs still pending"
                        )
                    else:
                        reason = (
                            f"{poll_summary['failed']} failed, "
                            f"{poll_summary['aborted']} aborted of "
                            f"{len(staged_blob_names)} staged"
                        )
                    meta["update_error"] = reason
                    meta["update_failed_at"] = datetime.now(UTC).isoformat()
                    meta["failed_files"] = poll_summary["failed_files"]
                    meta["copy_status"] = {
                        "phase": "partial",
                        "total_files": len(all_keys),
                        "success": poll_summary["success"],
                        "failed": poll_summary["failed"],
                        "aborted": poll_summary["aborted"],
                        "pending": poll_summary["pending"],
                        "timed_out": poll_summary["timed_out"],
                    }
                    return meta

                try:
                    _update_metadata(local_container, db_name, account_name, _partial)
                except Exception as exc:
                    LOGGER.warning(
                        "prepare_db partial-completion metadata write failed for %s: %s",
                        db_name,
                        sanitise(str(exc))[:200],
                    )
                return

            # Phase 3: all copies succeeded — auto-shard, then promote.
            from api.services.db.sharding import (
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
                        upload_shard_set(cred, account_name, db_name, n, volumes)
                        shard_sets_created.append(n)
                    except Exception as exc:
                        LOGGER.warning(
                            "shard set N=%d failed for %s: %s",
                            n,
                            db_name,
                            sanitise(str(exc))[:200],
                        )
            except LookupError:
                LOGGER.info("auto-shard skipped for %s: no volumes detected", db_name)
            except Exception as exc:
                LOGGER.warning(
                    "auto-shard failed for %s: %s",
                    db_name,
                    sanitise(str(exc))[:200],
                )

            # Per-DB ETag signature so /databases/check-updates can render
            # accurate per-DB update detection without bouncing latest-dir.
            # Composite signature samples N md5 ETags so multi-volume DBs
            # detect updates that touched only later shards.
            new_signature_etag: str | None = None
            new_composite_signature: str | None = None
            try:
                from api.services.ncbi_catalogue import database_update_signature

                sig = database_update_signature(db_name)
                new_signature_etag = sig.get("signature_etag")
                new_composite_signature = sig.get("composite_signature")
            except Exception as exc:
                LOGGER.debug(
                    "post-prepare signature lookup skipped db=%s: %s",
                    db_name,
                    type(exc).__name__,
                )

            def _promote(meta: dict[str, Any]) -> dict[str, Any]:
                meta["db_name"] = db_name
                meta["source_version"] = latest_dir
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
                    "total_files": len(all_keys),
                    "success": poll_summary["success"],
                    "failed": 0,
                    "aborted": 0,
                    "pending": 0,
                    "timed_out": False,
                }
                if previous_source_version and previous_source_version != latest_dir:
                    meta["updated_from_source_version"] = previous_source_version
                if shard_sets_created:
                    meta["sharded"] = True
                    meta["shard_sets"] = shard_sets_created
                    meta["shard_source_version"] = latest_dir
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
                        and oracle.get("source_version") != latest_dir
                    ):
                        oracle["status"] = "stale"
                    meta["db_order_oracle"] = oracle
                return meta

            try:
                _update_metadata(local_container, db_name, account_name, _promote)
            except Exception as exc:
                LOGGER.warning(
                    "prepare_db promotion metadata write failed for %s: %s",
                    db_name,
                    sanitise(str(exc))[:200],
                )
        finally:
            lock.release()

    Thread(target=_do_copies, daemon=True, name=f"prepare-db-{db_name}").start()

    # Audit — recorded after the lock + thread are set up so a 409 / 502
    # earlier in the route does NOT leak a phantom "started" event.
    try:
        from api.services.db.ops_audit import record_db_op

        audit_job_id = record_db_op(
            op="prepare_db",
            caller=caller,
            account_name=account_name,
            db_name=db_name,
            extra={
                "source_version": latest_dir,
                "files_total": len(all_keys),
                "subscription_id": sub,
                "storage_resource_group": storage_rg,
            },
        )
    except Exception as exc:
        LOGGER.debug("prepare_db audit record skipped: %s", type(exc).__name__)
        audit_job_id = ""

    LOGGER.info(
        "prepare_db started oid=%s db=%s files=%d source=%s access=%s audit=%s",
        redact_oid(caller.object_id),
        db_name,
        len(all_keys),
        latest_dir,
        access.get("action"),
        audit_job_id or "n/a",
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


@router.post("/prepare-db/{db_name}/cancel")
def prepare_db_cancel(
    db_name: str,
    body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Abort an in-flight prepare-db copy and clear the in-progress marker.

    Calls ``abort_copy`` on every staged blob whose copy is pending, then
    rewrites the metadata blob with ``copy_status.phase = "cancelled"`` so
    the SPA can flip the row back to a clean state without waiting the
    full 2 h stale-recovery window.

    Idempotent — if no copy is in flight, returns a no-op success. Refuses
    (409) when ``copy_status.phase == "completed"`` to avoid undoing a
    successful download.
    """
    sub = str(body.get("subscription_id") or "")
    storage_rg = str(body.get("storage_resource_group") or "")
    account_name = str(body.get("account_name") or "")
    if not all([sub, storage_rg, account_name, db_name]):
        raise HTTPException(
            400,
            "subscription_id, storage_resource_group, account_name path-{db_name} required",
        )
    _check(sub, _RE_SUB, "subscription_id")
    _check(storage_rg, _RE_RG, "storage_resource_group")
    _check(account_name, _RE_STORAGE_ACCOUNT, "account_name")
    _check(db_name, _RE_DB_NAME, "db_name")

    cred = get_credential()
    from api.services.storage.public_access import ensure_local_storage_access

    ensure_local_storage_access(cred, sub, storage_rg, account_name)

    from api.services.storage.data import _blob_service

    blob_svc = _blob_service(cred, account_name)
    container = blob_svc.get_container_client("blast-db")
    meta, _etag = _download_blob_with_etag(container, db_name)
    copy_status = meta.get("copy_status") or {}
    phase = str(copy_status.get("phase") or "") if isinstance(copy_status, dict) else ""
    if phase == "completed" and not meta.get("update_in_progress"):
        raise HTTPException(409, f"database {db_name} download already completed")

    # AKS-fanout cancel path: if the dispatch recorded `aks_job_ref`, delete
    # the K8s Job + ConfigMap. The azcopy upload pods write via
    # PUT-Block (not start_copy_from_url) so the blob `abort_copy` loop
    # below is a no-op for AKS mode — the Job has to go to actually stop
    # the data flow.
    aks_job_deleted: dict[str, Any] | None = None
    aks_job_ref_raw = meta.get("aks_job_ref")
    aks_job_ref = aks_job_ref_raw if isinstance(aks_job_ref_raw, dict) else None
    if aks_job_ref:
        try:
            from api.services.k8s.prepare_db_jobs import delete_prepare_db_job

            aks_job_deleted = delete_prepare_db_job(
                cred,
                str(aks_job_ref.get("subscription_id") or sub),
                str(aks_job_ref.get("resource_group") or ""),
                str(aks_job_ref.get("cluster_name") or ""),
                namespace=str(aks_job_ref.get("namespace") or "default"),
                job_name=str(aks_job_ref.get("job_name") or ""),
                configmap_name=str(
                    aks_job_ref.get("configmap_name")
                    or aks_job_ref.get("job_name")
                    or ""
                )
                or None,
            )
        except Exception as exc:
            LOGGER.warning(
                "prepare_db_cancel AKS Job delete failed db=%s job=%s: %s",
                db_name,
                aks_job_ref.get("job_name"),
                type(exc).__name__,
            )
            aks_job_deleted = {"status": "error", "error": type(exc).__name__}

    # Walk container for blobs under {db_name}/ and abort any pending copies.
    aborted = 0
    skipped = 0
    errors = 0
    copy_include_supported = True
    try:
        blobs = container.list_blobs(name_starts_with=f"{db_name}/", include=["copy"])
    except TypeError:
        copy_include_supported = False
        blobs = container.list_blobs(name_starts_with=f"{db_name}/")
    for blob in blobs:
        try:
            bc = container.get_blob_client(blob.name)
            copy_props = getattr(blob, "copy", None)
            if copy_props is None and not copy_include_supported:
                copy_props = getattr(bc.get_blob_properties(), "copy", None)
            status = str(getattr(copy_props, "status", "") or "").lower()
            cid = str(getattr(copy_props, "id", "") or "")
            if status == "pending" and cid:
                bc.abort_copy(cid)
                aborted += 1
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            LOGGER.debug(
                "prepare_db_cancel abort_copy failed db=%s blob=%s: %s",
                db_name,
                blob.name,
                type(exc).__name__,
            )

    def _cancel_mutator(meta_in: dict[str, Any]) -> dict[str, Any]:
        meta_in["db_name"] = db_name
        meta_in["update_in_progress"] = False
        _cancel_oid = redact_oid(caller.object_id) or "caller"
        meta_in["update_error"] = (
            f"cancelled by {_cancel_oid}: aborted {aborted} pending copies "
            f"({skipped} skipped, {errors} errors)"
        )
        meta_in["update_failed_at"] = datetime.now(UTC).isoformat()
        cs: dict[str, Any] = {
            "phase": "cancelled",
            "aborted": aborted,
            "skipped": skipped,
            "errors": errors,
        }
        if aks_job_ref:
            cs["mode"] = "aks"
            cs["aks_job_deleted"] = aks_job_deleted or {"status": "unknown"}
        meta_in["copy_status"] = cs
        meta_in.pop("updating_to_source_version", None)
        meta_in.pop("aks_job_ref", None)
        return meta_in

    try:
        _update_metadata(container, db_name, account_name, _cancel_mutator)
    except Exception as exc:
        LOGGER.warning(
            "prepare_db_cancel metadata write failed for %s: %s",
            db_name,
            type(exc).__name__,
        )

    try:
        from api.services.db.ops_audit import record_db_op

        record_db_op(
            op="prepare_db_cancel",
            caller=caller,
            account_name=account_name,
            db_name=db_name,
            extra={"aborted": aborted, "skipped": skipped, "errors": errors},
        )
    except Exception as exc:
        LOGGER.debug("cancel audit record skipped: %s", type(exc).__name__)

    LOGGER.info(
        "prepare_db_cancel oid=%s db=%s aborted=%d skipped=%d errors=%d",
        redact_oid(caller.object_id),
        db_name,
        aborted,
        skipped,
        errors,
    )
    return {
        "ok": True,
        "db_name": db_name,
        "aborted": aborted,
        "skipped": skipped,
        "errors": errors,
        "aks_job_deleted": aks_job_deleted,
    }


@router.post("/prepare-db/{db_name}/delete")
def prepare_db_delete(
    db_name: str,
    body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Permanently remove a staged BLAST database from the ``blast-db``
    container.

    Closes the resource lifecycle: a database the user created with
    prepare-db can be deleted again to reclaim Storage and reset the row to
    the "not downloaded" state. Steps, in order:

    1. Read metadata. Refuse (409) when a copy is genuinely in flight
       (``copy_status.phase`` in ``{queued, copying}`` or
       ``update_in_progress``) — the caller must Cancel first so we never
       race a live azcopy/server-side copy. A ``partial`` / ``cancelled`` /
       ``init_failed`` / ``completed`` database is safe to delete.
    2. Delete any leftover AKS-fanout Job + ConfigMap recorded in
       ``aks_job_ref`` (idempotent; a 404 is success).
    3. Delete every blob under ``{db_name}/`` (the shards + taxonomy) in
       batches of up to 256, and — only when every shard was removed —
       finally the ``{db_name}-metadata.json`` blob, so the DB vanishes
       from ``list_databases``. If any shard delete failed (``errors > 0``)
       the metadata blob is **kept** so the row stays visible and
       re-deletable instead of leaking orphan blobs; the response carries
       ``partial=True`` in that case.

    Idempotent — deleting an absent database returns a no-op success.
    """
    sub = str(body.get("subscription_id") or "")
    storage_rg = str(body.get("storage_resource_group") or "")
    account_name = str(body.get("account_name") or "")
    if not all([sub, storage_rg, account_name, db_name]):
        raise HTTPException(
            400,
            "subscription_id, storage_resource_group, account_name path-{db_name} required",
        )
    _check(sub, _RE_SUB, "subscription_id")
    _check(storage_rg, _RE_RG, "storage_resource_group")
    _check(account_name, _RE_STORAGE_ACCOUNT, "account_name")
    _check(db_name, _RE_DB_NAME, "db_name")

    cred = get_credential()
    from api.services.storage.public_access import ensure_local_storage_access

    ensure_local_storage_access(cred, sub, storage_rg, account_name)

    from api.services.storage.data import _blob_service

    blob_svc = _blob_service(cred, account_name)
    container = blob_svc.get_container_client("blast-db")
    meta, _etag = _download_blob_with_etag(container, db_name)
    copy_status = meta.get("copy_status") or {}
    phase = str(copy_status.get("phase") or "") if isinstance(copy_status, dict) else ""
    # Guard: never delete under a live copy. The caller must Cancel first,
    # which deletes the AKS Job and rewrites phase to "cancelled" — only then
    # is a Delete safe. update_in_progress covers the generation-swap update
    # path where copy_status may still read "completed" for the old gen.
    if phase in {"queued", "copying"} or meta.get("update_in_progress"):
        raise HTTPException(
            409,
            f"database {db_name} has a copy in progress; cancel it before deleting",
        )

    # AKS-fanout cleanup: remove any Job + ConfigMap still referenced. Best
    # effort — a missing Job (already GC'd by TTL) is fine.
    aks_job_deleted: dict[str, Any] | None = None
    aks_job_ref_raw = meta.get("aks_job_ref")
    aks_job_ref = aks_job_ref_raw if isinstance(aks_job_ref_raw, dict) else None
    if aks_job_ref:
        try:
            from api.services.k8s.prepare_db_jobs import delete_prepare_db_job

            aks_job_deleted = delete_prepare_db_job(
                cred,
                str(aks_job_ref.get("subscription_id") or sub),
                str(aks_job_ref.get("resource_group") or ""),
                str(aks_job_ref.get("cluster_name") or ""),
                namespace=str(aks_job_ref.get("namespace") or "default"),
                job_name=str(aks_job_ref.get("job_name") or ""),
                configmap_name=str(
                    aks_job_ref.get("configmap_name")
                    or aks_job_ref.get("job_name")
                    or ""
                )
                or None,
            )
        except Exception as exc:
            LOGGER.warning(
                "prepare_db_delete AKS Job delete failed db=%s job=%s: %s",
                db_name,
                aks_job_ref.get("job_name"),
                type(exc).__name__,
            )
            aks_job_deleted = {"status": "error", "error": type(exc).__name__}

    # Delete all shard / taxonomy blobs under {db_name}/, then the metadata
    # blob last so a mid-delete crash still leaves the metadata pointing at a
    # (now-partial) DB the user can re-delete rather than orphaning blobs.
    #
    # Use Azure batch delete (up to 256 blobs per HTTP request) so a large DB
    # like `nt` (~4.8k shard blobs) finishes in a handful of round-trips
    # instead of thousands of serial ones — the serial loop routinely blew
    # past the client request timeout, surfacing a misleading
    # "Request timed out" while the backend was still deleting.
    deleted = 0
    errors = 0

    def _delete_chunk(names: list[str]) -> tuple[int, int]:
        if not names:
            return 0, 0
        ok = 0
        bad = 0
        try:
            responses = container.delete_blobs(
                *names,
                delete_snapshots="include",
                raise_on_any_failure=False,
            )
            for resp in responses:
                status = getattr(resp, "status_code", 202)
                # 202 Accepted = deleted; 404 = already gone (treat as success).
                if status in (200, 202, 404):
                    ok += 1
                else:
                    bad += 1
                    LOGGER.debug(
                        "prepare_db_delete batch entry failed db=%s status=%s",
                        db_name,
                        status,
                    )
        except Exception as exc:
            # One bad batch must not strand the remaining blobs: fall back to
            # per-blob deletes for just this chunk.
            LOGGER.warning(
                "prepare_db_delete batch failed db=%s n=%d: %s; falling back",
                db_name,
                len(names),
                type(exc).__name__,
            )
            for name in names:
                try:
                    container.delete_blob(name, delete_snapshots="include")
                    ok += 1
                except ResourceNotFoundError:
                    ok += 1
                except Exception:
                    bad += 1
        return ok, bad

    chunk: list[str] = []
    # Fully enumerate the shard names BEFORE deleting anything so listing and
    # deleting never interleave — mutating the container mid-pagination could
    # otherwise interact with the server-side continuation marker. The name
    # list is tiny (a few hundred KB even for nt's ~4.8k shards).
    names = [blob.name for blob in container.list_blobs(name_starts_with=f"{db_name}/")]
    for name in names:
        chunk.append(name)
        if len(chunk) >= 256:
            ok, bad = _delete_chunk(chunk)
            deleted += ok
            errors += bad
            chunk = []
    ok, bad = _delete_chunk(chunk)
    deleted += ok
    errors += bad

    metadata_deleted = False
    if errors:
        # Some shard blobs survived (throttling / transient 5xx). Deleting the
        # metadata now would drop the DB from list_databases while orphan blobs
        # linger — an invisible storage leak the user can no longer re-delete
        # from the UI. Keep the metadata so the row stays visible and a repeat
        # Delete (idempotent) can sweep the remainder.
        LOGGER.warning(
            "prepare_db_delete kept metadata db=%s deleted=%d errors=%d "
            "(partial delete; metadata retained for re-delete)",
            db_name,
            deleted,
            errors,
        )
    else:
        try:
            container.delete_blob(
                f"{db_name}-metadata.json", delete_snapshots="include"
            )
            metadata_deleted = True
        except ResourceNotFoundError:
            metadata_deleted = True
        except Exception as exc:
            LOGGER.warning(
                "prepare_db_delete metadata blob delete failed db=%s: %s",
                db_name,
                type(exc).__name__,
            )

    # Drop the merged display-metadata cache so the SPA's DB list reflects the
    # removal on the next read instead of waiting out the TTL.
    try:
        from api.services.blast.db_metadata import notify_blast_db_metadata_changed

        notify_blast_db_metadata_changed(account_name, db_name)
    except Exception as exc:
        LOGGER.debug(
            "prepare_db_delete cache invalidate skipped db=%s: %s",
            db_name,
            type(exc).__name__,
        )

    try:
        from api.services.db.ops_audit import record_db_op

        record_db_op(
            op="prepare_db_delete",
            caller=caller,
            account_name=account_name,
            db_name=db_name,
            extra={
                "deleted": deleted,
                "errors": errors,
                "metadata_deleted": metadata_deleted,
            },
        )
    except Exception as exc:
        LOGGER.debug("delete audit record skipped: %s", type(exc).__name__)

    LOGGER.info(
        "prepare_db_delete oid=%s db=%s deleted=%d errors=%d metadata_deleted=%s",
        redact_oid(caller.object_id),
        db_name,
        deleted,
        errors,
        metadata_deleted,
    )
    return {
        "ok": True,
        "db_name": db_name,
        "deleted": deleted,
        "errors": errors,
        # partial=True whenever the DB is NOT fully gone: either a shard delete
        # failed (errors>0, metadata deliberately kept) OR every shard was
        # removed but the metadata blob delete itself failed (DB still listed).
        # Tells the SPA to warn the user and leave the row re-deletable instead
        # of reporting a clean delete.
        "partial": bool(errors) or not metadata_deleted,
        "metadata_deleted": metadata_deleted,
        "aks_job_deleted": aks_job_deleted,
    }




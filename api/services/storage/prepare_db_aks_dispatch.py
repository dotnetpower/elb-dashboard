"""AKS-fanout dispatch decision + orchestration for the prepare-db route.

Responsibility: Decide whether a prepare-db request should run on the AKS-fanout
path (cluster health / node readiness / kubelet RBAC pre-flight, NCBI key listing,
metadata start-marker write, Celery dispatch) and perform that dispatch, OR signal
that the caller should fall back to the server-side copy path.
Edit boundaries: This is the reusable cloud/data-plane slice extracted out of
`api/routes/storage/prepare_db.py` (issue #24). Keep HTTP status/response *shaping*
in the route; this module raises a domain error (`AksDispatchError`) carrying the
status + detail and returns the success response dict (or ``None`` for fall-through).
Do not import FastAPI / `HTTPException` here.
Key entry points: `try_dispatch_aks_mode`, `AksDispatchError`.
Risky contracts: ``mode == "aks"`` never silently falls back — every failure raises
`AksDispatchError` (acceptance criterion #3). ``mode == "auto"`` returns ``None`` to
fall through to the server-side path. Concurrency is serialised by the per-(account,
db) `prepare_db_lock` registry (same registry the cancel route + tests use); the
metadata ``update_in_progress`` flag is the cross-process gate. ``source_version`` is
written as a start marker only; the worker promotes it on full success. The Celery
task name `api.tasks.storage.prepare_db_via_aks` and the 409 detail ``code`` values
(`aks_unavailable`, `kubelet_rbac_missing`) are SPA-facing contracts.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_route.py
api/tests/test_prepare_db_hardening.py api/tests/test_storage_shared_taxonomy.py`.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from api.auth import CallerIdentity
from api.services.sanitise import redact_oid, sanitise
from api.services.storage.prepare_db_locks import prepare_db_lock as _prepare_db_lock
from api.services.storage.prepare_db_metadata import (
    download_blob_with_etag as _download_blob_with_etag,
)
from api.services.storage.prepare_db_metadata import (
    is_stale_prepare_marker as _is_stale_prepare_marker,
)
from api.services.storage.prepare_db_metadata import (
    update_metadata as _update_metadata,
)

LOGGER = logging.getLogger(__name__)

# Mirror of the route module's flag (env `PREPARE_DB_INCLUDE_TAXONOMY`). The
# AKS path and the server-side path read the same environment variable, so the
# two module-level evaluations are identical at runtime; they are kept separate
# only so each path can be patched independently in tests.
_INCLUDE_SHARED_TAXONOMY = (
    os.environ.get("PREPARE_DB_INCLUDE_TAXONOMY", "true").lower() != "false"
)


class AksDispatchError(Exception):
    """Domain error for AKS-fanout dispatch failures.

    Carries the HTTP ``status_code`` and ``detail`` (str or dict) that the
    route translates into an ``HTTPException``. Keeping this out of the
    FastAPI layer lets the dispatch logic live in the service tier without
    importing ``HTTPException``.
    """

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"AKS dispatch failed (status={status_code})")
        self.status_code = status_code
        self.detail = detail


def try_dispatch_aks_mode(
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
    ``AksDispatchError`` for explicit ``mode == "aks"`` failures (no silent
    fallback per acceptance criterion #3) and for input validation
    problems.
    """
    aks_rg = str(body.get("aks_resource_group") or "").strip()
    cluster_name = str(body.get("cluster_name") or "").strip()
    if mode == "aks" and (not aks_rg or not cluster_name):
        raise AksDispatchError(
            400,
            "aks_resource_group and cluster_name are required when mode=aks",
        )
    if not aks_rg or not cluster_name:
        # mode=auto with no AKS coords → fall through to server-side.
        return None

    from api.routes.storage.common import _RE_RG

    if not _RE_RG.match(aks_rg):
        raise AksDispatchError(
            400, f"invalid aks_resource_group: '{sanitise(str(aks_rg)[:40])}'"
        )
    if not _RE_RG.match(cluster_name):
        raise AksDispatchError(
            400, f"invalid cluster_name: '{sanitise(str(cluster_name)[:40])}'"
        )

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
            raise AksDispatchError(
                409,
                {
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
            raise AksDispatchError(
                409,
                {
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
            raise AksDispatchError(
                409,
                {
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
            raise AksDispatchError(
                409,
                {
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
    from api.routes.storage import common as _common
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
        latest_dir = _common._resolve_latest_dir()
    except Exception as exc:
        LOGGER.warning(
            "NCBI latest-dir lookup failed for AKS prepare-db: %s",
            type(exc).__name__,
        )
        raise AksDispatchError(
            502, f"could not contact NCBI: {sanitise(str(exc))[:200]}"
        ) from exc

    try:
        sized_keys = _common._list_keys_with_sizes(latest_dir, db_name)
    except _common.NcbiAccessDenied as exc:
        LOGGER.warning("NCBI 403 listing %s (AKS path)", db_name)
        raise AksDispatchError(
            502,
            "NCBI bucket refused the request (rate-limited); retry shortly.",
        ) from exc
    except Exception as exc:
        LOGGER.warning(
            "NCBI key list failed for %s (AKS path): %s",
            db_name,
            type(exc).__name__,
        )
        raise AksDispatchError(
            502,
            f"could not list NCBI database keys: {sanitise(str(exc))[:200]}",
        ) from exc

    if not sized_keys:
        raise AksDispatchError(
            404,
            (
                f"No files found for database '{db_name}' in NCBI S3 (snapshot: "
                f"{latest_dir})."
            ),
        )

    if _INCLUDE_SHARED_TAXONOMY:
        try:
            tax_keys = _common.shared_taxonomy_keys(latest_dir)
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

    # Size-based routing (mode=auto only). A tiny DB (e.g. 16S ~18 MB / 15
    # files) finishes a server-to-server async copy near-instantly, while the
    # AKS-fanout path pays a fixed per-Job bootstrap cost (pod scheduling +
    # image pull + `azcopy login` + the 30 s Celery poll granularity) that
    # dwarfs the transfer. Fall through to the server-side path BEFORE taking
    # the lock / writing start metadata, so no state is left behind. Explicit
    # ``mode=aks`` always honours the caller and skips this gate.
    if mode == "auto":
        from api.services.storage.prepare_db_aks_params import (
            prefer_server_side_for_small_db,
        )

        total_bytes = sum(file_sizes.values())
        if prefer_server_side_for_small_db(total_bytes, len(file_keys)):
            LOGGER.info(
                "prepare_db mode=auto db=%s small (%d bytes, %d files); "
                "using server-side path instead of AKS-fanout",
                db_name,
                total_bytes,
                len(file_keys),
            )
            return None

    # Acquire route-side lock + write start metadata. The Celery worker
    # process owns the rest of the lifecycle; cross-process serialisation
    # is the metadata's ``update_in_progress=true`` flag.
    lock = _prepare_db_lock(account_name, db_name)
    if not lock.acquire(blocking=False):
        raise AksDispatchError(409, "another prepare-db is in progress for this DB")

    try:
        blob_svc = _blob_service(cred, account_name)
        container = blob_svc.get_container_client("blast-db")
        previous_metadata, _ = _download_blob_with_etag(container, db_name)
        if previous_metadata.get("update_in_progress") and not _is_stale_prepare_marker(
            previous_metadata
        ):
            raise AksDispatchError(
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

        # Kubernetes Job tuning knobs (env-driven) are resolved by the
        # reusable, side-effect-free service helper — the route keeps only the
        # dispatch + HTTP concerns. Unset / unparsable overrides stay None so
        # the prepare_db_jobs builder defaults apply.
        from api.services.storage.prepare_db_aks_params import resolve_aks_job_limits

        limits = resolve_aks_job_limits()

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
                max_pods=limits.max_pods,
                files_per_pod=limits.files_per_pod,
                image=limits.image,
                active_deadline_seconds=limits.active_deadline_seconds,
                caller_oid=caller.object_id,
            )
            task_kwargs.update(limits.task_overrides())
            result = _safe_send_task(
                "api.tasks.storage.prepare_db_via_aks",
                queue="storage",
                **task_kwargs,
            )
        except AksDispatchError:
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

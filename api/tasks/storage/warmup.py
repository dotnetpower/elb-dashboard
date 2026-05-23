"""`warmup_database` Celery task — prepare + shard + node-warm a BLAST database.

Responsibility: Execute the full per-database warmup pipeline (verify staged blobs,
    auto-shard if needed, schedule node-local warmup Jobs on AKS, wait for readiness).
Edit boundaries: Keep the task body self-contained. Helpers live in `helpers.py`;
    cross-package RBAC/AKS attach calls go through `api.tasks.azure`.
Key entry points: `warmup_database` (Celery task `api.tasks.storage.warmup_database`).
Risky contracts: Task name `api.tasks.storage.warmup_database` is referenced by routes,
    beat schedules, and tests — do not rename. Task must remain idempotent + retry-aware
    and write phase checkpoints via `state_repo` so the SPA can render progress.
Validation: `uv run pytest -q api/tests/test_auto_warmup.py api/tests/test_warmup_route.py
    api/tests/test_warmup_jobs.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Any

from celery import shared_task

import api.tasks.storage as _facade
from api.tasks.storage.helpers import (
    BLAST_DATABASES,
)
from api.tasks.storage.helpers import (
    publish_db_metadata_invalidate as _publish_db_metadata_invalidate,
)
from api.tasks.storage.helpers import (
    wait_for_warmup_jobs as _wait_for_warmup_jobs,
)

LOGGER = logging.getLogger(__name__)


# Indirect through the package so tests can monkeypatch
# `api.tasks.storage.{get_credential,_update_state,_record_task_progress,
# _select_warmup_shard_count,_program_to_mol_type,_build_elb_image}` and have
# the override take effect inside this task. The wrappers are pure passthroughs
# typed with ``Any`` so the surrounding strict-typed task body stays clean.
def _update_state(job_id: str, phase: str, status: str = "running", **extra: Any) -> None:
    _facade._update_state(job_id, phase, status, **extra)


def _record_task_progress(task: Any, phase: str, **meta: Any) -> None:
    _facade._record_task_progress(task, phase, **meta)


def get_credential() -> Any:
    return _facade.get_credential()


def _select_warmup_shard_count(**kwargs: Any) -> int:
    return int(_facade._select_warmup_shard_count(**kwargs))


def _program_to_mol_type(*args: Any, **kwargs: Any) -> str:
    return str(_facade._program_to_mol_type(*args, **kwargs))


def _build_elb_image(*args: Any, **kwargs: Any) -> str:
    return str(_facade._build_elb_image(*args, **kwargs))


@shared_task(
    name="api.tasks.storage.warmup_database",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def warmup_database(
    self: Any,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    storage_account: str,
    database_name: str,
    storage_resource_group: str = "",
    cluster_name: str = "",
    machine_type: str = "",
    num_nodes: int = 0,
    acr_resource_group: str = "",
    acr_name: str = "",
    program: str = "blastn",
    warmup_timeout_seconds: int = 4 * 60 * 60,
    caller_oid: str = "",
    require_all_warmup_nodes: bool = False,
) -> dict[str, Any]:
    """Download a BLAST database from NCBI to the workload storage account.

    Uses the terminal sidecar's `update_blastdb.pl` or `azcopy` to transfer
    BLAST database files into the `blast-db` container. Falls back to direct
    Azure SDK blob operations for the download if the terminal sidecar is
    unavailable.
    """
    _record_task_progress(self, "starting", database=database_name)
    _update_state(job_id, "starting")

    db_info = BLAST_DATABASES.get(database_name)
    if not db_info:
        _update_state(
            job_id,
            "failed",
            status="failed",
            error_code=f"unknown database: {database_name}",
        )
        return {"status": "failed", "error": f"unknown database: {database_name}"}

    _record_task_progress(self, "checking_storage", database=database_name)
    _update_state(job_id, "downloading", status="running")

    try:
        from api.services.storage_data import list_databases

        cred = get_credential()
        databases = list_databases(cred, storage_account)
        match = next((db for db in databases if db.get("name") == database_name), None)
        if not match or int(match.get("file_count") or 0) == 0:
            error = f"database {database_name!r} is not prepared in workload storage"
            _update_state(job_id, "failed", status="failed", error_code=error)
            return {"database": database_name, "status": "failed", "error": error}

        # Auto-shard step — sharding is a hard prereq for warmup (the
        # daemonset vmtouches the per-shard layout files, not the raw
        # NCBI volumes). Doing it here means the user can click
        # "Warmup" on a freshly downloaded DB without having to remember
        # to click the per-chip shard button first.
        #
        # Inline (synchronous) is safe in a Celery worker: there is no
        # HTTP timeout, ensure_shard_sets is idempotent, and the work
        # for even the largest known DB completes in a few minutes.
        already_sharded = bool(match.get("sharded")) and bool(match.get("shard_sets"))
        sharding = "skipped" if already_sharded else "running"
        if not already_sharded:
            _record_task_progress(self, "sharding", database=database_name)
            _update_state(job_id, "sharding", status="running")
            try:
                import json
                from datetime import datetime

                from api.services.db.sharding import (
                    DEFAULT_CONTAINER,
                    ensure_shard_sets,
                )
                from api.services.sanitise import sanitise
                from api.services.storage_data import _blob_service

                # Mark in-progress before the long call so the SPA's
                # chip strip can reflect the auto-shard step.
                svc = _blob_service(cred, storage_account)
                cc = svc.get_container_client(DEFAULT_CONTAINER)
                bc = cc.get_blob_client(f"{database_name}-metadata.json")
                pre: dict[str, Any] = {}
                try:
                    from api.services.storage_data import read_metadata_blob_text

                    pre = json.loads(
                        read_metadata_blob_text(
                            bc, max_bytes=4 * 1024 * 1024, label="db-metadata.json"
                        )
                    )
                except Exception:
                    pre = {"db_name": database_name}
                pre["db_name"] = database_name
                pre["sharding_in_progress"] = True
                pre["sharding_started_at"] = datetime.now(UTC).isoformat()
                pre.pop("sharding_error", None)
                try:
                    bc.upload_blob(json.dumps(pre).encode("utf-8"), overwrite=True)
                    _publish_db_metadata_invalidate(storage_account, database_name)
                except Exception as exc:
                    LOGGER.warning(
                        "warmup_database pre-state write failed db=%s: %s",
                        database_name,
                        type(exc).__name__,
                    )

                summary = ensure_shard_sets(cred, storage_account, database_name)

                # Persist final state so the next /api/blast/databases
                # poll flips the chip to "sharded".
                final: dict[str, Any] = {}
                try:
                    from api.services.storage_data import read_metadata_blob_text

                    final = json.loads(
                        read_metadata_blob_text(
                            bc, max_bytes=4 * 1024 * 1024, label="db-metadata.json"
                        )
                    )
                except Exception:
                    final = {"db_name": database_name}
                final["sharding_in_progress"] = False
                final.pop("sharding_error", None)
                final["sharded"] = bool(summary.get("shard_sets"))
                final["shard_sets"] = summary.get("shard_sets", [])
                if final.get("source_version"):
                    final["shard_source_version"] = final.get("source_version")
                final["sharded_at"] = datetime.now(UTC).isoformat()
                if summary.get("total_bytes"):
                    final.setdefault("total_bytes", summary["total_bytes"])
                for key in ("total_letters", "total_sequences", "bytes_to_cache", "bytes_total"):
                    if summary.get(key):
                        final.setdefault(key, summary[key])
                try:
                    bc.upload_blob(json.dumps(final).encode("utf-8"), overwrite=True)
                    _publish_db_metadata_invalidate(storage_account, database_name)
                except Exception as exc:
                    LOGGER.warning(
                        "warmup_database final-state write failed db=%s: %s",
                        database_name,
                        type(exc).__name__,
                    )
                sharding = "completed"
                match["sharded"] = True
                match["shard_sets"] = summary.get("shard_sets", [])
                for key in (
                    "total_bytes",
                    "total_letters",
                    "total_sequences",
                    "bytes_to_cache",
                    "bytes_total",
                ):
                    if summary.get(key):
                        match[key] = summary[key]
            except Exception as exc:
                LOGGER.warning(
                    "warmup_database auto-shard failed db=%s: %s",
                    database_name,
                    type(exc).__name__,
                )
                # Best-effort error marker so the SPA shows a useful chip.
                try:
                    import json as _json

                    from api.services.db.sharding import DEFAULT_CONTAINER as _DC
                    from api.services.sanitise import sanitise as _sanitise
                    from api.services.storage_data import _blob_service as _bs

                    cred2 = get_credential()
                    svc2 = _bs(cred2, storage_account)
                    bc2 = svc2.get_container_client(_DC).get_blob_client(
                        f"{database_name}-metadata.json"
                    )
                    err_meta: dict[str, Any] = {}
                    try:
                        from api.services.storage_data import read_metadata_blob_text

                        err_meta = _json.loads(
                            read_metadata_blob_text(
                                bc2, max_bytes=4 * 1024 * 1024, label="db-metadata.json"
                            )
                        )
                    except Exception:
                        err_meta = {"db_name": database_name}
                    err_meta["sharding_in_progress"] = False
                    err_meta["sharding_error"] = _sanitise(f"{type(exc).__name__}: {exc}")[:300]
                    bc2.upload_blob(_json.dumps(err_meta).encode("utf-8"), overwrite=True)
                    _publish_db_metadata_invalidate(storage_account, database_name)
                except Exception as marker_exc:
                    LOGGER.debug(
                        "warmup_database shard error marker failed db=%s: %s",
                        database_name,
                        type(marker_exc).__name__,
                    )
                # Sharding is a prereq — don't claim success if it failed.
                err = sanitise(f"{type(exc).__name__}: {exc}")[:300]
                _update_state(
                    job_id,
                    "failed",
                    status="failed",
                    error_code=err,
                )
                return {
                    "database": database_name,
                    "status": "failed",
                    "error": f"auto-shard failed: {err}",
                }

        node_warmup: dict[str, Any] = {"status": "skipped", "reason": "cluster not supplied"}
        if cluster_name:
            _record_task_progress(self, "planning_node_warmup", database=database_name)
            _update_state(job_id, "planning_node_warmup", status="running")
            try:
                from api.services.k8s_monitoring import (
                    k8s_ensure_job_manifests,
                    k8s_ensure_warmup_scripts_configmap,
                    k8s_ready_warmup_node_names,
                    k8s_release_stale_warmup_jobs,
                )
                from api.services.warmup_jobs import build_warmup_job_plan

                nodes = k8s_ready_warmup_node_names(
                    cred, subscription_id, resource_group, cluster_name
                )
                if not nodes:
                    raise RuntimeError("AKS cluster has no Ready warmup nodes")
                actual_node_count = len(nodes)
                if num_nodes and actual_node_count < int(num_nodes):
                    progress = {
                        "database": database_name,
                        "reason": "waiting for all warmup nodes",
                        "requested_node_count": int(num_nodes),
                        "ready_node_count": actual_node_count,
                        "ready_nodes": nodes,
                        "strict": bool(require_all_warmup_nodes),
                    }
                    LOGGER.info(
                        "waiting for all warmup nodes db=%s requested=%s ready=%s strict=%s",
                        database_name,
                        num_nodes,
                        actual_node_count,
                        bool(require_all_warmup_nodes),
                    )
                    if require_all_warmup_nodes:
                        _record_task_progress(self, "waiting_for_warmup_nodes", **progress)
                        _update_state(
                            job_id,
                            "waiting_for_warmup_nodes",
                            status="running",
                            **progress,
                        )
                        return {
                            "database": database_name,
                            "status": "deferred",
                            "sharding": sharding,
                            "phase": "waiting_for_warmup_nodes",
                            "reason": "waiting for all warmup nodes",
                            "node_warmup": {"status": "waiting", **progress},
                            "output": (
                                "Waiting for all requested warmup nodes before creating "
                                "node-local warmup jobs."
                            ),
                        }

                selected_shards = _select_warmup_shard_count(
                    database=match,
                    node_count=actual_node_count,
                    machine_type=machine_type or "Standard_E16s_v5",
                )
                plan = build_warmup_job_plan(
                    db_name=database_name,
                    mol_type=_program_to_mol_type(program, database_name),
                    storage_account=storage_account,
                    num_shards=selected_shards,
                    nodes=nodes,
                    image=_build_elb_image(acr_name),
                    source_version=str(match.get("source_version") or ""),
                )

                role_summary: dict[str, str] = {"status": "skipped"}
                try:
                    from api.tasks.azure import (
                        _attach_acr,
                        _grant_storage_blob_contributor_to_aks,
                    )

                    if acr_name:
                        _attach_acr(
                            cred,
                            subscription_id,
                            resource_group,
                            cluster_name,
                            acr_resource_group or resource_group,
                            acr_name,
                        )
                    _grant_storage_blob_contributor_to_aks(
                        cred,
                        subscription_id,
                        resource_group,
                        cluster_name,
                        storage_resource_group or resource_group,
                        storage_account,
                    )
                    role_summary = {"status": "ensured"}
                except Exception as exc:
                    LOGGER.warning("warmup RBAC ensure failed: %s", exc)
                    role_summary = {"status": "failed", "error": str(exc)[:200]}

                configmap_summary = k8s_ensure_warmup_scripts_configmap(
                    cred,
                    subscription_id,
                    resource_group,
                    cluster_name,
                )
                if configmap_summary.get("status") == "error":
                    raise RuntimeError(f"warmup scripts ConfigMap failed: {configmap_summary}")

                # AKS stop/start replaces VMSS instance names. Any existing
                # `warm-<db>-<shard>` Job pinned to a now-gone node would
                # sit at `succeeded=1` forever (the dashboard correctly
                # marks the DB as `Stale`), and `k8s_ensure_job_manifests`
                # would then skip recreating it because the name still
                # exists. Drop those stale Jobs first so ensure creates
                # fresh ones on the current ready nodes.
                stale_summary = k8s_release_stale_warmup_jobs(
                    cred,
                    subscription_id,
                    resource_group,
                    cluster_name,
                    database_name,
                    nodes,
                    current_source_version=str(match.get("source_version") or ""),
                )

                _record_task_progress(
                    self,
                    "applying_warmup_jobs",
                    database=database_name,
                    shards=selected_shards,
                    nodes=plan.nodes,
                    rbac=role_summary,
                    scripts_configmap=configmap_summary,
                    stale_jobs=stale_summary,
                )
                _update_state(
                    job_id,
                    "applying_warmup_jobs",
                    status="running",
                    shards=selected_shards,
                    nodes=list(plan.nodes),
                    rbac=role_summary,
                    scripts_configmap=configmap_summary,
                    stale_jobs=stale_summary,
                )
                apply_summary = k8s_ensure_job_manifests(
                    cred,
                    subscription_id,
                    resource_group,
                    cluster_name,
                    list(plan.jobs),
                )
                if apply_summary.get("error_count"):
                    raise RuntimeError(f"warmup Job creation failed: {apply_summary['errors'][:2]}")

                wait_summary = _wait_for_warmup_jobs(
                    self,
                    job_id=job_id,
                    credential=cred,
                    subscription_id=subscription_id,
                    resource_group=resource_group,
                    cluster_name=cluster_name,
                    database_name=database_name,
                    expected_jobs=selected_shards,
                    timeout_seconds=max(60, min(int(warmup_timeout_seconds), 24 * 60 * 60)),
                )
                if wait_summary.get("status") != "completed":
                    raise RuntimeError(f"node warmup {wait_summary.get('status')}: {wait_summary}")
                node_warmup = {
                    "status": "completed",
                    "cluster_name": cluster_name,
                    "node_count": actual_node_count,
                    "num_shards": selected_shards,
                    "jobs_created": apply_summary.get("created_count", 0),
                    "jobs_existing": apply_summary.get("existing_count", 0),
                    "nodes_ready": wait_summary.get("nodes_ready", 0),
                }
            except Exception as exc:
                err = str(exc)[:500]
                LOGGER.warning("node-local warmup failed for %s: %s", database_name, err)
                _update_state(job_id, "failed", status="failed", error_code=err)
                return {
                    "database": database_name,
                    "status": "failed",
                    "sharding": sharding,
                    "node_warmup": {"status": "failed", "error": err},
                    "error": err,
                }

        _update_state(job_id, "completed", status="completed")
        return {
            "database": database_name,
            "status": "completed",
            "file_count": match.get("file_count", 0),
            "total_bytes": match.get("total_bytes", 0),
            "source_version": match.get("source_version", "unknown"),
            "sharding": sharding,
            "node_warmup": node_warmup,
            "output": (
                "Database prepared, sharded, and warmed on AKS nodes."
                if node_warmup.get("status") == "completed"
                else (
                    "Database is prepared in workload storage."
                    if sharding == "skipped"
                    else "Database prepared and sharded for warmup."
                )
            ),
        }

    except Exception as exc:
        LOGGER.warning("warmup verification failed: %s", exc)
        _update_state(job_id, "failed", status="failed", error_code=str(exc)[:500])
        return {"database": database_name, "status": "failed", "error": str(exc)[:500]}

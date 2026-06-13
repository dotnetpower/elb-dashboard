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
import os
from datetime import UTC
from typing import Any

from celery import shared_task

import api.tasks.storage as _facade
from api.services.feature_events import TERMINAL_STATUSES, record_feature_event
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
    if status in TERMINAL_STATUSES:
        record_feature_event(
            "warmup",
            status=status,
            job_id=job_id,
            phase=phase,
            error_code=extra.get("error_code"),
            database=extra.get("database"),
        )


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


def _env_int_override(name: str, *, lo: int, hi: int) -> int | None:
    """Read a positive, in-range integer ops override from the environment.

    Lets operators tune the warmup azcopy concurrency / buffer on the worker
    sidecar without a code change. Returns ``None`` (meaning "use the default
    behaviour" — i.e. let azcopy auto-tune) when the variable is unset, empty,
    unparseable, non-positive, OR outside ``[lo, hi]``. A bad value therefore
    degrades gracefully to the default instead of failing the warmup in the
    downstream ``build_warmup_job_plan`` range check.
    """
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < lo or value > hi:
        LOGGER.warning(
            "ignoring out-of-range %s=%s (allowed [%s, %s]); using azcopy auto-tune",
            name,
            value,
            lo,
            hi,
        )
        return None
    return value


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
    force_rewarm: bool = False,
    release_inflight_on_done: bool = False,
) -> dict[str, Any]:
    """Download a BLAST database from NCBI to the workload storage account.

    Uses the terminal sidecar's `update_blastdb.pl` or `azcopy` to transfer
    BLAST database files into the `blast-db` container. Falls back to direct
    Azure SDK blob operations for the download if the terminal sidecar is
    unavailable.

    ``force_rewarm`` is set by the post stop/start auto-warmup reconcile. A
    ``node_disk`` cluster keeps VMSS instance names stable across
    `az aks stop`/`start`, so the pre-stop ``warm-<db>-<shard>`` Jobs are not
    flagged Stale and ``k8s_ensure_job_manifests`` would skip recreating them,
    leaving the node RAM page cache cold. When ``force_rewarm`` is true the
    task drops the database's existing warmup Jobs before ensure so fresh Jobs
    actually run (the on-disk DB survives on node_disk, so only the vmtouch
    re-runs and the download is skipped).

    ``release_inflight_on_done`` is set by the auto-warmup reconcile path: the
    reconcile claimed a Redis in-flight slot before enqueue, and the task drops
    it in its ``finally`` so a deferred/failed warmup is retried on the next
    beat tick instead of waiting out the in-flight TTL.
    """
    _record_task_progress(self, "starting", database=database_name)
    _update_state(job_id, "starting")

    if not storage_resource_group:
        # Workload Storage frequently lives in a different RG than the AKS
        # cluster, so silently falling back to the cluster RG would make the
        # downstream RBAC ensure a no-op (ARM 404) and lead to node-local
        # warmup failures. Fail fast and force the caller to plumb the value.
        _update_state(
            job_id,
            "failed",
            status="failed",
            error_code="missing storage_resource_group",
        )
        return {
            "status": "failed",
            "error": (
                "storage_resource_group is required; the caller must pass "
                "the Storage account's resource group (do not rely on the "
                "AKS cluster RG)"
            ),
        }

    # The authoritative "does this database exist" check is the workload
    # Storage catalog lookup below (list_databases + file_count + copy_status).
    # Do NOT re-introduce a gate against a hardcoded BLAST_DATABASES dict here:
    # NCBI ships far more databases than any static list can track (e.g.
    # 18S_fungal_sequences, ITS_RefSeq_Fungi), and any prepared database must
    # be warmable. Gating on a hardcoded catalog rejected valid prepared DBs
    # with a misleading "unknown database" error.
    _record_task_progress(self, "checking_storage", database=database_name)
    _update_state(job_id, "downloading", status="running")

    try:
        from api.services.storage.data import list_databases

        cred = get_credential()
        databases = list_databases(cred, storage_account)
        match = next((db for db in databases if db.get("name") == database_name), None)
        if not match or int(match.get("file_count") or 0) == 0:
            error = f"database {database_name!r} is not prepared in workload storage"
            _update_state(job_id, "failed", status="failed", error_code=error)
            return {"database": database_name, "status": "failed", "error": error}

        # Defense in depth: a warmup against an in-flight prepare-db produces
        # confusing failures (auto-shard / vmtouch run against incomplete
        # volumes and report cryptic per-pod errors several minutes later).
        # ``copy_status.phase == "completed"`` is the authoritative signal;
        # legacy DBs predating the hardening have no ``copy_status`` and fall
        # back to the existing "file_count > 0" gate above.
        copy_status = match.get("copy_status")
        if isinstance(copy_status, dict):
            phase = str(copy_status.get("phase") or "")
            if phase and phase != "completed":
                success = int(copy_status.get("success") or 0)
                total = int(copy_status.get("total_files") or 0)
                progress = f", {success}/{total} files" if total else ""
                error = (
                    f"database {database_name!r} prepare-db is not complete "
                    f"(phase={phase}{progress})"
                )
                _update_state(job_id, "failed", status="failed", error_code=error)
                return {"database": database_name, "status": "failed", "error": error}
        if match.get("update_in_progress"):
            target = match.get("updating_to_source_version")
            suffix = f" to {target}" if isinstance(target, str) and target else ""
            error = (
                f"database {database_name!r} is updating{suffix}; "
                "retry warmup after the update completes"
            )
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
                from api.services.storage.data import _blob_service

                # Mark in-progress before the long call so the SPA's
                # chip strip can reflect the auto-shard step.
                svc = _blob_service(cred, storage_account)
                cc = svc.get_container_client(DEFAULT_CONTAINER)
                bc = cc.get_blob_client(f"{database_name}-metadata.json")
                pre: dict[str, Any] = {}
                try:
                    from api.services.storage.data import read_metadata_blob_text

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
                    from api.services.storage.data import read_metadata_blob_text

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
                    from api.services.storage.data import _blob_service as _bs

                    cred2 = get_credential()
                    svc2 = _bs(cred2, storage_account)
                    bc2 = svc2.get_container_client(_DC).get_blob_client(
                        f"{database_name}-metadata.json"
                    )
                    err_meta: dict[str, Any] = {}
                    try:
                        from api.services.storage.data import read_metadata_blob_text

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
                from api.services.k8s.monitoring import (
                    k8s_ensure_job_manifests,
                    k8s_ensure_warmup_scripts_configmap,
                    k8s_ready_warmup_node_names,
                    k8s_release_stale_warmup_jobs,
                )
                from api.services.warmup.jobs import build_warmup_job_plan

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
                # Leave azcopy concurrency / buffer UNSET by default so the
                # warmup pod uses azcopy's own CPU-based auto-tuning (16 * vCPU,
                # capped at 300) — a live benchmark measured that ~1.78x faster
                # than the old hard-coded concurrency=16. Operators can still
                # pin them on the worker via WARMUP_AZCOPY_CONCURRENCY /
                # WARMUP_AZCOPY_BUFFER_GB; when set, those values are injected as
                # Job env vars (None means "let azcopy auto-tune").
                azcopy_concurrency = _env_int_override("WARMUP_AZCOPY_CONCURRENCY", lo=1, hi=512)
                azcopy_buffer_gb = _env_int_override("WARMUP_AZCOPY_BUFFER_GB", lo=1, hi=64)
                plan = build_warmup_job_plan(
                    db_name=database_name,
                    mol_type=_program_to_mol_type(program, database_name),
                    storage_account=storage_account,
                    num_shards=selected_shards,
                    nodes=nodes,
                    image=_build_elb_image(acr_name),
                    azcopy_concurrency=azcopy_concurrency,
                    azcopy_buffer_gb=azcopy_buffer_gb,
                    source_version=str(match.get("source_version") or ""),
                )

                role_summary: dict[str, str] = {"status": "skipped"}
                try:
                    from api.tasks.azure import (
                        _attach_acr,
                        _grant_storage_blob_contributor_to_aks,
                    )

                    if acr_name:
                        if not acr_resource_group:
                            # Same fail-fast rationale as storage_resource_group:
                            # ACR commonly lives in a different RG (charter default
                            # is rg-elbacr-01). Silently falling back to the AKS
                            # cluster RG used to hide the misrouted ARM 404.
                            raise RuntimeError(
                                "acr_resource_group is required when acr_name "
                                "is set; the caller must pass the ACR's resource "
                                "group (do not rely on the AKS cluster RG)"
                            )
                        _attach_acr(
                            cred,
                            subscription_id,
                            resource_group,
                            cluster_name,
                            acr_resource_group,
                            acr_name,
                        )
                    _grant_storage_blob_contributor_to_aks(
                        cred,
                        subscription_id,
                        resource_group,
                        cluster_name,
                        storage_resource_group,
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
                #
                # On a `node_disk` cluster the Managed OS disk keeps the
                # instance names stable across stop/start, so the pre-stop
                # Jobs are NOT node-stale and the staleness sweep below would
                # keep them — leaving the RAM page cache cold. A forced
                # re-warm (post stop/start reconcile) therefore drops ALL of
                # the database's warmup Jobs first so ensure recreates them
                # and the vmtouch re-runs.
                force_release_summary: dict[str, Any] | None = None
                if force_rewarm:
                    from api.services.k8s.monitoring import k8s_release_warmup_cache

                    force_release_summary = k8s_release_warmup_cache(
                        cred,
                        subscription_id,
                        resource_group,
                        cluster_name,
                        database_name,
                    )
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
                    force_released_jobs=force_release_summary,
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
                    force_released_jobs=force_release_summary,
                )
                # Partial-failure guard for the forced re-warm: if any old Job
                # survived the release (delete returned a non-2xx/404), its name
                # still exists and `k8s_ensure_job_manifests` below will SKIP
                # recreating it — leaving that shard cold while the task would
                # otherwise report success. Fail loudly so Celery autoretry runs
                # the release again instead of silently warming a subset.
                if (
                    force_release_summary is not None
                    and force_release_summary.get("status") != "released"
                ):
                    raise RuntimeError(
                        "forced warmup Job release did not fully succeed: "
                        f"{force_release_summary.get('errors')}"
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

                # The number of Jobs actually planned is the wait target. For a
                # single-shard DB broadcast across every Ready node,
                # `build_warmup_job_plan` emits one Job *per node* (len(plan.jobs)
                # == node count), not `selected_shards` (== 1). Waiting on
                # `selected_shards` would report the DB "warm" as soon as ONE
                # node finished, leaving the other nodes cold and a search that
                # lands on them failing with "database not found". Wait on the
                # real planned Job count so every node is confirmed warm.
                expected_warm_jobs = max(1, len(plan.jobs))
                wait_summary = _wait_for_warmup_jobs(
                    self,
                    job_id=job_id,
                    credential=cred,
                    subscription_id=subscription_id,
                    resource_group=resource_group,
                    cluster_name=cluster_name,
                    database_name=database_name,
                    expected_jobs=expected_warm_jobs,
                    timeout_seconds=max(60, min(int(warmup_timeout_seconds), 24 * 60 * 60)),
                )
                if wait_summary.get("status") != "completed":
                    raise RuntimeError(f"node warmup {wait_summary.get('status')}: {wait_summary}")
                node_warmup = {
                    "status": "completed",
                    "cluster_name": cluster_name,
                    "node_count": actual_node_count,
                    "num_shards": selected_shards,
                    "jobs_expected": expected_warm_jobs,
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
        LOGGER.warning("warmup_database failed db=%s: %s", database_name, exc)
        _update_state(job_id, "failed", status="failed", error_code=str(exc)[:500])
        return {"database": database_name, "status": "failed", "error": str(exc)[:500]}
    finally:
        # The auto-warmup reconcile claimed a Redis in-flight slot
        # (`autowarmup_inflight_acquire`) before enqueuing this task. Release it
        # here so a deferred/failed warmup is retried on the next beat tick
        # rather than waiting out the TTL. Best-effort: the TTL is the backstop
        # and the manual `/api/warmup/start` path never sets this flag.
        if release_inflight_on_done:
            try:
                from api.services.auto_warmup_reconcile import (
                    autowarmup_inflight_release,
                )

                autowarmup_inflight_release(
                    subscription_id, resource_group, cluster_name, database_name
                )
            except Exception as exc:  # pragma: no cover - best effort cleanup
                LOGGER.debug("auto warm inflight release skipped: %s", type(exc).__name__)

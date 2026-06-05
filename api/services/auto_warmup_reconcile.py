"""Auto warmup reconciliation policy and readiness guards.

Responsibility: Auto warmup reconciliation policy and readiness guards
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `cluster_is_workload_ready`, `expected_warmup_node_count`,
`auto_warmup_ready_gate`, `autowarmup_inflight_key`, `autowarmup_inflight_redis`,
`autowarmup_inflight_acquire`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from api.services import monitoring
from api.services.auto_warmup import (
    AutoWarmupPreference,
    list_auto_warmup_preferences,
    mark_auto_warmup_ready_state,
)
from api.services.storage import data as storage_data

LOGGER = logging.getLogger(__name__)

_AUTOWARMUP_INFLIGHT_TTL_SECONDS = 15 * 60
_AUTOWARMUP_INFLIGHT_PREFIX = "autowarmup:inflight:"


def cluster_is_workload_ready(cluster: dict[str, Any]) -> bool:
    return (
        cluster.get("provisioning_state") == "Succeeded"
        and cluster.get("power_state") == "Running"
        and int(cluster.get("node_count") or 0) > 0
    )


def expected_warmup_node_count(cluster: dict[str, Any], configured_num_nodes: int = 0) -> int:
    if configured_num_nodes > 0:
        return configured_num_nodes
    try:
        return max(0, int(cluster.get("node_count") or 0))
    except (TypeError, ValueError):
        return 0


def auto_warmup_ready_gate(
    credential: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    cluster: dict[str, Any],
    configured_num_nodes: int = 0,
) -> dict[str, Any]:
    expected_node_count = expected_warmup_node_count(cluster, configured_num_nodes)
    if not cluster_is_workload_ready(cluster):
        return {
            "ready": False,
            "phase": "cluster_not_ready",
            "reason": "cluster is not Running/Succeeded yet",
            "expected_node_count": expected_node_count,
            "ready_node_count": 0,
        }
    if expected_node_count <= 0:
        return {
            "ready": True,
            "phase": "ready",
            "expected_node_count": expected_node_count,
            "ready_node_count": 0,
        }
    try:
        from api.services.k8s.monitoring import k8s_ready_warmup_node_names

        ready_nodes = k8s_ready_warmup_node_names(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
        )
    except Exception as exc:
        # Beat reconciler runs every 120 s; a sustained AKS outage would
        # otherwise emit a fresh WARNING per tick. Key by (cluster, exc
        # class) so a new failure class still surfaces; repeats drop to
        # DEBUG.
        from api.services.log_dedup import dedup_log_warning

        dedup_log_warning(
            LOGGER,
            ("auto_warmup_node_readiness", cluster_name, type(exc).__name__),
            "auto warmup node readiness lookup failed cluster=%s expected=%d: %s",
            cluster_name,
            expected_node_count,
            type(exc).__name__,
        )
        return {
            "ready": False,
            "phase": "waiting_for_warmup_nodes",
            "reason": "warmup node readiness is not available yet",
            "expected_node_count": expected_node_count,
            "ready_node_count": 0,
            "error": type(exc).__name__,
        }

    ready_node_count = len(ready_nodes)
    if ready_node_count < expected_node_count:
        return {
            "ready": False,
            "phase": "waiting_for_warmup_nodes",
            "reason": "waiting for all warmup nodes",
            "expected_node_count": expected_node_count,
            "ready_node_count": ready_node_count,
            "ready_nodes": ready_nodes,
        }
    return {
        "ready": True,
        "phase": "ready",
        "reason": "all warmup nodes are Ready",
        "expected_node_count": expected_node_count,
        "ready_node_count": ready_node_count,
        "ready_nodes": ready_nodes,
    }


def autowarmup_inflight_key(
    subscription_id: str, resource_group: str, cluster_name: str, db_name: str
) -> str:
    return (
        f"{_AUTOWARMUP_INFLIGHT_PREFIX}{subscription_id}:{resource_group}:{cluster_name}:{db_name}"
    )


def autowarmup_inflight_redis() -> Any | None:
    try:
        from api.services.redis_clients import get_ops_redis_client

        return get_ops_redis_client(socket_timeout=1.5)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("auto warm inflight redis unavailable: %s", type(exc).__name__)
        return None


def autowarmup_inflight_acquire(
    subscription_id: str, resource_group: str, cluster_name: str, db_name: str
) -> bool:
    """Atomically claim an enqueue slot for one auto-warmup DB."""
    client = autowarmup_inflight_redis()
    if client is None:
        return True
    key = autowarmup_inflight_key(subscription_id, resource_group, cluster_name, db_name)
    try:
        return bool(client.set(key, "1", nx=True, ex=_AUTOWARMUP_INFLIGHT_TTL_SECONDS))
    except Exception as exc:
        LOGGER.debug("auto warm inflight set failed: %s", type(exc).__name__)
        return True


def warmup_status_by_db(databases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in databases:
        name = str(item.get("name") or "")
        if name:
            out[name] = item
    return out


def _latest_ncbi_source_version() -> str:
    try:
        from api.routes.storage.common import _resolve_latest_dir

        return _resolve_latest_dir()
    except Exception as exc:
        LOGGER.warning("auto warm NCBI latest-dir lookup failed: %s", type(exc).__name__)
        return ""


InflightAcquire = Callable[[str, str, str, str], bool]
SendTask = Callable[..., Any]


def _seed_auto_warmup_job_state(
    *,
    job_id: str,
    pref: AutoWarmupPreference,
    db_name: str,
    machine_type: str,
    num_nodes: int,
    program: str,
    expected_node_count: int,
    force_rewarm: bool = False,
) -> bool:
    """Create the `JobState` row for an auto-warmup task before enqueue.

    The `warmup_database` task writes phase checkpoints via
    `state_repo.update()`, which is `get_entity` + patch under the hood.
    Without a pre-seeded row the first two checkpoints raise
    `ResourceNotFoundError` (caught as `KeyError`) and surface as red 404
    Dependency failures in App Insights, while the SPA loses progress
    visibility for the auto-warmup job. Returns True when the row exists
    (created or already present); False on unexpected create failure so
    the caller can still enqueue rather than block reconciliation.
    """

    from datetime import UTC, datetime

    from api.services.state.job_state import JobState
    from api.services.state_repo import get_state_repo

    now = datetime.now(UTC).isoformat(timespec="seconds")
    payload = {
        "subscription_id": pref.subscription_id,
        "resource_group": pref.resource_group,
        "storage_account": pref.storage_account,
        "database_name": db_name,
        "db": db_name,
        "cluster_name": pref.cluster_name,
        "machine_type": machine_type,
        "num_nodes": num_nodes,
        "acr_resource_group": pref.acr_resource_group,
        "acr_name": pref.acr_name,
        "program": program,
        "caller_oid": pref.owner_oid,
        "require_all_warmup_nodes": True,
        "auto_warmup": True,
        "expected_node_count": expected_node_count,
        "force_rewarm": force_rewarm,
    }
    state = JobState(
        job_id=job_id,
        type="warmup",
        status="queued",
        phase="queued",
        owner_oid=pref.owner_oid or None,
        tenant_id=None,
        created_at=now,
        updated_at=now,
        payload=payload,
        db=db_name,
        program=program,
        subscription_id=pref.subscription_id,
        resource_group=pref.resource_group,
        cluster_name=pref.cluster_name,
        storage_account=pref.storage_account,
    )
    try:
        get_state_repo().create(state)
        return True
    except Exception as exc:
        LOGGER.warning(
            "auto warm JobState seed failed job_id=%s db=%s: %s",
            job_id,
            db_name,
            type(exc).__name__,
        )
        return False


def _attach_auto_warmup_task_id(*, job_id: str, task_id: str) -> None:
    """Best-effort attach of the Celery `task_id` to a freshly enqueued auto-warmup job."""

    if not task_id:
        return
    try:
        from api.services.state_repo import get_state_repo

        get_state_repo().update(job_id, task_id=task_id)
    except Exception as exc:
        LOGGER.warning(
            "auto warm task_id attach failed job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )


def reconcile_auto_warmup_preferences(
    *,
    credential: Any,
    send_task: SendTask,
    preference: dict[str, Any] | None = None,
    force: bool = False,
    limit: int = 100,
    inflight_acquire: InflightAcquire = autowarmup_inflight_acquire,
) -> dict[str, Any]:
    """Reconcile Auto warm preferences and enqueue strict warmup tasks."""

    if preference is not None:
        prefs = [AutoWarmupPreference.from_dict(preference)]
    else:
        try:
            prefs = list_auto_warmup_preferences(limit=max(1, min(int(limit or 100), 500)))
        except Exception as exc:
            LOGGER.warning("auto warm preferences list failed: %s", type(exc).__name__)
            return {"status": "list_failed", "error": type(exc).__name__, "reconciled": []}

    reconciled: list[dict[str, Any]] = []
    for pref in prefs:
        result: dict[str, Any] = {
            "cluster_name": pref.cluster_name,
            "databases": pref.databases,
            "enqueued": [],
            "skipped": [],
        }
        try:
            if not pref.enabled or not pref.databases:
                result["status"] = "disabled"
                reconciled.append(result)
                continue
            if not pref.subscription_id or not pref.resource_group or not pref.cluster_name:
                result["status"] = "invalid"
                reconciled.append(result)
                continue

            clusters = monitoring.list_aks_clusters(
                credential, pref.subscription_id, pref.resource_group
            )
            cluster = next(
                (item for item in clusters if item.get("name") == pref.cluster_name), None
            )
            ready_gate = auto_warmup_ready_gate(
                credential,
                subscription_id=pref.subscription_id,
                resource_group=pref.resource_group,
                cluster_name=pref.cluster_name,
                cluster=cluster or {},
                configured_num_nodes=pref.num_nodes,
            )
            if not ready_gate["ready"]:
                mark_auto_warmup_ready_state(pref, ready=False)
                result.update({k: v for k, v in ready_gate.items() if k != "ready"})
                if ready_gate.get("phase") == "waiting_for_warmup_nodes":
                    LOGGER.info(
                        "auto warmup waiting for all warmup nodes cluster=%s expected=%s ready=%s",
                        pref.cluster_name,
                        ready_gate.get("expected_node_count"),
                        ready_gate.get("ready_node_count"),
                    )
                    result["status"] = "waiting_for_warmup_nodes"
                    result["skipped"].append(
                        {
                            "reason": "waiting_for_all_warmup_nodes",
                            "expected_node_count": ready_gate.get("expected_node_count", 0),
                            "ready_node_count": ready_gate.get("ready_node_count", 0),
                        }
                    )
                else:
                    result["status"] = "not_ready"
                reconciled.append(result)
                continue

            warm_status = warmup_status_by_db(
                monitoring.k8s_warmup_status(
                    credential,
                    pref.subscription_id,
                    pref.resource_group,
                    pref.cluster_name,
                ).get("databases", [])
            )
            try:
                downloaded_by_name = {
                    str(item.get("name")): item
                    for item in storage_data.list_databases(credential, pref.storage_account)
                    if item.get("name")
                }
            except Exception as exc:
                LOGGER.warning("auto warm database listing failed: %s", type(exc).__name__)
                downloaded_by_name = {db_name: {"name": db_name} for db_name in pref.databases}
            latest_source_version = (
                _latest_ncbi_source_version()
                if any(item.get("source_version") for item in downloaded_by_name.values())
                else ""
            )

            for db_name in pref.databases:
                db_meta = downloaded_by_name.get(db_name)
                if db_meta is None:
                    result["skipped"].append({"db": db_name, "reason": "not_downloaded"})
                    continue
                db_source_version = str(db_meta.get("source_version") or "")
                if (
                    latest_source_version
                    and db_source_version
                    and db_source_version != latest_source_version
                ):
                    result["skipped"].append(
                        {
                            "db": db_name,
                            "reason": "update_required",
                            "source_version": db_source_version,
                            "latest_version": latest_source_version,
                        }
                    )
                    continue
                warm_meta = warm_status.get(db_name) or {}
                warm_generation = str(warm_meta.get("source_version") or "")
                warm_generations = {
                    str(item) for item in warm_meta.get("source_versions", []) or [] if str(item)
                }
                if db_source_version and warm_generation != db_source_version:
                    warm_meta = {}
                elif len(warm_generations) > 1 or str(warm_meta.get("status") or "") == "Stale":
                    warm_meta = {}
                warm_state = str(warm_meta.get("status") or "")
                # A lingering "Ready"/"Loading" warmup Job normally means the DB
                # is already warm and is skipped. After an `az aks stop`/`start`
                # the node RAM page cache is always cold, yet on a `node_disk`
                # cluster the Managed OS disk keeps VMSS instance names stable,
                # so the pre-stop Jobs are NOT flagged Stale and the DB still
                # reports "Ready". `start_aks` enqueues this reconcile with
                # `force=True` precisely to re-warm in that case, so a forced
                # pass must not skip — it re-enqueues with `force_rewarm` so the
                # warmup task replaces the stale Jobs (download is skipped on
                # node_disk, only the vmtouch re-runs).
                #
                # `force_rewarm` must fire whenever same-generation warmup Jobs
                # are still present (`warm_meta` truthy after the generation /
                # Stale resets above), because `k8s_ensure_job_manifests` skips
                # any Job name that already exists. There are two triggers:
                #   * `force` — the post stop/start re-warm of a still-"Ready"
                #     (or "Loading") DB on node_disk.
                #   * `warm_state == "Failed"` — a prior warmup left Failed Jobs
                #     pinned to LIVE nodes. On node_disk their names are stable,
                #     so the node-staleness sweep keeps them and ensure would
                #     skip recreating forever (the DB stays "Failed" and the
                #     reconcile busy-loops). Force-releasing clears them so the
                #     retry actually re-runs. (Ephemeral self-heals via node
                #     rotation; node_disk needs this explicit release.)
                forced_rewarm = bool(warm_meta) and (force or warm_state == "Failed")
                if warm_state in {"Ready", "Loading"} and not force:
                    result["skipped"].append({"db": db_name, "reason": warm_state})
                    continue
                if not inflight_acquire(
                    pref.subscription_id,
                    pref.resource_group,
                    pref.cluster_name,
                    db_name,
                ):
                    result["skipped"].append({"db": db_name, "reason": "inflight"})
                    continue
                job_id = f"auto-warmup-{pref.cluster_name}-{db_name}-{int(time.time())}"
                machine_type = pref.machine_type or str((cluster or {}).get("node_sku") or "")
                num_nodes = int(ready_gate.get("expected_node_count") or 0)
                program = pref.programs.get(db_name, "blastn")
                _seed_auto_warmup_job_state(
                    job_id=job_id,
                    pref=pref,
                    db_name=db_name,
                    machine_type=machine_type,
                    num_nodes=num_nodes,
                    program=program,
                    expected_node_count=num_nodes,
                    force_rewarm=forced_rewarm,
                )
                task = send_task(
                    "api.tasks.storage.warmup_database",
                    kwargs={
                        "job_id": job_id,
                        "subscription_id": pref.subscription_id,
                        "resource_group": pref.resource_group,
                        "storage_account": pref.storage_account,
                        # The storage account may live in a different RG
                        # than the AKS cluster. Forwarding the preference's
                        # storage_resource_group is required so the warmup
                        # task's RBAC ensure (ARM lookup) targets the right
                        # RG. Omitting it falls back to the cluster RG and
                        # produces a ResourceNotFound that silently skips
                        # the role assignment.
                        "storage_resource_group": pref.storage_resource_group,
                        "database_name": db_name,
                        "cluster_name": pref.cluster_name,
                        "machine_type": machine_type,
                        "num_nodes": num_nodes,
                        "acr_resource_group": pref.acr_resource_group,
                        "acr_name": pref.acr_name,
                        "program": program,
                        "caller_oid": pref.owner_oid,
                        "require_all_warmup_nodes": True,
                        # When a forced (post stop/start) reconcile re-enqueues a
                        # DB that still reports "Ready", the warmup task must drop
                        # the pre-stop Jobs before it recreates them — otherwise
                        # `k8s_ensure_job_manifests` sees the existing names and
                        # no-ops, leaving the RAM cache cold on node_disk.
                        "force_rewarm": forced_rewarm,
                    },
                    queue="storage",
                )
                _attach_auto_warmup_task_id(job_id=job_id, task_id=getattr(task, "id", ""))
                result["enqueued"].append({"db": db_name, "task_id": task.id})

            mark_auto_warmup_ready_state(pref, ready=True, triggered=bool(result["enqueued"]))
            result["status"] = "triggered" if result["enqueued"] else "ready_noop"
        except Exception as exc:
            LOGGER.warning(
                "auto warm reconcile failed cluster=%s: %s",
                pref.cluster_name,
                type(exc).__name__,
            )
            result["status"] = "failed"
            result["error"] = str(exc)[:300]
        reconciled.append(result)

    return {"status": "completed", "clusters": reconciled}

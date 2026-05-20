"""Auto warmup reconciliation policy and readiness guards."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from api.services import monitoring, storage_data
from api.services.auto_warmup import (
    AutoWarmupPreference,
    list_auto_warmup_preferences,
    mark_auto_warmup_ready_state,
)

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
        from api.services.k8s_monitoring import k8s_ready_warmup_node_names

        ready_nodes = k8s_ready_warmup_node_names(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
        )
    except Exception as exc:
        LOGGER.warning(
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
        import redis

        url = os.environ.get("OPS_REDIS_URL", "redis://127.0.0.1:6379/2")
        return redis.Redis.from_url(url, socket_timeout=1.5)
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


def warmup_status_by_db(databases: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in databases:
        name = str(item.get("name") or "")
        status = str(item.get("status") or "")
        if name:
            out[name] = status
    return out


InflightAcquire = Callable[[str, str, str, str], bool]
SendTask = Callable[..., Any]


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
        prefs = list_auto_warmup_preferences(limit=max(1, min(int(limit or 100), 500)))

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
                downloaded = {
                    str(item.get("name"))
                    for item in storage_data.list_databases(
                        credential,
                        pref.storage_account,
                    )
                    if item.get("name")
                }
            except Exception as exc:
                LOGGER.warning("auto warm database listing failed: %s", type(exc).__name__)
                downloaded = set(pref.databases)

            for db_name in pref.databases:
                if db_name not in downloaded:
                    result["skipped"].append({"db": db_name, "reason": "not_downloaded"})
                    continue
                if warm_status.get(db_name) in {"Ready", "Loading"}:
                    result["skipped"].append({"db": db_name, "reason": warm_status[db_name]})
                    continue
                if not inflight_acquire(
                    pref.subscription_id,
                    pref.resource_group,
                    pref.cluster_name,
                    db_name,
                ):
                    result["skipped"].append({"db": db_name, "reason": "inflight"})
                    continue
                task = send_task(
                    "api.tasks.storage.warmup_database",
                    kwargs={
                        "job_id": f"auto-warmup-{pref.cluster_name}-{db_name}-{int(time.time())}",
                        "subscription_id": pref.subscription_id,
                        "resource_group": pref.resource_group,
                        "storage_account": pref.storage_account,
                        "database_name": db_name,
                        "cluster_name": pref.cluster_name,
                        "machine_type": pref.machine_type
                        or str((cluster or {}).get("node_sku") or ""),
                        "num_nodes": int(ready_gate.get("expected_node_count") or 0),
                        "acr_resource_group": pref.acr_resource_group,
                        "acr_name": pref.acr_name,
                        "program": pref.programs.get(db_name, "blastn"),
                        "caller_oid": pref.owner_oid,
                        "require_all_warmup_nodes": True,
                    },
                    queue="storage",
                )
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

"""Celery wrapper for bounded ElasticBLAST Kubernetes runtime garbage collection.

Responsibility: Discover deployed OpenAPI cluster scopes and invoke bounded runtime GC.
Edit boundaries: Cluster discovery and task orchestration only; Kubernetes deletion rules live
in `api.services.k8s.runtime_gc`.
Key entry points: `collect_k8s_runtime_garbage`.
Risky contracts: The task is idempotent, processes at most two clusters per run, and has hard
Celery deadlines. `K8S_RUNTIME_GC_ENABLED=false` disables all mutations. Proven stopped or
missing clusters must be skipped before any Kubernetes API call; an unavailable ARM gate
degrades open through `get_cluster_health`.
Validation: `uv run pytest -q api/tests/test_k8s_runtime_gc.py
api/tests/test_celery_queue_isolation.py`.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from celery import shared_task

LOGGER = logging.getLogger(__name__)
_GC_LOCK_KEY = "blast:k8s-runtime-gc:lock"
_GC_LOCK_TTL_SECONDS = 330
_GC_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _enabled() -> bool:
    return os.environ.get("K8S_RUNTIME_GC_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _cluster_scopes() -> list[dict[str, str]]:
    from api.services.openapi.runtime import (
        get_openapi_runtime_metadata,
        list_openapi_public_base_urls,
    )

    metadata_rows: list[dict[str, Any]] = []
    for payload in list_openapi_public_base_urls():
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if isinstance(metadata, dict):
            metadata_rows.append(metadata)
    runtime_metadata = get_openapi_runtime_metadata()
    if runtime_metadata:
        metadata_rows.append(runtime_metadata)

    scopes: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for metadata in metadata_rows:
        scope = {
            "subscription_id": str(metadata.get("subscription_id") or "").strip(),
            "resource_group": str(metadata.get("resource_group") or "").strip(),
            "cluster_name": str(metadata.get("cluster_name") or "").strip(),
        }
        key = tuple(scope.values())
        if all(key) and key not in seen:
            seen.add(key)
            scopes.append(scope)
        if len(scopes) >= 2:
            break
    return scopes


def _acquire_gc_lock() -> tuple[tuple[Any, str] | None, str]:
    from api.services.redis_clients import get_ops_redis_client

    try:
        client = get_ops_redis_client(
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
        token = uuid.uuid4().hex
        if client.set(_GC_LOCK_KEY, token, nx=True, ex=_GC_LOCK_TTL_SECONDS):
            return (client, token), ""
        return None, "in_progress"
    except Exception as exc:
        LOGGER.warning("k8s runtime GC lock unavailable: %s", type(exc).__name__)
        return None, "lock_unavailable"


def _release_gc_lock(handle: tuple[Any, str]) -> None:
    client, token = handle
    try:
        released = int(client.eval(_GC_RELEASE_LUA, 1, _GC_LOCK_KEY, token) or 0)
        if released == 0:
            LOGGER.warning(
                "k8s runtime GC lock was not released because ownership changed or expired"
            )
    except Exception as exc:
        LOGGER.warning("k8s runtime GC lock release failed: %s", type(exc).__name__)


@shared_task(
    name="api.tasks.blast.collect_k8s_runtime_garbage",
    soft_time_limit=240,
    time_limit=300,
    acks_late=False,
    reject_on_worker_lost=False,
)
def collect_k8s_runtime_garbage() -> dict[str, Any]:
    """Collect a bounded batch of terminal K8s objects for known OpenAPI clusters."""
    if not _enabled():
        return {"skipped": "disabled"}
    lock_handle, lock_reason = _acquire_gc_lock()
    if lock_handle is None:
        return {"skipped": lock_reason}

    try:
        scopes = _cluster_scopes()
        if not scopes:
            return {"skipped": "no_cluster_scope"}

        from api.services import get_credential
        from api.services.cluster_health import CLUSTER_SKIP_REASONS, get_cluster_health
        from api.services.env import env_int
        from api.services.feature_events import record_feature_event
        from api.services.k8s.runtime_gc import collect_runtime_garbage

        credential = get_credential()
        job_retention_seconds = env_int(
            "K8S_RUNTIME_GC_JOB_RETENTION_SECONDS",
            3600,
            minimum=1800,
            maximum=30 * 24 * 60 * 60,
        )
        configmap_retention_seconds = env_int(
            "K8S_RUNTIME_GC_CONFIGMAP_RETENTION_SECONDS",
            14 * 24 * 60 * 60,
            minimum=24 * 60 * 60,
            maximum=180 * 24 * 60 * 60,
        )
        max_deletes = env_int(
            "K8S_RUNTIME_GC_MAX_DELETES",
            200,
            minimum=10,
            maximum=1000,
        )
        total_deadline_seconds = env_int(
            "K8S_RUNTIME_GC_DEADLINE_SECONDS",
            180,
            minimum=60,
            maximum=210,
        )
        per_cluster_deadline = min(90, total_deadline_seconds // len(scopes))
        results: list[dict[str, Any]] = []
        for scope in scopes:
            health = get_cluster_health(credential, **scope)
            skip_reason = health.get("reason")
            if skip_reason in CLUSTER_SKIP_REASONS:
                LOGGER.info(
                    "k8s runtime GC skipped cluster=%s reason=%s power_state=%s",
                    scope["cluster_name"],
                    skip_reason,
                    health.get("power_state"),
                )
                results.append(
                    {
                        "cluster_name": scope["cluster_name"],
                        "skipped": skip_reason,
                        "power_state": health.get("power_state"),
                        "errors": [],
                    }
                )
                continue
            try:
                results.append(
                    collect_runtime_garbage(
                        credential,
                        **scope,
                        job_retention_seconds=job_retention_seconds,
                        configmap_retention_seconds=configmap_retention_seconds,
                        max_deletes=max_deletes,
                        deadline_seconds=per_cluster_deadline,
                    )
                )
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "k8s runtime GC failed cluster=%s error=%s",
                    scope["cluster_name"],
                    type(exc).__name__,
                )
                results.append(
                    {
                        "cluster_name": scope["cluster_name"],
                        "errors": [type(exc).__name__],
                    }
                )
        deleted_jobs = sum(int(item.get("jobs_deleted") or 0) for item in results)
        deleted_configmaps = sum(
            int(item.get("configmaps_deleted") or 0) for item in results
        )
        error_count = sum(len(item.get("errors") or []) for item in results)
        skipped_count = sum(bool(item.get("skipped")) for item in results)
        record_feature_event(
            "k8s_runtime_gc",
            status="failed" if error_count else "completed",
            cluster_count=len(results),
            jobs_deleted=deleted_jobs,
            configmaps_deleted=deleted_configmaps,
            error_count=error_count,
            skipped_count=skipped_count,
            job_retention_seconds=job_retention_seconds,
            configmap_retention_seconds=configmap_retention_seconds,
            max_deletes=max_deletes,
        )
        return {
            "clusters": results,
            "count": len(results),
            "jobs_deleted": deleted_jobs,
            "configmaps_deleted": deleted_configmaps,
            "errors": error_count,
            "skipped": skipped_count,
        }
    finally:
        _release_gc_lock(lock_handle)


__all__ = ("collect_k8s_runtime_garbage",)

"""Recover completed warmups whose ephemeral Celery result disappeared.

Responsibility: Prove a strict all-node warmup completed from current Kubernetes status,
then terminalize its durable JobState and release admission markers.
Edit boundaries: Warmup-only recovery evidence and cleanup; generic stale-row policy remains in
`api.services.db.stale_dbops`.
Key entry points: `recover_completed_warmup`.
Risky contracts: Recovery requires `warming_nodes`, Celery `PENDING`, every expected node Ready,
zero active/failed jobs, a warmup source, and exactly one source generation. Missing evidence
must fail closed without changing JobState or queue admission.
Validation: `uv run pytest -q api/tests/test_stale_dbops_reconcile.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

LOGGER = logging.getLogger(__name__)
_MAX_RECOVERY_SCOPES_PER_TICK = 8


def recover_completed_warmup(
    repo: Any,
    row: Any,
    *,
    celery_state: str | None,
    snapshot_cache: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> bool:
    """Return True after strictly-proven Kubernetes completion is persisted."""
    if celery_state != "PENDING":
        return False
    if str(getattr(row, "type", "") or "") != "warmup":
        return False
    if str(getattr(row, "status", "") or "").lower() not in {
        "queued",
        "pending",
        "running",
    }:
        return False
    if str(getattr(row, "phase", "") or "").lower() != "warming_nodes":
        return False
    payload = getattr(row, "payload", None)
    if not isinstance(payload, Mapping) or payload.get("require_all_warmup_nodes") is not True:
        return False

    subscription_id = str(payload.get("subscription_id") or "")
    resource_group = str(payload.get("resource_group") or "")
    cluster_name = str(payload.get("cluster_name") or payload.get("aks_cluster_name") or "")
    database = str(payload.get("database_name") or payload.get("db") or "")
    expected_nodes = int(payload.get("expected_node_count") or payload.get("num_nodes") or 0)
    if not all((subscription_id, resource_group, cluster_name, database)) or expected_nodes <= 0:
        return False

    scope = (subscription_id, resource_group, cluster_name)
    if (
        snapshot_cache is not None
        and scope not in snapshot_cache
        and len(snapshot_cache) >= _MAX_RECOVERY_SCOPES_PER_TICK
    ):
        return False
    try:
        from api.services import get_credential
        from api.services.auto_warmup_reconcile import warmup_status_by_db
        from api.services.monitoring import k8s_warmup_status

        snapshot = snapshot_cache.get(scope) if snapshot_cache is not None else None
        if snapshot is None:
            snapshot = k8s_warmup_status(
                get_credential(),
                subscription_id,
                resource_group,
                cluster_name,
            )
            if snapshot_cache is not None:
                snapshot_cache[scope] = snapshot
        item = warmup_status_by_db(snapshot.get("databases", []) or []).get(database) or {}
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        LOGGER.info(
            "warmup completion recovery probe unavailable job_id=%s error=%s",
            getattr(row, "job_id", ""),
            type(exc).__name__,
        )
        return False

    sources = {str(value) for value in item.get("sources", []) or []}
    source_versions = {
        str(value) for value in item.get("source_versions", []) or [] if str(value)
    }
    source_version = str(item.get("source_version") or "")
    if source_version:
        source_versions.add(source_version)
    if not (
        str(item.get("status") or "") == "Ready"
        and int(item.get("nodes_ready") or 0) >= expected_nodes
        and int(item.get("total_jobs") or 0) >= expected_nodes
        and int(item.get("nodes_active") or 0) == 0
        and int(item.get("nodes_failed") or 0) == 0
        and "warmup" in sources
        and len(source_versions) == 1
    ):
        return False

    job_id = str(getattr(row, "job_id", "") or "")
    if not job_id:
        return False
    try:
        repo.update(job_id, status="completed", phase="completed", error_code=None)
    except KeyError:
        return False
    try:
        repo.append_history(
            job_id,
            "completed",
            {
                "source": "reconcile_k8s_warmup_completed",
                "reason": "all_expected_warmup_nodes_ready",
                "expected_node_count": expected_nodes,
            },
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        LOGGER.info(
            "warmup completion recovery history skipped job_id=%s error=%s",
            job_id,
            type(exc).__name__,
        )

    try:
        from api.services.aks.execution_admission import clear_active_warmup_job
        from api.services.auto_warmup_reconcile import autowarmup_inflight_release

        clear_active_warmup_job(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            job_id=job_id,
        )
        autowarmup_inflight_release(
            subscription_id,
            resource_group,
            cluster_name,
            database,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        LOGGER.warning(
            "warmup completion recovery marker cleanup deferred job_id=%s error=%s",
            job_id,
            type(exc).__name__,
        )
    return True


__all__ = ["recover_completed_warmup"]

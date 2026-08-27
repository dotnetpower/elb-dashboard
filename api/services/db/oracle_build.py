"""Readiness planning for DB order-oracle builds.

Responsibility: Resolve one immutable oracle build context from Storage DB
    metadata, AKS health, warmup state, and Ready-node observations.
Edit boundaries: Read-only readiness and pure plan shaping; durable claims,
    Kubernetes mutation/polling, Celery dispatch, and HTTP response shaping
    remain in their owning modules.
Key entry points: `resolve_oracle_build_context`,
    `plan_oracle_build_from_snapshots`, `OracleBuildContext`,
    `OracleBuildBlocked`.
Risky contracts: A plan is valid only when Storage, shard layout, and every
    warmup shard share one source generation and every shard maps to a Ready
    node. ARM probe failure degrades open, but a proven stopped/missing cluster
    blocks before Kubernetes access.
Validation: `uv run pytest -q api/tests/test_oracle_build.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

from api.services.db.order_oracle import oracle_layout_fingerprint


class OracleBuildBlocked(RuntimeError):
    """A stable readiness condition prevents an oracle build."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class OracleBuildContext:
    db_name: str
    source_version: str
    layout_schema: int
    layout_fingerprint: str
    identity: str
    shards: tuple[str, ...]
    shard_nodes: tuple[tuple[str, str, str] | tuple[str, str], ...]

    @property
    def expected_parts(self) -> int:
        return len(self.shards)


def _blocked(code: str, message: str, *, status_code: int = 409) -> NoReturn:
    raise OracleBuildBlocked(code, message, status_code=status_code)


def plan_oracle_build_from_snapshots(
    *,
    db_name: str,
    db_meta: dict[str, Any] | None,
    warmup: dict[str, Any],
    ready_nodes: list[str],
    requested_source_version: str = "",
) -> OracleBuildContext:
    """Build a deterministic context from already-fetched observations."""
    if not isinstance(db_meta, dict):
        _blocked(
            "database_not_downloaded",
            f"database {db_name} is not downloaded",
            status_code=404,
        )
    assert db_meta is not None
    source_version = str(db_meta.get("source_version") or "")
    if requested_source_version and source_version != requested_source_version:
        _blocked(
            "source_version_changed",
            f"database {db_name} source_version changed; refresh before building the oracle",
        )
    if db_meta.get("update_in_progress"):
        _blocked(
            "database_updating",
            f"database {db_name} is updating; wait for promotion",
        )
    copy_status = db_meta.get("copy_status")
    if isinstance(copy_status, dict):
        phase = str(copy_status.get("phase") or "")
        if phase and phase != "completed":
            _blocked(
                "database_not_ready",
                f"database {db_name} download is not Ready (phase={phase})",
            )
    if db_meta.get("shards_stale"):
        _blocked(
            "shards_stale",
            f"database {db_name} shard layouts are stale; rebuild shards",
        )
    if not db_meta.get("sharded") and not db_meta.get("shard_sets"):
        _blocked(
            "shards_missing",
            f"database {db_name} has no published shard layout",
        )

    db_status = next(
        (
            item
            for item in warmup.get("databases", []) or []
            if isinstance(item, dict) and item.get("name") == db_name
        ),
        None,
    )
    if not isinstance(db_status, dict) or db_status.get("status") != "Ready":
        _blocked(
            "warmup_not_ready",
            f"node-local warmup for {db_name} must be Ready before building its oracle",
        )
    warm_source_version = str(db_status.get("source_version") or "")
    warm_versions = {str(item) for item in db_status.get("source_versions", []) or [] if str(item)}
    if len(warm_versions) > 1 or db_status.get("status") == "Stale":
        _blocked(
            "warmup_generation_mixed",
            f"node-local warmup for {db_name} has stale source versions",
        )
    if source_version and warm_source_version and source_version != warm_source_version:
        _blocked(
            "warmup_generation_stale",
            f"node-local warmup for {db_name} is for a stale DB generation",
        )
    effective_source_version = source_version or warm_source_version
    if not effective_source_version:
        _blocked(
            "source_version_missing",
            f"database {db_name} has no source generation marker",
        )

    shards = sorted({str(shard) for shard in db_status.get("shards", []) or [] if str(shard)})
    if not shards:
        total_jobs = int(db_status.get("total_jobs") or 0)
        shards = [f"{index:02d}" for index in range(total_jobs)]
    if not shards:
        _blocked("warmup_shards_missing", f"warmup for {db_name} reports no shards")

    pod_nodes: dict[str, str] = {}
    for pod in db_status.get("pod_statuses", []) or []:
        if not isinstance(pod, dict):
            continue
        shard = str(pod.get("shard") or "")
        node = str(pod.get("node") or "")
        if shard and node:
            pod_nodes[shard] = node
    raw_host_paths = db_status.get("shard_host_paths") or {}
    shard_host_paths = raw_host_paths if isinstance(raw_host_paths, dict) else {}
    mapped: list[tuple[str, str, str] | tuple[str, str]] = []
    normalised_paths: dict[str, str] = {}
    for index, shard in enumerate(shards):
        node = pod_nodes.get(shard) or (ready_nodes[index] if index < len(ready_nodes) else "")
        if not node:
            continue
        host_path = shard_host_paths.get(shard)
        if isinstance(host_path, str) and host_path:
            mapped.append((shard, node, host_path))
            normalised_paths[shard] = host_path
        else:
            mapped.append((shard, node))
    if len(mapped) != len(shards):
        _blocked(
            "shard_node_mapping_incomplete",
            "could not map every warmed shard to a Ready node",
        )

    layout_schema = int(db_meta.get("shard_layout_schema") or 0)
    fingerprint = oracle_layout_fingerprint(
        source_version=effective_source_version,
        shards=shards,
        shard_host_paths=normalised_paths,
        layout_schema=layout_schema,
    )
    return OracleBuildContext(
        db_name=db_name,
        source_version=effective_source_version,
        layout_schema=layout_schema,
        layout_fingerprint=fingerprint,
        identity=f"oracle-v1:{fingerprint}",
        shards=tuple(shards),
        shard_nodes=tuple(mapped),
    )


def resolve_oracle_build_context(
    credential: Any,
    *,
    subscription_id: str,
    storage_resource_group: str,
    storage_account: str,
    cluster_resource_group: str,
    cluster_name: str,
    db_name: str,
    requested_source_version: str = "",
) -> OracleBuildContext:
    """Fetch current observations and return one immutable build context."""
    from api.services.cluster_health import get_cluster_health

    try:
        health = get_cluster_health(
            credential,
            subscription_id,
            cluster_resource_group,
            cluster_name,
        )
    except Exception:
        health = None
    if health is not None and not health.get("healthy", True):
        raise OracleBuildBlocked(
            "aks_unavailable",
            "AKS cluster is not Running "
            f"(reason={health.get('reason')}, power_state={health.get('power_state')})",
            details={
                "cluster_reason": health.get("reason"),
                "cluster_power_state": health.get("power_state"),
            },
        )

    from api.services.k8s.monitoring import (
        k8s_ready_warmup_node_names,
        k8s_warmup_status,
    )
    from api.services.storage.data import list_databases

    databases = list_databases(credential, storage_account, "blast-db")
    db_meta = next(
        (item for item in databases if isinstance(item, dict) and item.get("name") == db_name),
        None,
    )
    warmup = k8s_warmup_status(
        credential,
        subscription_id,
        cluster_resource_group,
        cluster_name,
    )
    ready_nodes = k8s_ready_warmup_node_names(
        credential,
        subscription_id,
        cluster_resource_group,
        cluster_name,
    )
    return plan_oracle_build_from_snapshots(
        db_name=db_name,
        db_meta=db_meta,
        warmup=warmup,
        ready_nodes=ready_nodes,
        requested_source_version=requested_source_version,
    )


__all__ = [
    "OracleBuildBlocked",
    "OracleBuildContext",
    "plan_oracle_build_from_snapshots",
    "resolve_oracle_build_context",
]

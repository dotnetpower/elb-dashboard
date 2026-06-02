"""Per-cluster health gate used to short-circuit K8s-API monitor calls.

Responsibility: ARM-level reachability + power_state gate that callers consult
before issuing K8s API calls against an AKS cluster. Stopped or deleted
clusters return a degraded payload (no K8s call attempted) so monitor polling
does not generate one AppInsights exception per tick per stopped cluster.

Edit boundaries: Keep this layer thin — no kubeconfig fetch, no K8s API calls,
no mutation. Pure ARM lookup (`ManagedClusters.get`) wrapped in the existing
monitor snapshot cache so multi-cluster fleets pay one ARM call per cluster
per ~90s.

Key entry points: `get_cluster_health`, `cluster_skipped_payload`,
`CLUSTER_SKIP_REASONS`.

Risky contracts: The `degraded_reason` codes returned here are part of the
SPA banner contract (see `api/routes/monitor/common.py::_classify_exception`).
Renaming requires a coordinated SPA change.

Validation: `uv run pytest -q api/tests/test_cluster_health.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypedDict

from azure.core.credentials import TokenCredential

from api.services.azure_clients import aks_client
from api.services.monitor_cache import cached_snapshot

LOGGER = logging.getLogger(__name__)


CLUSTER_SKIP_REASONS = frozenset({"cluster_stopped", "cluster_not_found"})

_CLUSTER_META_TTL_SECONDS = 90.0
_CLUSTER_META_STALE_SECONDS = 900.0


class ClusterHealth(TypedDict):
    """Result of an ARM-level cluster reachability check.

    `healthy` is True only when the cluster exists in ARM AND its
    power_state is "Running". A stopped/missing cluster returns
    `healthy=False` with a `reason` code the caller can pass through
    to `cluster_skipped_payload`.

    `power_state` is None when the cluster does not exist (404) or when
    ARM itself was unreachable (the gate is best-effort — it never blocks
    callers when ARM is down, instead degrading to `reason=None` so the
    caller proceeds with the K8s call as if the gate were absent).

    `provisioning_state` is the ARM control-plane state (`Succeeded` /
    `Starting` / `Stopping` / `Updating` / …). It is None when the cluster
    does not exist or ARM was unreachable. The auto-stop evaluator uses it
    to avoid stopping a cluster whose start LRO is still in progress
    (AKS reports `power_state == "Running"` before provisioning settles).
    """

    healthy: bool
    exists: bool
    power_state: str | None
    provisioning_state: str | None
    reason: str | None


def _meta_cache_key(subscription_id: str, resource_group: str, cluster_name: str) -> str:
    return f"monitor:aks:meta:{subscription_id}:{resource_group}:{cluster_name}"


def _load_cluster_meta(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Single ARM `ManagedClusters.get` call serialized for the cache.

    Raises so `cached_snapshot` can record the failure once (transient
    suppression handled by `monitor_cache`); the wrapper below catches
    and converts to a benign `ClusterHealth` so the gate never blocks
    a caller when ARM itself is unreachable.
    """
    from azure.core.exceptions import ResourceNotFoundError

    client = aks_client(credential, subscription_id)
    try:
        cluster = client.managed_clusters.get(resource_group, cluster_name)
    except ResourceNotFoundError:
        return {"exists": False, "power_state": None}
    power_state = cluster.power_state.code if cluster.power_state else None
    return {
        "exists": True,
        "power_state": power_state,
        "provisioning_state": cluster.provisioning_state,
    }


def get_cluster_health(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    ttl_seconds: float | None = None,
    stale_seconds: float | None = None,
) -> ClusterHealth:
    """Return cached ARM health for a single cluster.

    Multi-cluster aware: each (sub, rg, cluster) tuple has its own cache
    entry so a stopped cluster does not affect polling for healthy siblings.

    ARM unreachable: the gate degrades open — returns `healthy=True,
    reason=None, exists=True` so the caller still attempts the K8s call.
    The K8s call may then fail (and `monitor_cache._refresh` will suppress
    the repeat noise), but we never inject a synthetic skip when we
    cannot prove the cluster is unhealthy.
    """
    ttl = ttl_seconds if ttl_seconds is not None else _CLUSTER_META_TTL_SECONDS
    stale = stale_seconds if stale_seconds is not None else _CLUSTER_META_STALE_SECONDS
    key = _meta_cache_key(subscription_id, resource_group, cluster_name)
    try:
        snapshot = cached_snapshot(
            key,
            lambda: _load_cluster_meta(
                credential, subscription_id, resource_group, cluster_name
            ),
            ttl_seconds=ttl,
            stale_seconds=stale,
        )
    except Exception as exc:  # ARM unreachable + no stale entry
        LOGGER.debug(
            "cluster_health gate degraded-open key=%s reason=%s",
            key,
            type(exc).__name__,
        )
        return ClusterHealth(
            healthy=True,
            exists=True,
            power_state=None,
            provisioning_state=None,
            reason=None,
        )

    exists = bool(snapshot.get("exists", True))
    power_state = snapshot.get("power_state")
    provisioning_state = snapshot.get("provisioning_state")
    if not exists:
        return ClusterHealth(
            healthy=False,
            exists=False,
            power_state=None,
            provisioning_state=None,
            reason="cluster_not_found",
        )
    if isinstance(power_state, str) and power_state and power_state != "Running":
        return ClusterHealth(
            healthy=False,
            exists=True,
            power_state=power_state,
            provisioning_state=(
                provisioning_state if isinstance(provisioning_state, str) else None
            ),
            reason="cluster_stopped",
        )
    return ClusterHealth(
        healthy=True,
        exists=True,
        power_state=power_state if isinstance(power_state, str) else None,
        provisioning_state=(
            provisioning_state if isinstance(provisioning_state, str) else None
        ),
        reason=None,
    )


def cluster_skipped_payload(
    reason: str,
    *,
    power_state: str | None,
    empty: dict[str, Any],
) -> dict[str, Any]:
    """Build the degraded response a monitor route returns when the gate
    decides a cluster is unreachable for benign reasons.

    Mirrors the shape of `api/routes/monitor/common.py::_graceful` so the
    SPA's existing diagnostics banner branches keep working without
    additional code changes (the banner looks at `degraded` /
    `degraded_reason`; `power_state` is a hint the cluster card surfaces).
    """
    out = dict(empty)
    out["degraded"] = True
    out["degraded_reason"] = reason
    if power_state is not None:
        out["power_state"] = power_state
    return out


def cached_snapshot_with_cluster_gate(
    cache_key: str,
    loader: Callable[[], dict[str, Any]],
    *,
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    empty: dict[str, Any],
    ttl_seconds: float | None = None,
    stale_seconds: float | None = None,
) -> dict[str, Any]:
    """Short-circuit a K8s-API-backed monitor call when the cluster is
    known-unhealthy (Stopped / deleted), otherwise delegate to
    `cached_snapshot`.

    Multi-cluster safe: the gate is per `(subscription, resource_group,
    cluster_name)` tuple, so a stopped cluster only skips its own keys —
    every healthy sibling is polled normally.

    ARM unreachable degrades open: the gate returns `healthy=True,
    reason=None` and the normal cached_snapshot path runs. Any K8s-side
    failure is then absorbed by `monitor_cache._refresh`'s
    transient-suppression logic.
    """
    health = get_cluster_health(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
    )
    if not health["healthy"] and health["reason"] in CLUSTER_SKIP_REASONS:
        LOGGER.debug(
            "cluster_health gate skipped key=%s reason=%s power_state=%s",
            cache_key,
            health["reason"],
            health["power_state"],
        )
        return cluster_skipped_payload(
            health["reason"] or "cluster_stopped",
            power_state=health["power_state"],
            empty=empty,
        )
    return cached_snapshot(
        cache_key,
        loader,
        ttl_seconds=ttl_seconds,
        stale_seconds=stale_seconds,
    )


__all__ = [
    "CLUSTER_SKIP_REASONS",
    "ClusterHealth",
    "cached_snapshot_with_cluster_gate",
    "cluster_skipped_payload",
    "get_cluster_health",
]

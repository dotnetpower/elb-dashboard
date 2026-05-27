"""Shared AKS route helpers.

Responsibility: Shared AKS route helpers
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_invalidate_aks_monitor_cache`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import os

from api.services.monitor_cache import invalidate_monitor_snapshot_prefix


def _invalidate_aks_monitor_cache(subscription_id: str, resource_group: str) -> None:
    """Drop cached /api/monitor/aks* snapshots for the targeted scope.

    Lifecycle actions (start/stop/delete) eventually flip the cluster's
    `power_state`/`provisioning_state` in ARM. Without this the SPA can
    keep seeing the previous reading for up to 5 minutes (TTL 30 s +
    stale-while-revalidate 5 min) even after the operator hits Start.
    Invalidating the prefix forces the very next monitor poll to bypass
    the cache and re-query ARM.

    The categories below mirror every cache key produced by `api/routes/monitor.py`
    under the `monitor:aks:*` namespace. When a new monitor:aks:<x>:* route is
    added, append "<x>" here so lifecycle mutations do not leave it stale.
    """
    sub = subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if not sub or not resource_group:
        return
    # The cluster-list key is `monitor:aks:{sub}:{rg}` (no subcategory). Boundary-safe
    # invalidation ignores neighbouring RGs whose names share a string prefix.
    invalidate_monitor_snapshot_prefix(f"monitor:aks:{sub}:{resource_group}")
    # Per-cluster keys are `monitor:aks:<cat>:{sub}:{rg}:{cluster}[:...]`.
    # `meta` is owned by `api/services/cluster_health.py::get_cluster_health` —
    # its `power_state` snapshot MUST be dropped on lifecycle mutations or the
    # cluster_health gate keeps the previous reading for up to 90 s and either
    # (a) skips healthy K8s polls right after Start, surfacing a false
    # `cluster_stopped` chip, or (b) attempts K8s calls right after Stop and
    # re-introduces the connect-timeout exception noise we tried to dedup.
    for category in ("nodes", "pods", "top-nodes", "warmup-status", "events", "meta"):
        invalidate_monitor_snapshot_prefix(f"monitor:aks:{category}:{sub}:{resource_group}")

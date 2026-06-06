"""Diagnostics resource fetch (snapshot) layer.

Fetches each configured Azure resource exactly once per diagnostic run, with
per-resource failure isolation, so one resource's outage or permission denial
becomes an `indeterminate` finding for that resource only — never a fabricated
`ok` and never a `gather`-wide abort.

Responsibility: Turn a `DiagnosticTarget` (the configured subscription / resource
    groups / account names) into a `dict[ResourceKind, ResourceSnapshot]` by
    calling the existing `api.services.monitoring` helpers, classifying any
    failure as `denied` (permission) / `error` / `timeout`.
Edit boundaries: IO + Azure SDK orchestration only. No best-practice logic
    (that is `rules/`), no HTTP shaping (that is the route), no severity model.
Key entry points: `DiagnosticTarget`, `gather_reliability_snapshot`,
    `gather_availability_snapshot`.
Risky contracts: A fetch that raises `AuthorizationFailed`/403 MUST yield
    `access="denied"` so rules emit `indeterminate`, never `critical` — this is
    what keeps the Reader persona green. Every fetch runs under a bounded
    deadline; an overrun yields `access="timeout"`, never an indefinite hang.
Validation: `uv run pytest -q api/tests/test_diagnostics_rules.py
    api/tests/test_diagnostics_route.py`.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential

from api.routes.monitor.common import _classify_exception
from api.services.diagnostics.models import ResourceKind, ResourceSnapshot

LOGGER = logging.getLogger(__name__)

# Per-fetch and overall deadlines. Both bounded so a slow ARM call cannot hang
# the request. Overridable via env for slow tenants / tests.
_FETCH_TIMEOUT_SECONDS = float(os.environ.get("DIAGNOSTICS_FETCH_TIMEOUT_SECONDS", "8"))
_RUN_DEADLINE_SECONDS = float(os.environ.get("DIAGNOSTICS_RUN_DEADLINE_SECONDS", "25"))

# Degraded-reason codes (from `_classify_exception`) that mean "this principal
# is not allowed to read the resource" rather than "the resource is broken".
# A Reader legitimately hits these, so they must become `indeterminate`.
_DENIED_REASONS: frozenset[str] = frozenset({"forbidden", "unauthorized", "auth_wrong_tenant"})


@dataclass(frozen=True)
class DiagnosticTarget:
    """The configured resources a diagnostic run inspects.

    Mirrors the SPA `ResourceConfig`. Empty fields are simply skipped — e.g. a
    run with no `storage_account` produces no Storage findings rather than an
    error.
    """

    subscription_id: str
    workload_resource_group: str = ""
    acr_resource_group: str = ""
    acr_name: str = ""
    storage_account_name: str = ""
    region: str = ""


def _access_from_reason(reason: str) -> str:
    return "denied" if reason in _DENIED_REASONS else "error"


def _collect_isolated(
    kind: ResourceKind,
    future: concurrent.futures.Future,
    deadline_remaining: float,
) -> ResourceSnapshot:
    """Collect one already-submitted fetch with isolation + a bounded wait.

    Any exception is classified into a `denied` / `error` snapshot; a timeout
    (the fetch overran the per-fetch cap or the run deadline) yields
    `access="timeout"`. Never raises and never blocks past the deadline.
    """
    timeout = max(0.0, min(_FETCH_TIMEOUT_SECONDS, deadline_remaining))
    try:
        data = future.result(timeout=timeout)
        return ResourceSnapshot(kind=kind, available=True, data=data or {})
    except concurrent.futures.TimeoutError:
        future.cancel()
        LOGGER.warning("diagnostics fetch timed out kind=%s timeout=%.1fs", kind, timeout)
        return ResourceSnapshot(
            kind=kind,
            available=False,
            reason="timed out",
            access="timeout",
        )
    except Exception as exc:
        reason = _classify_exception(exc)
        access = _access_from_reason(reason)
        LOGGER.warning(
            "diagnostics fetch failed kind=%s reason=%s exc=%s",
            kind,
            reason,
            type(exc).__name__,
        )
        return ResourceSnapshot(kind=kind, available=False, reason=reason, access=access)


def _discover_clusters(credential: TokenCredential, subscription_id: str) -> list[dict[str, Any]]:
    """Best-effort list of ELB-managed clusters in the subscription."""
    from api.services import monitoring as monitoring_svc

    return monitoring_svc.list_aks_clusters_in_subscription(
        credential, subscription_id, include_unmanaged=False
    )


def gather_reliability_snapshot(
    credential: TokenCredential, target: DiagnosticTarget
) -> dict[str, ResourceSnapshot]:
    """Fetch AKS / Storage / ACR / Container App for the Reliability category."""
    from api.services import monitoring as monitoring_svc

    snapshots: dict[str, ResourceSnapshot] = {}
    sub = target.subscription_id

    fetches: dict[ResourceKind, Callable[[], dict[str, Any]]] = {
        "aks": lambda: {"clusters": _discover_clusters(credential, sub)},
    }
    if target.storage_account_name and target.workload_resource_group:
        fetches["storage"] = lambda: monitoring_svc.get_storage_summary(
            credential, sub, target.workload_resource_group, target.storage_account_name
        )
    if target.acr_name and target.acr_resource_group:
        fetches["acr"] = lambda: monitoring_svc.list_acr_repositories(
            credential, sub, target.acr_resource_group, target.acr_name
        )

    _run_all(fetches, snapshots)
    # Container App config is local (env), no fetch — synthesised directly.
    snapshots["container_app"] = _container_app_snapshot()
    return snapshots


def gather_availability_snapshot(
    credential: TokenCredential, target: DiagnosticTarget
) -> dict[str, ResourceSnapshot]:
    """Fetch node pressure / sidecars / api metrics for the Availability category."""
    from api.services.k8s.node_pressure import k8s_node_request_pressure

    snapshots: dict[str, ResourceSnapshot] = {}
    sub = target.subscription_id

    def _aks_pressure() -> dict[str, Any]:
        clusters = _discover_clusters(credential, sub)
        pools_by_cluster: list[dict[str, Any]] = []
        for cluster in clusters:
            rg = cluster.get("resource_group") or ""
            name = cluster.get("name") or ""
            if not rg or not name:
                continue
            pressure = k8s_node_request_pressure(credential, sub, rg, name)
            pools_by_cluster.append(
                {
                    "cluster": name,
                    "power_state": cluster.get("power_state"),
                    "pressure": pressure,
                }
            )
        return {"clusters": pools_by_cluster}

    fetches: dict[ResourceKind, Callable[[], dict[str, Any]]] = {"aks": _aks_pressure}
    _run_all(fetches, snapshots)

    # Sidecars + API metrics are local in-process reads — cheap, never ARM.
    snapshots["container_app"] = _sidecars_snapshot()
    snapshots["api"] = _api_metrics_snapshot()
    return snapshots


def _run_all(
    fetches: dict[ResourceKind, Callable[[], dict[str, Any]]],
    snapshots: dict[str, ResourceSnapshot],
) -> None:
    """Run a batch of fetches truly concurrently under the overall deadline.

    All fetches are submitted up front (concurrent), then collected with a
    per-future timeout bounded by the remaining run deadline. The executor is
    shut down with ``wait=False`` so a fetch whose underlying SDK call is still
    blocked (despite its own socket timeout) cannot make the request hang past
    the deadline — the orphaned worker drains in the background and the route
    returns promptly with `timeout` snapshots for the stragglers.
    """
    import time

    if not fetches:
        return
    started = time.monotonic()
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, len(fetches)), thread_name_prefix="diagnostics"
    )
    try:
        futures = {kind: executor.submit(fetch) for kind, fetch in fetches.items()}
        for kind, future in futures.items():
            remaining = _RUN_DEADLINE_SECONDS - (time.monotonic() - started)
            snapshots[kind] = _collect_isolated(kind, future, remaining)
    finally:
        # Do NOT block on stragglers (wait=False); cancel any not-yet-started.
        executor.shutdown(wait=False, cancel_futures=True)


def _container_app_snapshot() -> ResourceSnapshot:
    """Synthesise the Container App reliability config from env (no fetch)."""
    revision = os.environ.get("CONTAINER_APP_REVISION", "")
    name = os.environ.get("CONTAINER_APP_NAME", "")
    return ResourceSnapshot(
        kind="container_app",
        available=True,
        data={
            "name": name,
            "revision": revision,
            "deployed": bool(name),
            # minReplicas is pinned to 1 by charter (cost design), surfaced so
            # the rule can emit `info` (expected_by_charter), not `warning`.
            "min_replicas": 1,
            "max_replicas": 1,
        },
    )


def _sidecars_snapshot() -> ResourceSnapshot:
    """One-shot sidecar health/CPU/MEM read (local, never ARM).

    When the metrics backend (Redis) is unreachable, `collect_snapshot` returns
    an all-`down` degraded payload. That is "could not read the metrics", NOT
    "the sidecars are down" — surface it as `unavailable` so the rule emits
    `indeterminate` instead of a false `critical`.
    """
    try:
        from api.routes import monitor as monitor_package

        snap = monitor_package.collect_snapshot(drain_events=False)
        if snap.get("degraded"):
            return ResourceSnapshot(
                kind="container_app",
                available=False,
                reason=str(snap.get("degraded_reason") or "metrics unavailable")[:80],
                access="error",
            )
        return ResourceSnapshot(kind="container_app", available=True, data=snap)
    except Exception as exc:
        LOGGER.warning("diagnostics sidecar snapshot failed: %s", type(exc).__name__)
        return ResourceSnapshot(
            kind="container_app", available=False, reason="error", access="error"
        )


def _api_metrics_snapshot() -> ResourceSnapshot:
    """Last-15-minute request latency / error-rate aggregate (local)."""
    try:
        from api.services.request_metrics import metrics as _metrics

        summary = _metrics().summarise(window_seconds=900, rpm_buckets=15)
        return ResourceSnapshot(kind="api", available=True, data=summary)
    except Exception as exc:
        LOGGER.warning("diagnostics api metrics snapshot failed: %s", type(exc).__name__)
        return ResourceSnapshot(kind="api", available=False, reason="error", access="error")

"""Ensure-running state machine for the AKS-hosted OpenAPI plane.

Responsibility: Decide a single, polling-friendly readiness phase for a cluster
that hosts ``elb-openapi`` — ``not_found`` / ``stopped`` / ``starting`` /
``warming`` / ``ready`` / ``unknown`` — from one cached ARM health lookup plus
(only when Running and warmup is configured) the live warmup readiness gate,
which checks BOTH warmup-node K8s readiness AND that every configured database
finished its node-local warmup Job.
This is the brain behind ``POST /api/aks/openapi/ensure-running``: an external
OpenAPI caller cannot wake the cluster that hosts the OpenAPI service (the
service is down with the cluster), so the always-on dashboard ``api`` sidecar
consults this evaluator and the route decides whether to enqueue a start.
Edit boundaries: Pure decision logic only — no Celery enqueue, no kubectl, no
direct ``azure.mgmt`` imports beyond the existing service wrappers. The route
owns the start side effect; this module only sets ``start_recommended``.
Key entry points: `evaluate_ensure_running`, `EnsureRunningResult`,
`ENSURE_RUNNING_STATUSES`.
Risky contracts: ``status`` is the external contract polled by callers; the
values in `ENSURE_RUNNING_STATUSES` must stay stable. ``start_recommended`` is
True only for a fully-stopped cluster (never while Stopping/Starting) so the
route cannot race an in-flight stop/start LRO. A cluster whose warmup readiness
cannot be confirmed — warmup nodes not all Ready, OR any configured database
still Loading/Stale/missing — degrades to ``warming`` (never ``ready``) so a
caller never submits against a cold cluster. A database in a TERMINAL ``Failed``
state is the one exception: warmup is best-effort (like the upstream
``/v1/ready``), so the cluster still reports ``ready`` with the failed set
surfaced, rather than polling forever.
Validation: `uv run pytest -q api/tests/test_aks_ensure_running.py`.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from azure.core.credentials import TokenCredential

LOGGER = logging.getLogger(__name__)

# External, polled status vocabulary. Keep stable — callers branch on these.
ENSURE_RUNNING_STATUSES = frozenset(
    {"not_found", "stopped", "starting", "warming", "ready", "unknown"}
)

# Polling cadence hints (seconds) returned to the caller as ``retry_after_seconds``
# and surfaced by the route as a ``Retry-After`` header. ``ready`` / ``not_found``
# return ``None`` (no further polling needed / caller must create the cluster).
_RETRY_TRANSITION = 30  # stopped / starting / unknown — slow ARM LRO
_RETRY_WARMING = 15  # warming — warmup nodes register Ready in tighter windows


class EnsureRunningResult(TypedDict):
    """Outcome of one ensure-running evaluation.

    ``start_recommended`` is advisory: the route enqueues ``start_aks`` only when
    it is True AND auto-start is allowed for the request. ``warmup`` is the raw
    readiness-gate summary (``ready`` / ``phase`` / ``expected_node_count`` /
    ``ready_node_count``, plus ``databases_total`` / ``databases_ready`` /
    ``pending_databases`` / ``failed_databases`` once the warmup nodes are
    Ready) when warmup was evaluated, else ``None``.
    """

    status: str
    power_state: str | None
    provisioning_state: str | None
    exists: bool
    start_recommended: bool
    warmup: dict[str, Any] | None
    retry_after_seconds: int | None
    reason: str


def _result(
    status: str,
    *,
    power_state: str | None = None,
    provisioning_state: str | None = None,
    exists: bool = True,
    start_recommended: bool = False,
    warmup: dict[str, Any] | None = None,
    retry_after_seconds: int | None = None,
    reason: str = "",
) -> EnsureRunningResult:
    return EnsureRunningResult(
        status=status,
        power_state=power_state,
        provisioning_state=provisioning_state,
        exists=exists,
        start_recommended=start_recommended,
        warmup=warmup,
        retry_after_seconds=retry_after_seconds,
        reason=reason,
    )


def _evaluate_warmup_phase(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    power_state: str,
    provisioning_state: str,
) -> EnsureRunningResult:
    """Decide ``warming`` vs ``ready`` for a Running cluster.

    Returns ``ready`` immediately when no enabled warmup preference with at least
    one database exists (nothing to warm → the cluster is good to serve). When
    warmup IS configured, runs the live readiness gate in two stages: first the
    warmup-node K8s readiness gate, then a per-database warmup-Job check. A
    cluster is ``ready`` only when its nodes are all Ready AND every configured
    database is warm (``Ready``) or terminally ``Failed`` (best-effort); any
    database still loading, or a transient ARM/K8s hiccup / missing snapshot,
    degrades to ``warming`` so a caller never submits against a cluster whose
    node-local DB cache is still being populated.
    """
    from api.services.auto_warmup import get_auto_warmup_preference

    pref = get_auto_warmup_preference(subscription_id, resource_group, cluster_name)
    warmup_required = bool(pref is not None and pref.enabled and pref.databases)
    if not warmup_required:
        return _result(
            "ready",
            power_state=power_state,
            provisioning_state=provisioning_state,
            reason="cluster is Running and no warmup is configured",
        )

    from api.services.auto_warmup_reconcile import auto_warmup_ready_gate
    from api.services.monitoring import get_aks_cluster_snapshot

    snapshot = get_aks_cluster_snapshot(
        credential, subscription_id, resource_group, cluster_name
    )
    if snapshot is None:
        # The cluster disappeared between the cached health read and now, or the
        # fresh ARM get failed. Treat as not-yet-ready rather than ready.
        return _result(
            "warming",
            power_state=power_state,
            provisioning_state=provisioning_state,
            retry_after_seconds=_RETRY_WARMING,
            reason="cluster snapshot unavailable; warmup readiness unconfirmed",
        )

    if pref is None:  # narrowed by warmup_required; defensive against ``-O``
        raise RuntimeError("warmup_required returned True but pref is None")
    gate = auto_warmup_ready_gate(
        credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        cluster=snapshot,
        configured_num_nodes=pref.num_nodes,
    )
    warmup_summary: dict[str, Any] = {
        "ready": bool(gate.get("ready")),
        "phase": str(gate.get("phase") or ""),
        "expected_node_count": int(gate.get("expected_node_count") or 0),
        "ready_node_count": int(gate.get("ready_node_count") or 0),
    }
    if not gate.get("ready"):
        # Warmup nodes are not all Ready yet — the per-DB warmup Jobs have not
        # even been scheduled on every node, so the cluster is still warming.
        return _result(
            "warming",
            power_state=power_state,
            provisioning_state=provisioning_state,
            warmup=warmup_summary,
            retry_after_seconds=_RETRY_WARMING,
            reason=str(gate.get("reason") or "warmup is still in progress"),
        )

    # Nodes are Ready — but node readiness only proves the K8s nodes are up, NOT
    # that every configured database finished copying to the node-local SSD. A
    # caller submitting while e.g. ``core_nt`` is still ``Loading`` (only
    # ``ribosome`` warmed) would silently fall back to the slow on-node DB init,
    # which is exactly the cold submit this gate exists to prevent. Report
    # ``ready`` once every configured database is warm (``Ready``), OR is in a
    # terminal ``Failed`` state that retrying cannot fix (best-effort, degraded).
    db_warmup = _evaluate_database_warmup(
        credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        configured_databases=list(pref.databases),
    )
    warmup_summary.update(db_warmup["summary"])
    if not db_warmup["ready"]:
        warmup_summary["ready"] = False
        warmup_summary["phase"] = "warming_databases"
        return _result(
            "warming",
            power_state=power_state,
            provisioning_state=provisioning_state,
            warmup=warmup_summary,
            retry_after_seconds=_RETRY_WARMING,
            reason=db_warmup["reason"],
        )
    if db_warmup["degraded"]:
        # Every still-progressing database is warm, but at least one is in a
        # terminal Failed state. Blocking ``ready`` forever on it would turn the
        # upstream "slow but works" fallback into "never submits", so report
        # ``ready`` (degraded) and surface the failed set for the caller / UI.
        warmup_summary["phase"] = "ready_degraded"
    return _result(
        "ready",
        power_state=power_state,
        provisioning_state=provisioning_state,
        warmup=warmup_summary,
        reason=db_warmup["reason"],
    )


def _evaluate_database_warmup(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    configured_databases: list[str],
) -> dict[str, Any]:
    """Confirm every configured warmup database is warm on the cluster.

    Reads the node-local warmup Job aggregation (``k8s_warmup_status``) and
    classifies each configured database into three buckets, mirroring the
    retryable/terminal split the BLAST submit gate
    (``ensure_node_warmup_ready_for_submit``) already uses:

    * ``Ready`` — warm on the node-local SSD.
    * still progressing (``Loading`` / ``Pending`` / ``Starting`` / ``Stale`` /
      ``Unknown`` / missing, or any node still active) — keep ``warming`` so a
      caller polls until it finishes.
    * terminal ``Failed`` with no active node — retrying cannot fix it (missing
      RBAC, network-blocked Storage, corrupt blob) and the reconcile circuit
      breaker stops re-enqueuing it, so blocking ``ready`` forever would strand
      the whole cluster un-submittable. Treat warmup as best-effort here (like
      the upstream ``/v1/ready``): report ``ready`` with ``degraded=True`` and
      the failed set so the caller can submit (the search falls back to the slow
      on-node init for the failed DB only).

    Returns ``ready`` / ``degraded`` plus an additive ``summary``
    (``databases_total`` / ``databases_ready`` / ``pending_databases`` /
    ``failed_databases``) and a human ``reason``. A K8s read hiccup yields an
    empty status → every DB missing → ``warming``, consistent with the
    node-readiness gate's own fail-closed behaviour. NOTE: this gate confirms the
    DB is warm, NOT that it is the latest NCBI generation — the per-submit gate
    owns the generation/``source_version`` comparison.
    """
    from api.services.auto_warmup_reconcile import warmup_status_by_db
    from api.services.monitoring import k8s_warmup_status

    configured = [db for db in configured_databases if db]
    if not configured:
        # Defensive: ``warmup_required`` already guarantees a non-empty list.
        return {"ready": True, "degraded": False, "summary": {}, "reason": ""}

    status = k8s_warmup_status(credential, subscription_id, resource_group, cluster_name)
    by_db = warmup_status_by_db(status.get("databases", []) or [])

    ready_dbs: list[str] = []
    pending: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for db_name in configured:
        meta = by_db.get(db_name) or {}
        db_status = str(meta.get("status") or "")
        active = int(meta.get("nodes_active") or 0)
        if db_status == "Ready":
            ready_dbs.append(db_name)
        elif db_status == "Failed" and active == 0:
            failed.append({"db": db_name, "status": db_status})
        else:
            # Loading / Pending / Starting / Stale / Unknown / missing / active>0
            pending.append({"db": db_name, "status": db_status or "pending"})

    summary: dict[str, Any] = {
        "databases_total": len(configured),
        "databases_ready": len(ready_dbs),
        "pending_databases": pending,
        "failed_databases": failed,
    }
    if pending:
        pending_names = ", ".join(item["db"] for item in pending)
        return {
            "ready": False,
            "degraded": False,
            "summary": summary,
            "reason": f"warmup databases still loading: {pending_names}",
        }
    if failed:
        failed_names = ", ".join(item["db"] for item in failed)
        return {
            "ready": True,
            "degraded": True,
            "summary": summary,
            "reason": (
                f"cluster is Running; warmup failed for {failed_names} — serving "
                "without the node-local cache for those databases"
            ),
        }
    return {
        "ready": True,
        "degraded": False,
        "summary": summary,
        "reason": "cluster is Running and all warmup databases are Ready",
    }


def evaluate_ensure_running(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> EnsureRunningResult:
    """Map current ARM + warmup state to one ensure-running phase.

    Single cached ARM health lookup drives the stopped/starting/running branch;
    only a Running cluster with warmup configured pays the extra fresh ARM get +
    K8s node-readiness call. The function never mutates anything — the caller
    decides whether to act on ``start_recommended``.
    """
    from api.services.cluster_health import get_cluster_health

    health = get_cluster_health(
        credential, subscription_id, resource_group, cluster_name
    )
    if not health["exists"]:
        return _result(
            "not_found",
            exists=False,
            reason="cluster does not exist in ARM",
        )

    power_state = health["power_state"]
    provisioning_state = health["provisioning_state"]
    if power_state is None:
        # ARM unreachable (gate degraded open). We cannot prove the cluster is
        # stopped, so never trigger a start — just ask the caller to retry.
        return _result(
            "unknown",
            power_state=None,
            provisioning_state=provisioning_state,
            retry_after_seconds=_RETRY_TRANSITION,
            reason="cluster power state is currently unknown (ARM unreachable)",
        )

    ps = power_state.strip().casefold()
    prov = (provisioning_state or "").strip().casefold()

    # A start/stop LRO in flight wins over the raw power_state: AKS reports
    # power_state=Running the instant the start LRO begins, long before the
    # control plane settles, and reports Stopped while a stop LRO finishes.
    if ps == "starting" or prov == "starting":
        return _result(
            "starting",
            power_state=power_state,
            provisioning_state=provisioning_state,
            retry_after_seconds=_RETRY_TRANSITION,
            reason="cluster start is in progress",
        )
    if prov == "stopping" or ps == "stopping":
        # Mid-stop: do NOT enqueue a start (it would race the stop LRO and ARM
        # would reject it). Report stopped; a later poll sees a settled Stopped
        # cluster and recommends the start then.
        return _result(
            "stopped",
            power_state=power_state,
            provisioning_state=provisioning_state,
            start_recommended=False,
            retry_after_seconds=_RETRY_TRANSITION,
            reason="cluster is stopping; wait for it to settle before starting",
        )
    if ps == "stopped":
        return _result(
            "stopped",
            power_state=power_state,
            provisioning_state=provisioning_state,
            start_recommended=True,
            retry_after_seconds=_RETRY_TRANSITION,
            reason="cluster is stopped",
        )
    if ps == "running":
        return _evaluate_warmup_phase(
            credential,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            power_state=power_state,
            provisioning_state=provisioning_state or "",
        )

    # Any other transient power_state (e.g. an unmapped value) — surface it as
    # unknown and let the caller retry without taking action.
    return _result(
        "unknown",
        power_state=power_state,
        provisioning_state=provisioning_state,
        retry_after_seconds=_RETRY_TRANSITION,
        reason=f"cluster power state '{power_state}' is not actionable yet",
    )

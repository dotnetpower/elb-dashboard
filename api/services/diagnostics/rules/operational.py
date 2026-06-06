"""Operational-health rule catalog (live production incident tracking).

Unlike the config-posture catalogs (reliability / security) and the
config-performance catalog (availability), this catalog reads **live runtime
signals** — Kubernetes Warning events, pod restarts, node conditions, failed
Jobs, BLAST job-state rows, and per-route API errors — and turns them into
findings an operator can *trace to the specific failing object*. Every finding
carries the offending reason / pod / node / job id + a sample message in
`observed` so the next step (kubectl describe, logs, the AKS card) is obvious.

Responsibility: Map the operational snapshot (per-cluster events/pods/nodes/
    jobs/deployments + jobstate + per-route metrics) to traceable findings.
Edit boundaries: Pure functions only. No Azure/K8s SDK, no fetch. Thresholds are
    module constants.
Key entry points: `evaluate_operational`.
Risky contracts: A failure/permission-denied snapshot yields `indeterminate`,
    never `critical`. Aggregations cap the number of findings (one per
    reason-class / condition, with counts) so a noisy cluster cannot flood the
    page.
Validation: `uv run pytest -q api/tests/test_diagnostics_operational_rules.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from api.services.diagnostics.models import Finding, ResourceSnapshot
from api.services.diagnostics.rules.common import indeterminate_for, short_name

_PILLAR = "Operational Excellence"
_CATEGORY = "operational"

_DOC_EVENTS = "https://learn.microsoft.com/azure/aks/events"
_DOC_TROUBLESHOOT = (
    "https://learn.microsoft.com/troubleshoot/azure/azure-kubernetes/welcome-azure-kubernetes"
)
_DOC_NODE = "https://learn.microsoft.com/azure/aks/node-health"
_DOC_PENDING = "https://learn.microsoft.com/troubleshoot/azure/azure-kubernetes/availability-performance/cluster-node-pod-resource-issues"
_DOC_IMAGE = "https://learn.microsoft.com/troubleshoot/azure/azure-kubernetes/connectivity/cannot-pull-image-from-acr-to-aks-cluster"
_DOC_BLAST = "https://learn.microsoft.com/azure/aks/"

# Thresholds (conservative — operational warnings, criticals only for confirmed
# down/failed states).
_RESTART_WARN = 5  # cumulative container restarts on one pod
_RESTART_CRIT = 20
_PENDING_AGE_WARN_MIN = 10  # pod stuck Pending longer than this
_STALE_RUNNING_HOURS = 12  # BLAST job "running" with no update for this long
_MAX_SAMPLE_OBJECTS = 5  # cap object names echoed into a finding

# Kubernetes Warning event reasons grouped into operator-meaningful classes.
# Each class becomes at most one finding (aggregated count) so a storm of
# identical events does not flood the page.
_EVENT_CLASSES: list[dict[str, Any]] = [
    {
        "id": "events.failed_scheduling",
        "reasons": {"FailedScheduling"},
        "severity": "warning",
        "title": "Pods cannot be scheduled",
        "detail": "The scheduler could not place one or more pods "
        "(insufficient capacity, taints, or affinity).",
        "recommendation": "Check node capacity / autoscaler and pod requests, "
        "taints, and affinity rules.",
        "doc_url": _DOC_PENDING,
    },
    {
        "id": "events.image_pull",
        "reasons": {
            "Failed",
            "ErrImagePull",
            "ImagePullBackOff",
            "InspectFailed",
            "ImageInspectError",
        },
        "severity": "warning",
        "title": "Container image pull failures",
        "detail": "One or more containers could not pull their image "
        "(registry auth, tag missing, or network).",
        "recommendation": "Verify the image tag exists, the cluster can reach "
        "the registry, and pull credentials are valid.",
        "doc_url": _DOC_IMAGE,
    },
    {
        "id": "events.oom",
        "reasons": {"OOMKilling", "OOMKilled"},
        "severity": "critical",
        "title": "Containers killed for out-of-memory",
        "detail": "The kernel OOM-killed one or more containers that exceeded "
        "their memory limit.",
        "recommendation": "Raise the container memory limit or reduce memory "
        "use; check for a leak.",
        "doc_url": _DOC_TROUBLESHOOT,
    },
    {
        "id": "events.backoff",
        "reasons": {"BackOff"},
        "severity": "warning",
        "title": "Containers restarting (back-off / crash loop)",
        "detail": "Containers are repeatedly crashing and being restarted with back-off.",
        "recommendation": "Inspect the crashing container's logs "
        "(`kubectl logs --previous`) for the root cause.",
        "doc_url": _DOC_TROUBLESHOOT,
    },
    {
        "id": "events.unhealthy",
        "reasons": {"Unhealthy", "ProbeWarning"},
        "severity": "warning",
        "title": "Readiness / liveness probe failures",
        "detail": "Health probes are failing, so pods are being marked unready or restarted.",
        "recommendation": "Check the probe endpoints and the container's startup "
        "time vs probe thresholds.",
        "doc_url": _DOC_TROUBLESHOOT,
    },
    {
        "id": "events.volume",
        "reasons": {"FailedMount", "FailedAttachVolume", "VolumeFailedMount", "FailedBinding"},
        "severity": "warning",
        "title": "Volume mount / attach failures",
        "detail": "A pod could not mount or attach a persistent volume.",
        "recommendation": "Check the PVC/PV binding, the CSI driver, and the "
        "disk's zone alignment with the node.",
        "doc_url": _DOC_TROUBLESHOOT,
    },
    {
        "id": "events.node",
        "reasons": {
            "NodeNotReady",
            "NodeNotSchedulable",
            "NodeHasDiskPressure",
            "NodeHasMemoryPressure",
        },
        "severity": "warning",
        "title": "Node health events",
        "detail": "Nodes reported NotReady / pressure / unschedulable conditions.",
        "recommendation": "Inspect the affected nodes (kubelet, disk, memory); "
        "the autoscaler may need to replace them.",
        "doc_url": _DOC_NODE,
    },
    {
        "id": "events.evicted",
        "reasons": {"Evicted", "Preempting", "Preempted"},
        "severity": "warning",
        "title": "Pod evictions / preemptions",
        "detail": "Pods were evicted or preempted, usually due to node resource pressure.",
        "recommendation": "Add capacity or set pod priorities; evicted pods lose "
        "their node and must reschedule.",
        "doc_url": _DOC_PENDING,
    },
    {
        "id": "events.sandbox",
        "reasons": {
            "FailedCreatePodSandBox",
            "SandboxChanged",
            "NetworkNotReady",
            "FailedCreatePodContainer",
        },
        "severity": "warning",
        "title": "Pod sandbox / CNI / network failures",
        "detail": "The container runtime or CNI could not create the pod sandbox or network.",
        "recommendation": "Check the CNI (Azure CNI / kubenet), node networking, "
        "and the container runtime.",
        "doc_url": _DOC_TROUBLESHOOT,
    },
    {
        "id": "events.controller",
        "reasons": {"FailedCreate", "FailedDelete", "FailedKillPod"},
        "severity": "warning",
        "title": "Controller could not create / delete pods",
        "detail": "A Job/ReplicaSet controller failed to create or delete pods "
        "(quota, admission, or API errors).",
        "recommendation": "Check namespace resource quotas, admission webhooks, "
        "and the controller's events.",
        "doc_url": _DOC_TROUBLESHOOT,
    },
]


def evaluate_operational(snapshots: dict[str, ResourceSnapshot]) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_aks_runtime_rules(snapshots.get("aks")))
    findings.extend(_jobstate_rules(snapshots.get("queue")))
    findings.extend(_api_route_rules(snapshots.get("api")))
    return findings


def _mk(**kwargs: Any) -> Finding:
    return Finding(category=_CATEGORY, pillar=_PILLAR, **kwargs)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _age_minutes(value: str) -> float | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    return max(0.0, (_now() - ts).total_seconds() / 60.0)


# --------------------------------------------------------------------------- AKS


def _aks_runtime_rules(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="aks",
                id="aks.runtime",
                title="Cluster runtime health could not be read",
                doc_url=_DOC_EVENTS,
            )
        ]
    clusters: list[dict[str, Any]] = snap.data.get("clusters") or []
    if not clusters:
        return [
            _mk(
                id="aks.runtime",
                resource_kind="aks",
                severity="info",
                title="No running cluster to inspect",
                detail="No managed cluster was discovered to read live operational signals.",
                doc_url=_DOC_EVENTS,
            )
        ]
    findings: list[Finding] = []
    for cluster in clusters:
        findings.extend(_cluster_runtime_rules(cluster))
    return findings


def _cluster_runtime_rules(cluster: dict[str, Any]) -> list[Finding]:
    name = short_name(cluster.get("cluster"))
    if str(cluster.get("power_state") or "").lower() == "stopped":
        return [
            _mk(
                id="aks.runtime",
                resource_kind="aks",
                resource_name=name,
                severity="info",
                title=f"Cluster '{name}' is stopped — no runtime signals",
                detail=(
                    "The cluster is stopped, so its API server is not reachable "
                    "for live signals."
                ),
                doc_url=_DOC_NODE,
            )
        ]
    # The whole-cluster fetch failed (API server unreachable, kubeconfig, etc.).
    # Surface it honestly as indeterminate rather than an empty "all clear".
    if cluster.get("fetch_error"):
        return [
            _mk(
                id="aks.runtime",
                resource_kind="aks",
                resource_name=name,
                severity="indeterminate",
                title=f"Cluster '{name}' runtime could not be read",
                detail=(
                    "The cluster's Kubernetes API could not be reached to read "
                    f"live signals ({short_name(cluster.get('fetch_error'))})."
                ),
                recommendation=(
                    "Check the cluster's API server reachability and the "
                    "dashboard's kubeconfig token."
                ),
                doc_url=_DOC_NODE,
                observed={"error": short_name(cluster.get("fetch_error"))},
            )
        ]
    findings: list[Finding] = []
    findings.extend(_event_findings(name, cluster.get("events") or []))
    findings.extend(_pod_findings(name, cluster.get("pods") or []))
    findings.extend(_node_findings(name, cluster.get("nodes") or []))
    findings.extend(
        _workload_findings(name, cluster.get("jobs") or [], cluster.get("deployments") or [])
    )
    return findings


def _event_findings(cluster: str, events: list[dict[str, Any]]) -> list[Finding]:
    warnings = [e for e in events if str(e.get("type")) == "Warning"]
    if not warnings:
        return []
    findings: list[Finding] = []
    matched_reasons: set[str] = set()
    for klass in _EVENT_CLASSES:
        hits = [e for e in warnings if str(e.get("reason")) in klass["reasons"]]
        if not hits:
            continue
        matched_reasons |= klass["reasons"]
        total = sum(int(e.get("count") or 1) for e in hits)
        objects = _sample_objects(
            f"{e.get('involved_kind', '')}/{e.get('involved_name', '')}".strip("/") for e in hits
        )
        sample_msg = short_name(next((e.get("message") for e in hits if e.get("message")), ""))[
            :200
        ]
        findings.append(
            _mk(
                id=klass["id"],
                resource_kind="aks",
                resource_name=cluster,
                severity=klass["severity"],
                title=f"{klass['title']} on '{cluster}' ({total} event(s))",
                detail=klass["detail"] + (f" Example: {sample_msg}" if sample_msg else ""),
                recommendation=klass["recommendation"],
                doc_url=klass["doc_url"],
                observed={"count": str(total), "objects": objects},
            )
        )
    # Catch-all: any other Warning reason not in a known class, aggregated.
    other = [e for e in warnings if str(e.get("reason")) not in matched_reasons]
    if other:
        by_reason: dict[str, int] = {}
        for e in other:
            by_reason[str(e.get("reason") or "Unknown")] = by_reason.get(
                str(e.get("reason") or "Unknown"), 0
            ) + int(e.get("count") or 1)
        top = sorted(by_reason.items(), key=lambda kv: -kv[1])[:3]
        findings.append(
            _mk(
                id="events.other",
                resource_kind="aks",
                resource_name=cluster,
                severity="info",
                title=f"Other warning events on '{cluster}'",
                detail="Additional warning events were recorded: "
                + ", ".join(f"{r} ×{c}" for r, c in top)
                + ".",
                recommendation="Review the cluster events in the AKS card for context.",
                doc_url=_DOC_EVENTS,
                observed={"reasons": short_name(",".join(r for r, _ in top))},
            )
        )
    return findings


def _pod_findings(cluster: str, pods: list[dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []

    # Restart storms.
    crit_restart = [p for p in pods if int(p.get("restarts") or 0) >= _RESTART_CRIT]
    warn_restart = [p for p in pods if _RESTART_WARN <= int(p.get("restarts") or 0) < _RESTART_CRIT]
    if crit_restart:
        findings.append(
            _mk(
                id="pods.restarts",
                resource_kind="aks",
                resource_name=cluster,
                severity="critical",
                title=f"{len(crit_restart)} pod(s) on '{cluster}' are crash-looping",
                detail=f"Pods have restarted >= {_RESTART_CRIT} times — a persistent crash loop.",
                recommendation=(
                    "Inspect the container logs (`kubectl logs --previous`) of the listed pods."
                ),
                doc_url=_DOC_TROUBLESHOOT,
                observed={
                    "pods": _sample_objects(_pod_label(p) for p in crit_restart),
                    "max_restarts": str(max(int(p.get("restarts") or 0) for p in crit_restart)),
                },
            )
        )
    elif warn_restart:
        findings.append(
            _mk(
                id="pods.restarts",
                resource_kind="aks",
                resource_name=cluster,
                severity="warning",
                title=f"{len(warn_restart)} pod(s) on '{cluster}' are restarting frequently",
                detail=f"Pods have restarted >= {_RESTART_WARN} times.",
                recommendation=(
                    "Check the restarting pods' logs before they enter a full crash loop."
                ),
                doc_url=_DOC_TROUBLESHOOT,
                observed={"pods": _sample_objects(_pod_label(p) for p in warn_restart)},
            )
        )

    # Stuck Pending.
    pending = [
        p
        for p in pods
        if str(p.get("status")) == "Pending"
        and (_age_minutes(str(p.get("age") or "")) or 0) >= _PENDING_AGE_WARN_MIN
    ]
    if pending:
        findings.append(
            _mk(
                id="pods.pending",
                resource_kind="aks",
                resource_name=cluster,
                severity="warning",
                title=f"{len(pending)} pod(s) on '{cluster}' stuck in Pending",
                detail=(
                    f"Pods have been Pending for over {_PENDING_AGE_WARN_MIN} minutes "
                    "(unschedulable or waiting on resources)."
                ),
                recommendation=(
                    "Check scheduling events / node capacity / PVC binding for the listed pods."
                ),
                doc_url=_DOC_PENDING,
                observed={"pods": _sample_objects(_pod_label(p) for p in pending)},
            )
        )

    # Failed phase.
    failed = [p for p in pods if str(p.get("status")) == "Failed"]
    if failed:
        findings.append(
            _mk(
                id="pods.failed",
                resource_kind="aks",
                resource_name=cluster,
                severity="warning",
                title=f"{len(failed)} pod(s) on '{cluster}' are in the Failed phase",
                detail="Pods terminated with a non-zero exit and were not restarted.",
                recommendation="Inspect the failed pods' logs and the owning Job/controller.",
                doc_url=_DOC_TROUBLESHOOT,
                observed={"pods": _sample_objects(_pod_label(p) for p in failed)},
            )
        )

    # Not-ready running pods (ready < total) — excludes completed Jobs.
    not_ready = [
        p
        for p in pods
        if str(p.get("status")) == "Running" and _not_ready(str(p.get("ready") or ""))
    ]
    if not_ready:
        findings.append(
            _mk(
                id="pods.not_ready",
                resource_kind="aks",
                resource_name=cluster,
                severity="warning",
                title=f"{len(not_ready)} running pod(s) on '{cluster}' are not fully ready",
                detail=(
                    "Pods are Running but not all containers are Ready "
                    "(failing readiness probes)."
                ),
                recommendation=(
                    "Check readiness probes and container startup for the listed pods."
                ),
                doc_url=_DOC_TROUBLESHOOT,
                observed={"pods": _sample_objects(_pod_label(p) for p in not_ready)},
            )
        )
    return findings


def _node_findings(cluster: str, nodes: list[dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    not_ready = [n for n in nodes if str(n.get("status")) == "NotReady"]
    if not_ready:
        findings.append(
            _mk(
                id="nodes.not_ready",
                resource_kind="aks",
                resource_name=cluster,
                severity="critical",
                title=f"{len(not_ready)} node(s) on '{cluster}' are NotReady",
                detail="Nodes are NotReady — their pods are unschedulable or being evicted.",
                recommendation=(
                    "Inspect kubelet health on the affected nodes; the autoscaler may replace them."
                ),
                doc_url=_DOC_NODE,
                observed={"nodes": _sample_objects(str(n.get("name") or "") for n in not_ready)},
            )
        )
    for cond, label, rec in (
        ("disk_pressure", "disk pressure", "Free disk on the node or expand the OS/data disk."),
        ("memory_pressure", "memory pressure", "Reduce pod memory use or add capacity."),
        ("pid_pressure", "PID pressure", "Reduce process count or use a larger node SKU."),
    ):
        hit = [n for n in nodes if n.get(cond) is True]
        if hit:
            findings.append(
                _mk(
                    id=f"nodes.{cond}",
                    resource_kind="aks",
                    resource_name=cluster,
                    severity="warning",
                    title=f"{len(hit)} node(s) on '{cluster}' report {label}",
                    detail=f"Nodes report the {label} condition, which can evict pods.",
                    recommendation=rec,
                    doc_url=_DOC_NODE,
                    observed={"nodes": _sample_objects(str(n.get("name") or "") for n in hit)},
                )
            )
    cordoned = [n for n in nodes if n.get("unschedulable") is True]
    if cordoned:
        findings.append(
            _mk(
                id="nodes.cordoned",
                resource_kind="aks",
                resource_name=cluster,
                severity="info",
                title=f"{len(cordoned)} node(s) on '{cluster}' are cordoned (unschedulable)",
                detail="Cordoned nodes accept no new pods (often during an upgrade or drain).",
                recommendation=(
                    "Confirm the cordon is intentional; uncordon when maintenance is done."
                ),
                doc_url=_DOC_NODE,
                observed={"nodes": _sample_objects(str(n.get("name") or "") for n in cordoned)},
            )
        )
    return findings


def _workload_findings(
    cluster: str, jobs: list[dict[str, Any]], deployments: list[dict[str, Any]]
) -> list[Finding]:
    findings: list[Finding] = []

    failed_jobs = [j for j in jobs if str(j.get("status")) == "Failed"]
    if failed_jobs:
        findings.append(
            _mk(
                id="jobs.failed",
                resource_kind="aks",
                resource_name=cluster,
                severity="warning",
                title=f"{len(failed_jobs)} Job(s) on '{cluster}' have Failed",
                detail="Kubernetes Jobs reached the Failed condition (exhausted retries).",
                recommendation=(
                    "Inspect the failed Jobs' pod logs; for BLAST jobs check the search inputs."
                ),
                doc_url=_DOC_TROUBLESHOOT,
                observed={"jobs": _sample_objects(_job_label(j) for j in failed_jobs)},
            )
        )
    retrying = [
        j
        for j in jobs
        if str(j.get("status")) in {"Running", "Pending"} and int(j.get("failed") or 0) > 0
    ]
    if retrying:
        findings.append(
            _mk(
                id="jobs.retrying",
                resource_kind="aks",
                resource_name=cluster,
                severity="info",
                title=(
                    f"{len(retrying)} Job(s) on '{cluster}' have failed pods but are still retrying"
                ),
                detail="Jobs have one or more failed pods and have not yet exhausted retries.",
                recommendation=(
                    "Watch these Jobs; repeated pod failures usually end in a Failed Job."
                ),
                doc_url=_DOC_TROUBLESHOOT,
                observed={"jobs": _sample_objects(_job_label(j) for j in retrying)},
            )
        )

    under = [d for d in deployments if _under_replicated(d)]
    fully_down = [d for d in under if int(d.get("available") or 0) == 0]
    degraded = [d for d in under if int(d.get("available") or 0) > 0]
    if fully_down:
        findings.append(
            _mk(
                id="deployments.down",
                resource_kind="aks",
                resource_name=cluster,
                severity="critical",
                title=(
                    f"{len(fully_down)} Deployment(s) on '{cluster}' have zero "
                    "available replicas"
                ),
                detail="Deployments are scaled but no replica is available — the workload is down.",
                recommendation=(
                    "Inspect the Deployment's pods/events; this is an active "
                    "outage of that workload."
                ),
                doc_url=_DOC_TROUBLESHOOT,
                observed={"deployments": _sample_objects(_dep_label(d) for d in fully_down)},
            )
        )
    if degraded:
        findings.append(
            _mk(
                id="deployments.degraded",
                resource_kind="aks",
                resource_name=cluster,
                severity="warning",
                title=f"{len(degraded)} Deployment(s) on '{cluster}' are under-replicated",
                detail="Deployments have fewer ready replicas than desired (partial capacity).",
                recommendation=(
                    "Check why replicas are not ready (scheduling, probes, image pulls)."
                ),
                doc_url=_DOC_TROUBLESHOOT,
                observed={"deployments": _sample_objects(_dep_label(d) for d in degraded)},
            )
        )
    return findings


# ------------------------------------------------------------------ BLAST jobs


def _jobstate_rules(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="queue",
                id="blast.jobstate",
                title="BLAST job history could not be read",
                doc_url=_DOC_BLAST,
            )
        ]
    jobs: list[dict[str, Any]] = snap.data.get("jobs") or []
    if not jobs:
        return []
    findings: list[Finding] = []

    failed = [j for j in jobs if str(j.get("status")) == "failed"]
    if failed:
        codes: dict[str, int] = {}
        for j in failed:
            codes[str(j.get("error_code") or "unknown")] = (
                codes.get(str(j.get("error_code") or "unknown"), 0) + 1
            )
        top = sorted(codes.items(), key=lambda kv: -kv[1])[:3]
        findings.append(
            _mk(
                id="blast.failed_jobs",
                resource_kind="queue",
                resource_name="BLAST",
                severity="warning",
                title=f"{len(failed)} recent BLAST job(s) failed",
                detail="Recent BLAST searches ended in a failed state. Top error codes: "
                + ", ".join(f"{c} ×{n}" for c, n in top)
                + ".",
                recommendation=(
                    "Open the failed jobs in Recent searches to see the run-detail error."
                ),
                doc_url=_DOC_BLAST,
                observed={
                    "count": str(len(failed)),
                    "jobs": _sample_objects(str(j.get("job_id") or "") for j in failed),
                },
            )
        )

    stale = [j for j in jobs if str(j.get("status")) == "running" and _is_stale(j)]
    if stale:
        findings.append(
            _mk(
                id="blast.stale_jobs",
                resource_kind="queue",
                resource_name="BLAST",
                severity="warning",
                title=f"{len(stale)} BLAST job(s) have been running without progress",
                detail=(
                    "Jobs are still 'running' but their state has not updated in over "
                    f"{_STALE_RUNNING_HOURS}h — likely stuck."
                ),
                recommendation=(
                    "Check the cluster and the job's pods; the job may need to be "
                    "cancelled and resubmitted."
                ),
                doc_url=_DOC_BLAST,
                observed={"jobs": _sample_objects(str(j.get("job_id") or "") for j in stale)},
            )
        )
    return findings


def _is_stale(job: dict[str, Any]) -> bool:
    age = _age_minutes(str(job.get("updated_at") or ""))
    return age is not None and age >= _STALE_RUNNING_HOURS * 60


# --------------------------------------------------------------------- API routes


def _api_route_rules(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None or not snap.available:
        return []
    by_path: list[dict[str, Any]] = snap.data.get("by_path") or []
    if not by_path:
        return []
    findings: list[Finding] = []

    erroring = [
        p
        for p in by_path
        if int(p.get("count") or 0) >= 5
        and int(p.get("errors") or 0) > 0
        and (int(p.get("errors") or 0) / max(1, int(p.get("count") or 1))) >= 0.10
    ]
    if erroring:
        worst = max(
            erroring, key=lambda p: int(p.get("errors") or 0) / max(1, int(p.get("count") or 1))
        )
        rate = int(worst.get("errors") or 0) / max(1, int(worst.get("count") or 1))
        findings.append(
            _mk(
                id="api.route_errors",
                resource_kind="api",
                resource_name="API",
                severity="warning",
                title=f"{len(erroring)} API route(s) are returning errors",
                detail=f"Worst: {short_name(worst.get('path'))} at {rate * 100:.0f}% 5xx "
                f"({worst.get('errors')}/{worst.get('count')}).",
                recommendation=(
                    "Open the HTTP inspector for the failing route to see the error body."
                ),
                doc_url="https://learn.microsoft.com/azure/well-architected/operational-excellence/observability",
                observed={"routes": _sample_objects(short_name(p.get("path")) for p in erroring)},
            )
        )

    slow = [
        p
        for p in by_path
        if isinstance(p.get("p95_ms"), (int, float)) and float(p["p95_ms"]) >= 3000
    ]
    if slow:
        worst = max(slow, key=lambda p: float(p.get("p95_ms") or 0))
        findings.append(
            _mk(
                id="api.route_latency",
                resource_kind="api",
                resource_name="API",
                severity="info",
                title=f"{len(slow)} API route(s) are slow (p95 >= 3s)",
                detail=(
                    f"Worst: {short_name(worst.get('path'))} at p95 "
                    f"{int(float(worst.get('p95_ms') or 0))} ms."
                ),
                recommendation=(
                    "Profile the slow route; check downstream ARM/K8s calls and throttling."
                ),
                doc_url="https://learn.microsoft.com/azure/well-architected/performance-efficiency/",
                observed={"routes": _sample_objects(short_name(p.get("path")) for p in slow)},
            )
        )
    return findings


# --------------------------------------------------------------------- helpers


def _sample_objects(names) -> str:
    seen: list[str] = []
    for n in names:
        n = str(n).strip()
        if n and n not in seen:
            seen.append(n)
        if len(seen) >= _MAX_SAMPLE_OBJECTS:
            break
    return short_name(", ".join(seen))


def _pod_label(p: dict[str, Any]) -> str:
    ns = str(p.get("namespace") or "")
    name = str(p.get("name") or "")
    return f"{ns}/{name}" if ns else name


def _job_label(j: dict[str, Any]) -> str:
    return _pod_label(j)


def _dep_label(d: dict[str, Any]) -> str:
    return _pod_label(d)


def _not_ready(ready: str) -> bool:
    """`ready` is 'r/t'; True when r < t (and t > 0)."""
    if "/" not in ready:
        return False
    r, _, t = ready.partition("/")
    try:
        return int(t) > 0 and int(r) < int(t)
    except ValueError:
        return False


def _under_replicated(d: dict[str, Any]) -> bool:
    ready = str(d.get("ready") or "")
    if "/" not in ready:
        return False
    r, _, desired = ready.partition("/")
    try:
        return int(desired) > 0 and int(r) < int(desired)
    except ValueError:
        return False

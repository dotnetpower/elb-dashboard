"""Availability & Performance rule catalog.

Pure best-practice checks for the Availability/Performance category. Each rule
reads one `ResourceSnapshot` and returns zero or more `Finding`s. No IO here —
rules are fed synthetic snapshots by the golden tests.

Responsibility: Map the fetched AKS node-pressure / sidecar health / API latency
    snapshots to Availability findings, honouring the "stopped cluster is a
    warning here (cannot run work)" and "failure/permission → indeterminate"
    rules. Node/pod signals are aggregated, never one finding per node.
Edit boundaries: Pure functions only. Thresholds are module constants.
Key entry points: `evaluate_availability`.
Risky contracts: A new id/severity stays additive. Permission-denied snapshots
    MUST yield `indeterminate`, never `critical`.
Validation: `uv run pytest -q api/tests/test_diagnostics_rules.py`.
"""

from __future__ import annotations

from typing import Any

from api.services.diagnostics.models import Finding, ResourceSnapshot
from api.services.diagnostics.rules.common import indeterminate_for, short_name
from api.services.diagnostics.rules.specs import RuleSpec, evaluate_specs, want_true

_PILLAR = "Performance Efficiency"
_CATEGORY = "availability"

_DOC_AKS_SCALE = "https://learn.microsoft.com/azure/aks/cluster-autoscaler-overview"
_DOC_AKS_NODE = "https://learn.microsoft.com/azure/aks/concepts-scale"
_DOC_AKS_CNI = "https://learn.microsoft.com/azure/aks/concepts-network"
_DOC_AKS_LB = "https://learn.microsoft.com/azure/aks/load-balancer-standard"
_DOC_AKS_MONITOR = (
    "https://learn.microsoft.com/azure/azure-monitor/containers/container-insights-overview"
)
_DOC_CONTAINER_APP = "https://learn.microsoft.com/azure/container-apps/observability"

# Declarative AKS performance/observability checks over the cluster config that
# the availability snapshot carries alongside node pressure.
_AKS_PERF_SPECS: list[RuleSpec] = [
    RuleSpec(
        id="aks.network_plugin",
        resource_kind="aks",
        pillar=_PILLAR,
        field="network_plugin",
        title_ok="Cluster uses the Azure CNI network plugin",
        title_bad="Cluster uses the kubenet network plugin",
        detail_ok="Azure CNI gives pods VNet IPs for better performance and policy support.",
        detail_bad="kubenet adds a routing hop and limits network policy options.",
        recommendation="Consider Azure CNI for direct VNet integration and network policy support.",
        doc_url=_DOC_AKS_CNI,
        bad_severity="info",
        compliant=lambda v: None if v is None else str(v).lower() == "azure",
    ),
    RuleSpec(
        id="aks.load_balancer_sku",
        resource_kind="aks",
        pillar=_PILLAR,
        field="load_balancer_sku",
        title_ok="Cluster uses the Standard load balancer",
        title_bad="Cluster uses the Basic load balancer",
        detail_ok="The Standard load balancer supports zones and higher scale.",
        detail_bad="The Basic load balancer has no zone support and lower scale limits.",
        recommendation="Use the Standard load balancer SKU for zone support and scale.",
        doc_url=_DOC_AKS_LB,
        compliant=lambda v: None if v is None else str(v).lower() == "standard",
    ),
    RuleSpec(
        id="aks.monitoring_addon",
        resource_kind="aks",
        pillar=_PILLAR,
        field="addon_monitoring",
        title_ok="Container Insights monitoring is enabled",
        title_bad="Container Insights monitoring is not enabled",
        detail_ok="Cluster and node metrics/logs flow to Azure Monitor.",
        detail_bad="Without Container Insights, cluster performance is hard to observe.",
        recommendation="Enable the Azure Monitor (Container Insights) add-on.",
        doc_url=_DOC_AKS_MONITOR,
        compliant=want_true,
    ),
]
_DOC_PERF = "https://learn.microsoft.com/azure/well-architected/performance-efficiency/"

# API surface thresholds. Conservative — these are warnings, not criticals, so a
# brief latency spike does not page anyone.
_API_ERROR_RATE_WARN = 0.05  # 5% 5xx over the window
_API_ERROR_RATE_CRIT = 0.20  # 20% 5xx is an active outage
_API_P95_WARN_MS = 2000.0
_SIDECAR_CPU_WARN_PCT = 90.0


def evaluate_availability(snapshots: dict[str, ResourceSnapshot]) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_aks_pressure_rules(snapshots.get("aks")))
    findings.extend(_sidecar_rules(snapshots.get("container_app")))
    findings.extend(_api_rules(snapshots.get("api")))
    return findings


def _mk(**kwargs: Any) -> Finding:
    return Finding(category=_CATEGORY, pillar=_PILLAR, **kwargs)


# --------------------------------------------------------------------- AKS nodes


def _aks_pressure_rules(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="aks",
                id="aks.node_pressure",
                title="AKS node capacity could not be verified",
                doc_url=_DOC_AKS_NODE,
            )
        ]
    clusters: list[dict[str, Any]] = snap.data.get("clusters") or []
    if not clusters:
        return [
            _mk(
                id="aks.node_pressure",
                resource_kind="aks",
                severity="info",
                title="No running AKS cluster to assess",
                detail="No managed cluster was discovered to measure node capacity.",
                doc_url=_DOC_AKS_NODE,
            )
        ]
    findings: list[Finding] = []
    for entry in clusters:
        findings.extend(_cluster_pressure_rules(entry))
        # Static performance/observability config checks (network plugin, LB
        # SKU, monitoring add-on). The availability snapshot carries the cluster
        # config under `config`; tolerate its absence (older callers).
        config = entry.get("config")
        if isinstance(config, dict):
            findings.extend(
                evaluate_specs(
                    _AKS_PERF_SPECS,
                    config,
                    category=_CATEGORY,
                    resource_name=short_name(entry.get("cluster")),
                )
            )
    return findings


def _cluster_pressure_rules(entry: dict[str, Any]) -> list[Finding]:
    name = short_name(entry.get("cluster"))
    power = (entry.get("power_state") or "").strip()
    if power.lower() == "stopped":
        return [
            _mk(
                id="aks.node_pressure",
                resource_kind="aks",
                resource_name=name,
                severity="warning",
                title=f"Cluster '{name}' is stopped — no capacity available",
                detail="A stopped cluster cannot run BLAST work until it is started.",
                recommendation=("Start the cluster from the AKS card before submitting a search."),
                doc_url=_DOC_AKS_NODE,
                observed={"power_state": power},
            )
        ]

    pressure = entry.get("pressure") or {}
    if not pressure.get("reachable", False):
        return [
            _mk(
                id="aks.node_pressure",
                resource_kind="aks",
                resource_name=name,
                severity="indeterminate",
                title=f"Cluster '{name}' node capacity could not be read",
                detail="The Kubernetes API was not reachable to measure request pressure.",
                recommendation=(
                    "Re-run once the cluster is reachable; check the kubeconfig / network path."
                ),
                doc_url=_DOC_AKS_NODE,
                observed={"reachable": "false"},
            )
        ]

    threshold = float(pressure.get("high_pressure_threshold_pct", 90) or 90)
    pools: dict[str, Any] = pressure.get("pools") or {}
    hot = []
    for pool_name, pool in pools.items():
        cpu = float(pool.get("cpu_request_pct", 0) or 0)
        mem = float(pool.get("memory_request_pct", 0) or 0)
        if cpu >= threshold or mem >= threshold:
            hot.append((pool_name, cpu, mem))
    if hot:
        worst = max(hot, key=lambda t: max(t[1], t[2]))
        return [
            _mk(
                id="aks.node_pressure",
                resource_kind="aks",
                resource_name=name,
                severity="warning",
                title=f"Cluster '{name}' has {len(hot)} agent pool(s) under high request pressure",
                detail=(
                    f"Pool '{worst[0]}' is at {int(worst[1])}% CPU / {int(worst[2])}% memory "
                    f"requested (threshold {int(threshold)}%). New pods may stay Pending."
                ),
                recommendation=(
                    "Scale the pool out or enable the cluster autoscaler so pods can schedule."
                ),
                doc_url=_DOC_AKS_SCALE,
                observed={
                    "pools_over_threshold": str(len(hot)),
                    "worst_pool": short_name(worst[0]),
                },
            )
        ]
    return [
        _mk(
            id="aks.node_pressure",
            resource_kind="aks",
            resource_name=name,
            severity="ok",
            title=f"Cluster '{name}' has node capacity headroom",
            detail=f"All agent pools are below the {int(threshold)}% request-pressure threshold.",
            doc_url=_DOC_AKS_NODE,
        )
    ]


# ----------------------------------------------------------------- Container App


def _sidecar_rules(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="container_app",
                id="container_app.sidecars",
                title="Sidecar health could not be verified",
                doc_url=_DOC_CONTAINER_APP,
            )
        ]
    sidecars: dict[str, Any] = snap.data.get("sidecars") or {}
    if not sidecars:
        return []
    down = [n for n, s in sidecars.items() if (s or {}).get("health") == "down"]
    degraded = [n for n, s in sidecars.items() if (s or {}).get("health") == "degraded"]
    hot_cpu = [
        n
        for n, s in sidecars.items()
        if isinstance((s or {}).get("cpu_pct"), (int, float))
        and float(s["cpu_pct"]) >= _SIDECAR_CPU_WARN_PCT
    ]
    findings: list[Finding] = []
    if down:
        findings.append(
            _mk(
                id="container_app.sidecars",
                resource_kind="container_app",
                severity="critical",
                title=f"{len(down)} sidecar(s) are down",
                detail=f"Down: {', '.join(sorted(down))}. The control plane is degraded.",
                recommendation=(
                    "Inspect the sidecar logs; a down api/worker/redis blocks dashboard actions."
                ),
                doc_url=_DOC_CONTAINER_APP,
                observed={"down": short_name(",".join(sorted(down)))},
            )
        )
    elif degraded or hot_cpu:
        parts = []
        if degraded:
            parts.append(f"degraded: {', '.join(sorted(degraded))}")
        if hot_cpu:
            parts.append(f"high CPU: {', '.join(sorted(hot_cpu))}")
        findings.append(
            _mk(
                id="container_app.sidecars",
                resource_kind="container_app",
                severity="warning",
                title="Some sidecars are under pressure",
                detail="; ".join(parts) + ".",
                recommendation=(
                    "Watch the affected sidecars; sustained pressure may slow the dashboard."
                ),
                doc_url=_DOC_CONTAINER_APP,
                observed={"degraded": str(len(degraded)), "high_cpu": str(len(hot_cpu))},
            )
        )
    else:
        findings.append(
            _mk(
                id="container_app.sidecars",
                resource_kind="container_app",
                severity="ok",
                title="All sidecars are healthy",
                detail=f"{len(sidecars)} sidecars reporting healthy.",
                doc_url=_DOC_CONTAINER_APP,
            )
        )
    return findings


# ------------------------------------------------------------------- API surface


def _api_rules(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="api",
                id="api.latency",
                title="API latency could not be measured",
                doc_url=_DOC_PERF,
            )
        ]
    data = snap.data
    if data.get("degraded") or (data.get("total", 0) or 0) == 0:
        return [
            _mk(
                id="api.latency",
                resource_kind="api",
                severity="info",
                title="No recent API traffic to assess",
                detail="No requests were recorded in the last 15 minutes.",
                doc_url=_DOC_PERF,
            )
        ]
    error_rate = float(data.get("error_rate", 0) or 0)
    p95 = data.get("p95_ms")
    findings: list[Finding] = []

    if error_rate >= _API_ERROR_RATE_CRIT:
        findings.append(
            _mk(
                id="api.error_rate",
                resource_kind="api",
                severity="critical",
                title=f"API error rate is {error_rate * 100:.0f}%",
                detail=f"{data.get('errors', 0)} of {data.get('total', 0)} requests returned 5xx.",
                recommendation=(
                    "Inspect the failing routes in the HTTP inspector; this is an active outage."
                ),
                doc_url=_DOC_PERF,
                observed={"error_rate": f"{error_rate:.3f}"},
            )
        )
    elif error_rate >= _API_ERROR_RATE_WARN:
        findings.append(
            _mk(
                id="api.error_rate",
                resource_kind="api",
                severity="warning",
                title=f"API error rate is elevated ({error_rate * 100:.0f}%)",
                detail=f"{data.get('errors', 0)} of {data.get('total', 0)} requests returned 5xx.",
                recommendation="Check the failing routes in the HTTP inspector.",
                doc_url=_DOC_PERF,
                observed={"error_rate": f"{error_rate:.3f}"},
            )
        )
    else:
        findings.append(
            _mk(
                id="api.error_rate",
                resource_kind="api",
                severity="ok",
                title="API error rate is healthy",
                detail=f"{error_rate * 100:.1f}% 5xx over the last 15 minutes.",
                doc_url=_DOC_PERF,
                observed={"error_rate": f"{error_rate:.3f}"},
            )
        )

    if isinstance(p95, (int, float)) and float(p95) >= _API_P95_WARN_MS:
        findings.append(
            _mk(
                id="api.latency",
                resource_kind="api",
                severity="warning",
                title=f"API p95 latency is {int(p95)} ms",
                detail=(
                    f"95th-percentile latency exceeds {int(_API_P95_WARN_MS)} ms over the window."
                ),
                recommendation=(
                    "Investigate slow routes; check ARM throttling and downstream timeouts."
                ),
                doc_url=_DOC_PERF,
                observed={"p95_ms": str(int(p95))},
            )
        )
    elif isinstance(p95, (int, float)):
        findings.append(
            _mk(
                id="api.latency",
                resource_kind="api",
                severity="ok",
                title="API latency is within budget",
                detail=f"p95 {int(p95)} ms over the last 15 minutes.",
                doc_url=_DOC_PERF,
                observed={"p95_ms": str(int(p95))},
            )
        )
    return findings

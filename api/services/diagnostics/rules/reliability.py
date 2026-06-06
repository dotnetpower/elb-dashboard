"""Reliability rule catalog.

Pure best-practice checks for the Reliability category. Each rule reads one
`ResourceSnapshot` and returns zero or more `Finding`s. No IO here — rules are
fed synthetic snapshots by `api/tests/test_diagnostics_rules.py`, so a threshold
change touches only this file + the golden fixture.

Responsibility: Map the fetched AKS / Storage / ACR / Container App snapshots to
    Reliability findings, honouring the charter's by-design exceptions and the
    "failure/permission → indeterminate, never critical" rule.
Edit boundaries: Pure functions only (snapshot dict in, findings out). No Azure
    SDK, no HTTP, no fetch. Time-sensitive facts carry an `as_of` and degrade to
    `info`, never a stale `critical`.
Key entry points: `evaluate_reliability`.
Risky contracts: A new `severity`/`id` must stay additive (the SPA default-
    handles unknown ids). Permission-denied snapshots MUST yield `indeterminate`.
Validation: `uv run pytest -q api/tests/test_diagnostics_rules.py`.
"""

from __future__ import annotations

from typing import Any

from api.services.diagnostics.models import Finding, ResourceSnapshot
from api.services.diagnostics.rules.common import indeterminate_for, short_name

_PILLAR = "Reliability"
_CATEGORY = "reliability"

# Microsoft Learn anchors (stable doc roots).
_DOC_AKS = "https://learn.microsoft.com/azure/aks/best-practices"
_DOC_AKS_UPGRADE = "https://learn.microsoft.com/azure/aks/supported-kubernetes-versions"
_DOC_AKS_SCALE = "https://learn.microsoft.com/azure/aks/cluster-autoscaler-overview"
_DOC_STORAGE_REDUNDANCY = "https://learn.microsoft.com/azure/storage/common/storage-redundancy"
_DOC_ACR_SKU = "https://learn.microsoft.com/azure/container-registry/container-registry-skus"

# k8s minor-version support floor, as a conservative cutoff. Versions below this
# are flagged `warning` ("plan an upgrade"), NOT `critical`, because the exact
# AKS support window shifts over time and a stale `critical` would be a false
# alarm. Bump `(minor, as_of)` together when refreshing the catalog.
_K8S_MIN_SUPPORTED_MINOR = 29  # i.e. 1.29
_K8S_AS_OF = "2026-06-01"


def evaluate_reliability(snapshots: dict[str, ResourceSnapshot]) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_aks_rules(snapshots.get("aks")))
    findings.extend(_storage_rules(snapshots.get("storage")))
    findings.extend(_acr_rules(snapshots.get("acr")))
    findings.extend(_container_app_rules(snapshots.get("container_app")))
    return findings


def _mk(**kwargs: Any) -> Finding:
    return Finding(category=_CATEGORY, pillar=_PILLAR, **kwargs)


# --------------------------------------------------------------------------- AKS


def _aks_rules(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="aks",
                id="aks.reachable",
                title="AKS cluster state could not be verified",
                doc_url=_DOC_AKS,
            )
        ]
    clusters: list[dict[str, Any]] = snap.data.get("clusters") or []
    if not clusters:
        return [
            _mk(
                id="aks.present",
                resource_kind="aks",
                severity="info",
                title="No ElasticBLAST-managed AKS cluster found",
                detail=(
                    "No managed cluster was discovered in the subscription. This "
                    "is normal before the first cluster is provisioned."
                ),
                recommendation=(
                    "Provision a cluster from the Dashboard when you run your first search."
                ),
                doc_url=_DOC_AKS,
            )
        ]
    findings: list[Finding] = []
    for cluster in clusters:
        findings.extend(_aks_cluster_rules(cluster))
    return findings


def _aks_cluster_rules(cluster: dict[str, Any]) -> list[Finding]:
    name = short_name(cluster.get("name"))
    findings: list[Finding] = []

    provisioning = (cluster.get("provisioning_state") or "").strip()
    power = (cluster.get("power_state") or "").strip()

    # Provisioning state — anything but Succeeded is an active reliability risk,
    # except a deliberately Stopped cluster (cost saving) which is `info`.
    if power.lower() == "stopped":
        findings.append(
            _mk(
                id="aks.power_state",
                resource_kind="aks",
                resource_name=name,
                severity="info",
                expected_by_charter=True,
                title=f"Cluster '{name}' is stopped",
                detail=(
                    "The cluster is stopped — a deliberate cost-saving state. "
                    "Start it before submitting work."
                ),
                recommendation="Start the cluster from the AKS card when you need to run a search.",
                doc_url=_DOC_AKS,
                observed={"power_state": power or "unknown"},
            )
        )
    elif provisioning and provisioning != "Succeeded":
        findings.append(
            _mk(
                id="aks.provisioning_state",
                resource_kind="aks",
                resource_name=name,
                severity="critical",
                title=f"Cluster '{name}' is not in a healthy provisioning state",
                detail=f"provisioning_state='{provisioning}' (expected 'Succeeded').",
                recommendation=(
                    "Inspect the cluster in the AKS card; a failed/updating state "
                    "blocks job submission."
                ),
                doc_url=_DOC_AKS,
                observed={"provisioning_state": provisioning, "power_state": power or "unknown"},
            )
        )
    else:
        findings.append(
            _mk(
                id="aks.provisioning_state",
                resource_kind="aks",
                resource_name=name,
                severity="ok",
                title=f"Cluster '{name}' is provisioned and running",
                detail="provisioning_state='Succeeded'.",
                doc_url=_DOC_AKS,
                observed={"provisioning_state": provisioning or "Succeeded"},
            )
        )

    # Autoscaler — recommended for a bursty BLAST workload pool.
    pools: list[dict[str, Any]] = cluster.get("agent_pools") or []
    user_pools = [p for p in pools if (p.get("mode") or "").lower() != "system"] or pools
    any_autoscale = any(p.get("enable_auto_scaling") for p in user_pools)
    if user_pools and not any_autoscale:
        findings.append(
            _mk(
                id="aks.autoscale",
                resource_kind="aks",
                resource_name=name,
                severity="warning",
                title=f"Cluster '{name}' has no autoscaling workload pool",
                detail="No workload agent pool has the cluster autoscaler enabled.",
                recommendation=(
                    "Enable the cluster autoscaler so BLAST bursts get nodes and "
                    "idle nodes scale in."
                ),
                doc_url=_DOC_AKS_SCALE,
                observed={"workload_pools": str(len(user_pools))},
            )
        )
    elif user_pools:
        findings.append(
            _mk(
                id="aks.autoscale",
                resource_kind="aks",
                resource_name=name,
                severity="ok",
                title=f"Cluster '{name}' autoscaling is enabled",
                detail="At least one workload pool has the cluster autoscaler enabled.",
                doc_url=_DOC_AKS_SCALE,
            )
        )

    # Kubernetes version support floor (conservative — warning, not critical).
    findings.append(_aks_version_finding(name, cluster.get("k8s_version")))
    return findings


def _aks_version_finding(name: str, version: str | None) -> Finding:
    minor = _parse_minor(version)
    if minor is None:
        return _mk(
            id="aks.k8s_version",
            resource_kind="aks",
            resource_name=name,
            severity="info",
            title=f"Cluster '{name}' Kubernetes version could not be parsed",
            detail=(
                f"Reported version '{version or 'unknown'}'. "
                "Verify it against the AKS support policy."
            ),
            recommendation=(
                "Confirm the cluster runs a currently-supported Kubernetes minor version."
            ),
            doc_url=_DOC_AKS_UPGRADE,
            observed={"k8s_version": version or "unknown"},
        )
    if minor < _K8S_MIN_SUPPORTED_MINOR:
        return _mk(
            id="aks.k8s_version",
            resource_kind="aks",
            resource_name=name,
            severity="warning",
            title=f"Cluster '{name}' may be running an out-of-support Kubernetes version",
            detail=(
                f"Version '{version}' is below the 1.{_K8S_MIN_SUPPORTED_MINOR} support floor "
                f"tracked as of {_K8S_AS_OF}. Verify against the current AKS support policy."
            ),
            recommendation="Plan a Kubernetes upgrade to a supported minor version.",
            doc_url=_DOC_AKS_UPGRADE,
            rule_version=f"as_of:{_K8S_AS_OF}",
            observed={"k8s_version": version or "unknown"},
        )
    return _mk(
        id="aks.k8s_version",
        resource_kind="aks",
        resource_name=name,
        severity="ok",
        title=f"Cluster '{name}' Kubernetes version is within the tracked support floor",
        detail=(
            f"Version '{version}' is at or above 1.{_K8S_MIN_SUPPORTED_MINOR} (as of {_K8S_AS_OF})."
        ),
        doc_url=_DOC_AKS_UPGRADE,
        rule_version=f"as_of:{_K8S_AS_OF}",
        observed={"k8s_version": version or "unknown"},
    )


def _parse_minor(version: str | None) -> int | None:
    if not version:
        return None
    parts = str(version).lstrip("v").split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


# ----------------------------------------------------------------------- Storage


def _storage_rules(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="storage",
                id="storage.reachable",
                title="Storage account redundancy could not be verified",
                doc_url=_DOC_STORAGE_REDUNDANCY,
            )
        ]
    name = short_name(snap.data.get("name"))
    sku = (snap.data.get("sku") or "").strip()
    findings: list[Finding] = []

    # Redundancy: LRS keeps a single region's three copies. For durable
    # research artifacts ZRS/GRS is the best practice.
    if sku.endswith("LRS"):
        findings.append(
            _mk(
                id="storage.redundancy",
                resource_kind="storage",
                resource_name=name,
                severity="warning",
                title=f"Storage account '{name}' uses single-region redundancy (LRS)",
                detail=f"SKU '{sku}' stores three copies in one datacentre only.",
                recommendation=(
                    "Consider ZRS (zone) or GRS/RA-GRS (geo) redundancy for "
                    "durable BLAST artifacts."
                ),
                doc_url=_DOC_STORAGE_REDUNDANCY,
                observed={"sku": sku},
            )
        )
    elif sku:
        findings.append(
            _mk(
                id="storage.redundancy",
                resource_kind="storage",
                resource_name=name,
                severity="ok",
                title=f"Storage account '{name}' uses zone/geo redundancy",
                detail=f"SKU '{sku}'.",
                doc_url=_DOC_STORAGE_REDUNDANCY,
                observed={"sku": sku},
            )
        )
    return findings


# --------------------------------------------------------------------------- ACR


def _acr_rules(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None:
        return []
    if not snap.available:
        return [
            indeterminate_for(
                snap,
                category=_CATEGORY,
                pillar=_PILLAR,
                resource_kind="acr",
                id="acr.reachable",
                title="Container registry SKU could not be verified",
                doc_url=_DOC_ACR_SKU,
            )
        ]
    name = short_name(snap.data.get("name"))
    sku = (snap.data.get("sku") or "").strip()
    if sku.lower() == "basic":
        return [
            _mk(
                id="acr.sku",
                resource_kind="acr",
                resource_name=name,
                severity="warning",
                title=f"Registry '{name}' is on the Basic SKU",
                detail="Basic has no zone redundancy and no geo-replication.",
                recommendation=(
                    "Use the Premium SKU for zone-redundant, geo-replicated "
                    "image pulls in production."
                ),
                doc_url=_DOC_ACR_SKU,
                observed={"sku": sku},
            )
        ]
    if sku:
        return [
            _mk(
                id="acr.sku",
                resource_kind="acr",
                resource_name=name,
                severity="ok",
                title=f"Registry '{name}' SKU supports redundancy",
                detail=f"SKU '{sku}'.",
                doc_url=_DOC_ACR_SKU,
                observed={"sku": sku},
            )
        ]
    return []


# ------------------------------------------------------------------- Container App


def _container_app_rules(snap: ResourceSnapshot | None) -> list[Finding]:
    if snap is None or not snap.available:
        return []
    if not snap.data.get("deployed"):
        return []  # local dev — no Container App to assess.
    name = short_name(snap.data.get("name"))
    return [
        _mk(
            id="container_app.replicas",
            resource_kind="container_app",
            resource_name=name,
            severity="info",
            expected_by_charter=True,
            title="Control plane runs a single pinned replica",
            detail=(
                "The Container App is pinned to minReplicas=1 / maxReplicas=1 — a "
                "deliberate cost design in the charter, not a defect. The beat "
                "reconciler rebuilds queue state from the jobstate table on restart."
            ),
            recommendation="No action — this is the intended single-revision cost posture.",
            doc_url="https://learn.microsoft.com/azure/container-apps/scale-app",
            observed={"min_replicas": "1", "max_replicas": "1"},
        )
    ]

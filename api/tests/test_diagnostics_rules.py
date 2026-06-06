"""Golden tests for the diagnostics rule catalogs.

Feeds synthetic `ResourceSnapshot`s to the pure rule evaluators and asserts the
exact finding set, so a threshold change touches only the catalog + this fixture.

Responsibility: Pin the Reliability rule outputs (severity, id, by-design caps,
    indeterminate-on-failure) against curated snapshots.
Edit boundaries: Pure-function assertions only — no TestClient, no Azure.
Key entry points: the `test_*` functions below.
Risky contracts: A permission-denied snapshot MUST yield `indeterminate`, never
    `critical` — that is the Reader-persona guard, asserted here.
Validation: `uv run pytest -q api/tests/test_diagnostics_rules.py`.
"""

from __future__ import annotations

from api.services.diagnostics.models import ResourceSnapshot
from api.services.diagnostics.rules import evaluate_reliability


def _by_id(findings):
    return {f.id: f for f in findings}


def test_healthy_cluster_is_ok() -> None:
    snap = {
        "aks": ResourceSnapshot(
            kind="aks",
            data={
                "clusters": [
                    {
                        "name": "elb-cluster-01",
                        "resource_group": "rg-elb",
                        "provisioning_state": "Succeeded",
                        "power_state": "Running",
                        "k8s_version": "1.30.4",
                        "agent_pools": [
                            {"mode": "System", "enable_auto_scaling": False},
                            {"mode": "User", "enable_auto_scaling": True},
                        ],
                    }
                ]
            },
        )
    }
    findings = _by_id(evaluate_reliability(snap))
    assert findings["aks.provisioning_state"].severity == "ok"
    assert findings["aks.autoscale"].severity == "ok"
    assert findings["aks.k8s_version"].severity == "ok"


def test_failed_provisioning_is_critical() -> None:
    snap = {
        "aks": ResourceSnapshot(
            kind="aks",
            data={
                "clusters": [
                    {
                        "name": "c1",
                        "provisioning_state": "Failed",
                        "power_state": "Running",
                        "k8s_version": "1.30.0",
                        "agent_pools": [{"mode": "User", "enable_auto_scaling": True}],
                    }
                ]
            },
        )
    }
    findings = _by_id(evaluate_reliability(snap))
    assert findings["aks.provisioning_state"].severity == "critical"


def test_stopped_cluster_is_info_not_critical() -> None:
    snap = {
        "aks": ResourceSnapshot(
            kind="aks",
            data={
                "clusters": [
                    {
                        "name": "c1",
                        "provisioning_state": "Succeeded",
                        "power_state": "Stopped",
                        "k8s_version": "1.30.0",
                        "agent_pools": [{"mode": "User", "enable_auto_scaling": True}],
                    }
                ]
            },
        )
    }
    findings = _by_id(evaluate_reliability(snap))
    assert findings["aks.power_state"].severity == "info"
    assert findings["aks.power_state"].expected_by_charter is True
    # No provisioning_state finding should be emitted for a stopped cluster.
    assert "aks.provisioning_state" not in findings


def test_no_autoscale_is_warning() -> None:
    snap = {
        "aks": ResourceSnapshot(
            kind="aks",
            data={
                "clusters": [
                    {
                        "name": "c1",
                        "provisioning_state": "Succeeded",
                        "power_state": "Running",
                        "k8s_version": "1.30.0",
                        "agent_pools": [{"mode": "User", "enable_auto_scaling": False}],
                    }
                ]
            },
        )
    }
    findings = _by_id(evaluate_reliability(snap))
    assert findings["aks.autoscale"].severity == "warning"


def test_old_k8s_is_warning_not_critical() -> None:
    snap = {
        "aks": ResourceSnapshot(
            kind="aks",
            data={
                "clusters": [
                    {
                        "name": "c1",
                        "provisioning_state": "Succeeded",
                        "power_state": "Running",
                        "k8s_version": "1.27.9",
                        "agent_pools": [{"mode": "User", "enable_auto_scaling": True}],
                    }
                ]
            },
        )
    }
    findings = _by_id(evaluate_reliability(snap))
    # Stale support facts degrade to warning, never a false critical.
    assert findings["aks.k8s_version"].severity == "warning"


def test_storage_lrs_is_warning_grs_is_ok() -> None:
    lrs = {
        "storage": ResourceSnapshot(kind="storage", data={"name": "stelb", "sku": "Standard_LRS"})
    }
    grs = {
        "storage": ResourceSnapshot(kind="storage", data={"name": "stelb", "sku": "Standard_GRS"})
    }
    assert _by_id(evaluate_reliability(lrs))["storage.redundancy"].severity == "warning"
    assert _by_id(evaluate_reliability(grs))["storage.redundancy"].severity == "ok"


def test_acr_basic_is_warning_premium_is_ok() -> None:
    basic = {"acr": ResourceSnapshot(kind="acr", data={"name": "acrelb", "sku": "Basic"})}
    premium = {"acr": ResourceSnapshot(kind="acr", data={"name": "acrelb", "sku": "Premium"})}
    assert _by_id(evaluate_reliability(basic))["acr.sku"].severity == "warning"
    assert _by_id(evaluate_reliability(premium))["acr.sku"].severity == "ok"


def test_container_app_single_replica_is_info_by_design() -> None:
    snap = {
        "container_app": ResourceSnapshot(
            kind="container_app",
            data={"name": "ca-elb-dashboard", "deployed": True, "min_replicas": 1},
        )
    }
    finding = _by_id(evaluate_reliability(snap))["container_app.replicas"]
    assert finding.severity == "info"
    assert finding.expected_by_charter is True


def test_permission_denied_is_indeterminate_never_critical() -> None:
    """Reader-persona guard: a denied fetch must never surface as critical."""
    for kind, rule_id in (
        ("aks", "aks.reachable"),
        ("storage", "storage.reachable"),
        ("acr", "acr.reachable"),
    ):
        snap = {
            kind: ResourceSnapshot(kind=kind, available=False, reason="forbidden", access="denied")
        }
        findings = _by_id(evaluate_reliability(snap))
        assert findings[rule_id].severity == "indeterminate"
        assert all(f.severity != "critical" for f in findings.values())


def test_no_clusters_is_info() -> None:
    snap = {"aks": ResourceSnapshot(kind="aks", data={"clusters": []})}
    findings = _by_id(evaluate_reliability(snap))
    assert findings["aks.present"].severity == "info"

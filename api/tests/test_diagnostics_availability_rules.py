"""Golden tests for the Availability rule catalog.

Responsibility: Pin the Availability rule outputs (node pressure aggregation,
    stopped-cluster warning, sidecar health, API latency/error rate, and the
    indeterminate-on-failure guard) against curated snapshots.
Edit boundaries: Pure-function assertions only.
Key entry points: the `test_*` functions below.
Risky contracts: A permission-denied/unreachable snapshot MUST yield
    `indeterminate`, never `critical`.
Validation: `uv run pytest -q api/tests/test_diagnostics_availability_rules.py`.
"""

from __future__ import annotations

from api.services.diagnostics.models import ResourceSnapshot
from api.services.diagnostics.rules import evaluate_availability


def _by_id(findings):
    return {f.id: f for f in findings}


def _pressure(cpu: int, mem: int, threshold: int = 90) -> dict:
    return {
        "reachable": True,
        "high_pressure_threshold_pct": threshold,
        "pools": {"blastpool": {"nodes": 2, "cpu_request_pct": cpu, "memory_request_pct": mem}},
    }


def test_high_node_pressure_is_warning() -> None:
    snap = {
        "aks": ResourceSnapshot(
            kind="aks",
            data={
                "clusters": [
                    {"cluster": "c1", "power_state": "Running", "pressure": _pressure(99, 60)}
                ]
            },
        )
    }
    assert _by_id(evaluate_availability(snap))["aks.node_pressure"].severity == "warning"


def test_headroom_is_ok() -> None:
    snap = {
        "aks": ResourceSnapshot(
            kind="aks",
            data={
                "clusters": [
                    {"cluster": "c1", "power_state": "Running", "pressure": _pressure(40, 50)}
                ]
            },
        )
    }
    assert _by_id(evaluate_availability(snap))["aks.node_pressure"].severity == "ok"


def test_stopped_cluster_is_warning_in_availability() -> None:
    snap = {
        "aks": ResourceSnapshot(
            kind="aks",
            data={"clusters": [{"cluster": "c1", "power_state": "Stopped", "pressure": {}}]},
        )
    }
    finding = _by_id(evaluate_availability(snap))["aks.node_pressure"]
    assert finding.severity == "warning"


def test_unreachable_pressure_is_indeterminate() -> None:
    snap = {
        "aks": ResourceSnapshot(
            kind="aks",
            data={
                "clusters": [
                    {"cluster": "c1", "power_state": "Running", "pressure": {"reachable": False}}
                ]
            },
        )
    }
    finding = _by_id(evaluate_availability(snap))["aks.node_pressure"]
    assert finding.severity == "indeterminate"


def test_aks_denied_is_indeterminate_never_critical() -> None:
    snap = {
        "aks": ResourceSnapshot(kind="aks", available=False, reason="forbidden", access="denied")
    }
    findings = _by_id(evaluate_availability(snap))
    assert findings["aks.node_pressure"].severity == "indeterminate"
    assert all(f.severity != "critical" for f in findings.values())


def test_sidecar_down_is_critical() -> None:
    snap = {
        "container_app": ResourceSnapshot(
            kind="container_app",
            data={
                "sidecars": {"api": {"health": "ok", "cpu_pct": 10}, "worker": {"health": "down"}}
            },
        )
    }
    assert _by_id(evaluate_availability(snap))["container_app.sidecars"].severity == "critical"


def test_sidecar_degraded_is_warning() -> None:
    snap = {
        "container_app": ResourceSnapshot(
            kind="container_app",
            data={"sidecars": {"api": {"health": "degraded", "cpu_pct": 10}}},
        )
    }
    assert _by_id(evaluate_availability(snap))["container_app.sidecars"].severity == "warning"


def test_sidecar_high_cpu_is_warning() -> None:
    snap = {
        "container_app": ResourceSnapshot(
            kind="container_app",
            data={"sidecars": {"api": {"health": "ok", "cpu_pct": 95}}},
        )
    }
    assert _by_id(evaluate_availability(snap))["container_app.sidecars"].severity == "warning"


def test_sidecars_healthy_is_ok() -> None:
    snap = {
        "container_app": ResourceSnapshot(
            kind="container_app",
            data={
                "sidecars": {
                    "api": {"health": "ok", "cpu_pct": 10},
                    "redis": {"health": "ok", "cpu_pct": 5},
                }
            },
        )
    }
    assert _by_id(evaluate_availability(snap))["container_app.sidecars"].severity == "ok"


def test_api_error_rate_critical_and_warning() -> None:
    crit = {
        "api": ResourceSnapshot(
            kind="api", data={"total": 100, "errors": 25, "error_rate": 0.25, "p95_ms": 100}
        )
    }
    warn = {
        "api": ResourceSnapshot(
            kind="api", data={"total": 100, "errors": 8, "error_rate": 0.08, "p95_ms": 100}
        )
    }
    assert _by_id(evaluate_availability(crit))["api.error_rate"].severity == "critical"
    assert _by_id(evaluate_availability(warn))["api.error_rate"].severity == "warning"


def test_api_high_p95_is_warning() -> None:
    snap = {
        "api": ResourceSnapshot(
            kind="api", data={"total": 100, "errors": 0, "error_rate": 0.0, "p95_ms": 2500}
        )
    }
    assert _by_id(evaluate_availability(snap))["api.latency"].severity == "warning"


def test_api_no_traffic_is_info() -> None:
    snap = {"api": ResourceSnapshot(kind="api", data={"total": 0, "degraded": True})}
    assert _by_id(evaluate_availability(snap))["api.latency"].severity == "info"


def test_aks_perf_config_specs() -> None:
    entry = {
        "cluster": "c1",
        "power_state": "Running",
        "pressure": _pressure(40, 50),
        "config": {
            "name": "c1",
            "network_plugin": "azure",
            "load_balancer_sku": "standard",
            "addon_monitoring": True,
        },
    }
    findings = _by_id(
        evaluate_availability({"aks": ResourceSnapshot(kind="aks", data={"clusters": [entry]})})
    )
    assert findings["aks.network_plugin"].severity == "ok"
    assert findings["aks.load_balancer_sku"].severity == "ok"
    assert findings["aks.monitoring_addon"].severity == "ok"


def test_aks_perf_config_warns_on_basic_lb_and_no_monitoring() -> None:
    entry = {
        "cluster": "c1",
        "power_state": "Running",
        "pressure": _pressure(40, 50),
        "config": {
            "name": "c1",
            "network_plugin": "kubenet",
            "load_balancer_sku": "basic",
            "addon_monitoring": False,
        },
    }
    findings = _by_id(
        evaluate_availability({"aks": ResourceSnapshot(kind="aks", data={"clusters": [entry]})})
    )
    assert findings["aks.network_plugin"].severity == "info"
    assert findings["aks.load_balancer_sku"].severity == "warning"
    assert findings["aks.monitoring_addon"].severity == "warning"

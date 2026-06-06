"""Golden tests for the operational-health rule catalog.

Responsibility: Pin the live-incident checks (events / pods / nodes / jobs /
    deployments / BLAST jobstate / API routes) against curated runtime snapshots,
    including the aggregation, severity, and traceability (`observed`) contract.
Edit boundaries: Pure-function assertions only — no TestClient, no Azure/K8s.
Key entry points: the `test_*` functions below.
Risky contracts: A failure/permission-denied snapshot MUST yield `indeterminate`,
    never `critical`. Each finding must carry the offending object(s) in
    `observed` so an operator can trace the problem.
Validation: `uv run pytest -q api/tests/test_diagnostics_operational_rules.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from api.services.diagnostics.models import ResourceSnapshot
from api.services.diagnostics.rules import evaluate_operational


def _by_id(findings):
    return {f.id: f for f in findings}


def _aks(clusters):
    return ResourceSnapshot(kind="aks", data={"clusters": clusters})


def _iso_minutes_ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


# ---------------------------------------------------------------------- events


def test_failed_scheduling_event_is_warning_and_traceable() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "events": [
            {
                "type": "Warning",
                "reason": "FailedScheduling",
                "count": 4,
                "involved_kind": "Pod",
                "involved_name": "blast-abc",
                "message": "0/3 nodes are available: insufficient cpu",
            }
        ],
    }
    findings = _by_id(evaluate_operational({"aks": _aks([cluster])}))
    f = findings["events.failed_scheduling"]
    assert f.severity == "warning"
    assert f.observed["count"] == "4"
    assert "Pod/blast-abc" in f.observed["objects"]


def test_oom_event_is_critical() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "events": [{"type": "Warning", "reason": "OOMKilling", "count": 1, "message": "oom"}],
    }
    findings = _by_id(evaluate_operational({"aks": _aks([cluster])}))
    assert findings["events.oom"].severity == "critical"


def test_image_pull_event_classified() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "events": [{"type": "Warning", "reason": "ImagePullBackOff", "count": 7}],
    }
    findings = _by_id(evaluate_operational({"aks": _aks([cluster])}))
    assert findings["events.image_pull"].severity == "warning"
    assert findings["events.image_pull"].observed["count"] == "7"


def test_unknown_warning_reason_is_other_info() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "events": [{"type": "Warning", "reason": "SomethingNovel", "count": 2}],
    }
    findings = _by_id(evaluate_operational({"aks": _aks([cluster])}))
    assert findings["events.other"].severity == "info"
    assert "SomethingNovel" in findings["events.other"].observed["reasons"]


def test_normal_events_produce_no_event_findings() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "events": [{"type": "Normal", "reason": "Scheduled", "count": 1}],
    }
    findings = _by_id(evaluate_operational({"aks": _aks([cluster])}))
    assert not any(k.startswith("events.") for k in findings)


# ------------------------------------------------------------------------ pods


def test_crash_loop_restarts_is_critical() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "pods": [
            {
                "namespace": "blast",
                "name": "p1",
                "status": "Running",
                "ready": "1/1",
                "restarts": 25,
            }
        ],
    }
    f = _by_id(evaluate_operational({"aks": _aks([cluster])}))["pods.restarts"]
    assert f.severity == "critical"
    assert "blast/p1" in f.observed["pods"]
    assert f.observed["max_restarts"] == "25"


def test_moderate_restarts_is_warning() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "pods": [
            {"namespace": "blast", "name": "p1", "status": "Running", "ready": "1/1", "restarts": 6}
        ],
    }
    assert (
        _by_id(evaluate_operational({"aks": _aks([cluster])}))["pods.restarts"].severity
        == "warning"
    )


def test_stuck_pending_pod_is_warning() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "pods": [
            {
                "namespace": "blast",
                "name": "p1",
                "status": "Pending",
                "ready": "0/1",
                "restarts": 0,
                "age": _iso_minutes_ago(30),
            }
        ],
    }
    assert (
        _by_id(evaluate_operational({"aks": _aks([cluster])}))["pods.pending"].severity == "warning"
    )


def test_recently_pending_pod_is_not_flagged() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "pods": [
            {
                "namespace": "blast",
                "name": "p1",
                "status": "Pending",
                "ready": "0/1",
                "restarts": 0,
                "age": _iso_minutes_ago(1),
            }
        ],
    }
    assert "pods.pending" not in _by_id(evaluate_operational({"aks": _aks([cluster])}))


def test_failed_pod_and_not_ready_pod() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "pods": [
            {"namespace": "blast", "name": "f1", "status": "Failed", "ready": "0/1", "restarts": 0},
            {
                "namespace": "blast",
                "name": "nr",
                "status": "Running",
                "ready": "1/2",
                "restarts": 0,
            },
        ],
    }
    findings = _by_id(evaluate_operational({"aks": _aks([cluster])}))
    assert findings["pods.failed"].severity == "warning"
    assert findings["pods.not_ready"].severity == "warning"


# ----------------------------------------------------------------------- nodes


def test_not_ready_node_is_critical() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "nodes": [{"name": "aks-n1", "status": "NotReady"}],
    }
    f = _by_id(evaluate_operational({"aks": _aks([cluster])}))["nodes.not_ready"]
    assert f.severity == "critical"
    assert "aks-n1" in f.observed["nodes"]


def test_node_pressure_conditions() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "nodes": [
            {"name": "aks-n1", "status": "Ready", "disk_pressure": True},
            {"name": "aks-n2", "status": "Ready", "memory_pressure": True},
            {"name": "aks-n3", "status": "Ready", "pid_pressure": True},
            {"name": "aks-n4", "status": "Ready", "unschedulable": True},
        ],
    }
    findings = _by_id(evaluate_operational({"aks": _aks([cluster])}))
    assert findings["nodes.disk_pressure"].severity == "warning"
    assert findings["nodes.memory_pressure"].severity == "warning"
    assert findings["nodes.pid_pressure"].severity == "warning"
    assert findings["nodes.cordoned"].severity == "info"


# ------------------------------------------------------------------- workloads


def test_failed_job_and_under_replicated_deployment() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "jobs": [
            {"namespace": "blast", "name": "j1", "status": "Failed", "failed": 3},
            {"namespace": "blast", "name": "j2", "status": "Running", "failed": 1},
        ],
        "deployments": [
            {"namespace": "kube-system", "name": "d-down", "ready": "0/2", "available": 0},
            {"namespace": "kube-system", "name": "d-deg", "ready": "1/2", "available": 1},
        ],
    }
    findings = _by_id(evaluate_operational({"aks": _aks([cluster])}))
    assert findings["jobs.failed"].severity == "warning"
    assert findings["jobs.retrying"].severity == "info"
    assert findings["deployments.down"].severity == "critical"
    assert findings["deployments.degraded"].severity == "warning"


# ----------------------------------------------------------------- BLAST jobs


def test_failed_blast_jobs_aggregated() -> None:
    snap = {
        "queue": ResourceSnapshot(
            kind="queue",
            data={
                "jobs": [
                    {
                        "job_id": "j1",
                        "status": "failed",
                        "error_code": "submit_timeout",
                        "updated_at": _iso_minutes_ago(5),
                    },
                    {
                        "job_id": "j2",
                        "status": "failed",
                        "error_code": "submit_timeout",
                        "updated_at": _iso_minutes_ago(8),
                    },
                    {"job_id": "j3", "status": "completed", "updated_at": _iso_minutes_ago(9)},
                ]
            },
        )
    }
    f = _by_id(evaluate_operational(snap))["blast.failed_jobs"]
    assert f.severity == "warning"
    assert f.observed["count"] == "2"
    assert "submit_timeout" in f.detail


def test_stale_running_blast_job() -> None:
    snap = {
        "queue": ResourceSnapshot(
            kind="queue",
            data={
                "jobs": [
                    {"job_id": "j1", "status": "running", "updated_at": _iso_minutes_ago(60 * 24)},
                    {"job_id": "j2", "status": "running", "updated_at": _iso_minutes_ago(5)},
                ]
            },
        )
    }
    findings = _by_id(evaluate_operational(snap))
    assert findings["blast.stale_jobs"].severity == "warning"
    assert "j1" in findings["blast.stale_jobs"].observed["jobs"]


# ------------------------------------------------------------------ API routes


def test_api_route_error_hotspot() -> None:
    snap = {
        "api": ResourceSnapshot(
            kind="api",
            data={
                "by_path": [
                    {"path": "/api/blast/submit", "count": 20, "errors": 5, "p95_ms": 100},
                    {"path": "/api/health", "count": 100, "errors": 0, "p95_ms": 10},
                ]
            },
        )
    }
    f = _by_id(evaluate_operational(snap))["api.route_errors"]
    assert f.severity == "warning"
    assert "/api/blast/submit" in f.observed["routes"]


def test_api_slow_route() -> None:
    snap = {
        "api": ResourceSnapshot(
            kind="api",
            data={"by_path": [{"path": "/api/arm/x", "count": 30, "errors": 0, "p95_ms": 4200}]},
        )
    }
    assert _by_id(evaluate_operational(snap))["api.route_latency"].severity == "info"


# -------------------------------------------------------------- honesty guards


def test_runtime_denied_is_indeterminate_never_critical() -> None:
    snap = {
        "aks": ResourceSnapshot(kind="aks", available=False, reason="forbidden", access="denied")
    }
    findings = _by_id(evaluate_operational(snap))
    assert findings["aks.runtime"].severity == "indeterminate"
    assert all(f.severity != "critical" for f in findings.values())


def test_jobstate_unavailable_is_indeterminate() -> None:
    snap = {
        "queue": ResourceSnapshot(kind="queue", available=False, reason="error", access="error")
    }
    assert _by_id(evaluate_operational(snap))["blast.jobstate"].severity == "indeterminate"


def test_stopped_cluster_runtime_is_info() -> None:
    cluster = {"cluster": "c1", "power_state": "Stopped"}
    assert _by_id(evaluate_operational({"aks": _aks([cluster])}))["aks.runtime"].severity == "info"


def test_per_cluster_fetch_error_is_indeterminate() -> None:
    cluster = {"cluster": "c1", "power_state": "Running", "fetch_error": "RuntimeError"}
    f = _by_id(evaluate_operational({"aks": _aks([cluster])}))["aks.runtime"]
    assert f.severity == "indeterminate"
    assert "RuntimeError" in f.observed["error"]


def test_healthy_cluster_emits_no_problem_findings() -> None:
    cluster = {
        "cluster": "c1",
        "power_state": "Running",
        "events": [],
        "pods": [
            {"namespace": "blast", "name": "p1", "status": "Running", "ready": "1/1", "restarts": 0}
        ],
        "nodes": [{"name": "aks-n1", "status": "Ready"}],
        "jobs": [{"namespace": "blast", "name": "j1", "status": "Complete", "failed": 0}],
        "deployments": [{"namespace": "kube-system", "name": "d1", "ready": "2/2", "available": 2}],
    }
    findings = evaluate_operational({"aks": _aks([cluster])})
    assert all(f.severity in {"ok", "info"} for f in findings), [
        (f.id, f.severity) for f in findings if f.severity not in {"ok", "info"}
    ]

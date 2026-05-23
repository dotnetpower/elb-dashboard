"""Tests for Kubernetes BLAST Status behavior.

Responsibility: Tests for Kubernetes BLAST Status behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_Response`, `_Session`, `_job`,
`test_k8s_check_blast_status_ignores_other_jobs_when_scoped`,
`test_k8s_check_blast_status_uses_scoped_job_label_without_pod`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_k8s_blast_status.py`.
"""

from __future__ import annotations

from typing import Any

from api.services.k8s import monitoring as k8s_monitoring


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _Session:
    def __init__(self, pods: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> None:
        self.pods = pods
        self.jobs = jobs
        self.closed = False

    def get(self, url: str, **_: Any) -> _Response:
        if url.endswith("/api/v1/namespaces/default"):
            return _Response(200, {})
        if url.endswith("/api/v1/namespaces/default/pods"):
            return _Response(200, {"items": self.pods})
        if url.endswith("/apis/batch/v1/namespaces/default/jobs"):
            return _Response(200, {"items": self.jobs})
        raise AssertionError(url)

    def close(self) -> None:
        self.closed = True


def _job(
    name: str,
    job_id: str,
    *,
    succeeded: int = 0,
    active: int = 0,
    start_time: str | None = None,
    completion_time: str | None = None,
) -> dict[str, Any]:
    status: dict[str, Any] = {"succeeded": succeeded, "active": active}
    if start_time:
        status["startTime"] = start_time
    if completion_time:
        status["completionTime"] = completion_time
    return {
        "metadata": {"name": name, "labels": {"app": "blast", "elb-job-id": job_id}},
        "status": status,
    }


def _pod(
    name: str,
    job_name: str,
    job_id: str,
    *,
    containers: list[dict[str, Any]],
    start_time: str | None = None,
) -> dict[str, Any]:
    status: dict[str, Any] = {"containerStatuses": containers}
    if start_time:
        status["startTime"] = start_time
    return {
        "metadata": {
            "name": name,
            "ownerReferences": [{"kind": "Job", "name": job_name}],
        },
        "spec": {
            "containers": [
                {"env": [{"name": "BLAST_ELB_JOB_ID", "value": job_id}]},
            ]
        },
        "status": status,
    }


def _terminated_container(name: str, started_at: str, finished_at: str) -> dict[str, Any]:
    return {
        "name": name,
        "state": {
            "terminated": {
                "startedAt": started_at,
                "finishedAt": finished_at,
            }
        },
    }


def test_k8s_check_blast_status_ignores_other_jobs_when_scoped(monkeypatch) -> None:
    session = _Session(pods=[], jobs=[_job("blast-other", "other-job", succeeded=1)])
    monkeypatch.setattr(k8s_monitoring, "_get_k8s_session", lambda *_args: (session, "https://k8s"))

    status = k8s_monitoring.k8s_check_blast_status(
        None, "sub", "rg", "cluster", "default", job_id="target-job"
    )

    assert status["status"] == "creating"
    assert status["jobs"] == 0
    assert session.closed is True


def test_k8s_check_blast_status_uses_scoped_job_label_without_pod(monkeypatch) -> None:
    session = _Session(
        pods=[],
        jobs=[
            _job("blast-other", "other-job", succeeded=1),
            _job("blast-target", "target-job", succeeded=1),
        ],
    )
    monkeypatch.setattr(k8s_monitoring, "_get_k8s_session", lambda *_args: (session, "https://k8s"))

    status = k8s_monitoring.k8s_check_blast_status(
        None, "sub", "rg", "cluster", "default", job_id="target-job"
    )

    assert status["status"] == "completed"
    assert status["job_id"] == "target-job"
    assert status["jobs"] == 1
    assert status["succeeded"] == 1


def test_k8s_check_blast_status_returns_runtime_timestamps(monkeypatch) -> None:
    session = _Session(
        pods=[],
        jobs=[
            _job(
                "blast-target",
                "target-job",
                succeeded=1,
                start_time="2026-05-21T03:04:30Z",
                completion_time="2026-05-21T03:06:35Z",
            )
        ],
    )
    monkeypatch.setattr(k8s_monitoring, "_get_k8s_session", lambda *_args: (session, "https://k8s"))

    status = k8s_monitoring.k8s_check_blast_status(
        None, "sub", "rg", "cluster", "default", job_id="target-job"
    )

    assert status["status"] == "completed"
    assert status["started_at"] == "2026-05-21T03:04:30Z"
    assert status["completed_at"] == "2026-05-21T03:06:35Z"


def test_k8s_check_blast_status_orders_offset_runtime_timestamps(monkeypatch) -> None:
    session = _Session(
        pods=[],
        jobs=[
            _job(
                "blast-target-1",
                "target-job",
                succeeded=1,
                start_time="2026-05-21T12:04:30+09:00",
                completion_time="2026-05-21T12:05:35+09:00",
            ),
            _job(
                "blast-target-2",
                "target-job",
                succeeded=1,
                start_time="2026-05-21T03:04:45Z",
                completion_time="2026-05-21T03:06:35Z",
            ),
        ],
    )
    monkeypatch.setattr(k8s_monitoring, "_get_k8s_session", lambda *_args: (session, "https://k8s"))

    status = k8s_monitoring.k8s_check_blast_status(
        None, "sub", "rg", "cluster", "default", job_id="target-job"
    )

    assert status["started_at"] == "2026-05-21T12:04:30+09:00"
    assert status["completed_at"] == "2026-05-21T03:06:35Z"


def test_k8s_check_blast_status_ignores_unparseable_runtime_timestamps(monkeypatch) -> None:
    session = _Session(
        pods=[],
        jobs=[
            _job(
                "blast-target-1",
                "target-job",
                succeeded=1,
                start_time="not-a-time",
                completion_time="also-not-a-time",
            ),
            _job(
                "blast-target-2",
                "target-job",
                succeeded=1,
                start_time="2026-05-21T03:04:45Z",
                completion_time="2026-05-21T03:06:35Z",
            ),
        ],
    )
    monkeypatch.setattr(k8s_monitoring, "_get_k8s_session", lambda *_args: (session, "https://k8s"))

    status = k8s_monitoring.k8s_check_blast_status(
        None, "sub", "rg", "cluster", "default", job_id="target-job"
    )

    assert status["status"] == "completed"
    assert status["started_at"] == "2026-05-21T03:04:45Z"
    assert status["completed_at"] == "2026-05-21T03:06:35Z"


def test_k8s_check_blast_status_reports_container_spans(monkeypatch) -> None:
    session = _Session(
        pods=[
            _pod(
                "blast-target-pod-1",
                "blast-target-1",
                "target-job",
                start_time="2026-05-21T03:04:31Z",
                containers=[
                    _terminated_container(
                        "blast",
                        "2026-05-21T03:04:45Z",
                        "2026-05-21T03:04:50Z",
                    ),
                    _terminated_container(
                        "results-export",
                        "2026-05-21T03:04:46Z",
                        "2026-05-21T03:05:00Z",
                    ),
                ],
            ),
            _pod(
                "blast-target-pod-2",
                "blast-target-2",
                "target-job",
                containers=[
                    _terminated_container(
                        "blast",
                        "2026-05-21T03:04:47Z",
                        "2026-05-21T03:04:53Z",
                    ),
                    _terminated_container(
                        "results-export",
                        "2026-05-21T03:04:48Z",
                        "2026-05-21T03:05:05Z",
                    ),
                ],
            ),
        ],
        jobs=[
            _job(
                "blast-target-1",
                "target-job",
                succeeded=1,
                start_time="2026-05-21T03:04:30Z",
                completion_time="2026-05-21T03:06:35Z",
            ),
            _job(
                "blast-target-2",
                "target-job",
                succeeded=1,
                start_time="2026-05-21T03:04:32Z",
                completion_time="2026-05-21T03:06:36Z",
            ),
        ],
    )
    monkeypatch.setattr(k8s_monitoring, "_get_k8s_session", lambda *_args: (session, "https://k8s"))

    status = k8s_monitoring.k8s_check_blast_status(
        None, "sub", "rg", "cluster", "default", job_id="target-job"
    )

    assert status["status"] == "completed"
    assert status["started_at"] == "2026-05-21T03:04:30Z"
    assert status["completed_at"] == "2026-05-21T03:06:36Z"
    assert status["blast_container_count"] == 2
    assert status["blast_container_started_at"] == "2026-05-21T03:04:45Z"
    assert status["blast_container_completed_at"] == "2026-05-21T03:04:53Z"
    assert status["blast_container_duration_ms"] == 8000
    assert status["results_export_container_count"] == 2
    assert status["results_export_container_started_at"] == "2026-05-21T03:04:46Z"
    assert status["results_export_container_completed_at"] == "2026-05-21T03:05:05Z"
    assert status["results_export_container_duration_ms"] == 19000

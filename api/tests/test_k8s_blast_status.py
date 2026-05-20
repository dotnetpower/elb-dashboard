from __future__ import annotations

from typing import Any

from api.services import k8s_monitoring


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


def _job(name: str, job_id: str, *, succeeded: int = 0, active: int = 0) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "labels": {"app": "blast", "elb-job-id": job_id}},
        "status": {"succeeded": succeeded, "active": active},
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

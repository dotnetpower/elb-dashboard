"""Tests for bounded ElasticBLAST Kubernetes runtime garbage collection.

Responsibility: Verify terminal/age filters, deletion bounds, and fail-closed handling.
Edit boundaries: Fake HTTP sessions only; never call a real cluster.
Key entry points: `test_runtime_gc_deletes_only_old_terminal_objects`.
Risky contracts: Active, recent, and timestamp-invalid objects must never be deleted.
Validation: `uv run pytest -q api/tests/test_k8s_runtime_gc.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _Session:
    def __init__(self, jobs: list[dict[str, Any]], configmaps: list[dict[str, Any]]) -> None:
        self.jobs = jobs
        self.configmaps = configmaps
        self.deletes: list[str] = []
        self.delete_statuses: list[int] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> _Response:
        selector = str((kwargs.get("params") or {}).get("labelSelector") or "")
        if url.endswith("/jobs"):
            app = selector.split("=", 1)[-1]
            items = [
                job
                for job in self.jobs
                if job.get("metadata", {}).get("labels", {}).get("app") == app
            ]
            return _Response(200, {"items": items, "metadata": {}})
        if url.endswith("/configmaps"):
            return _Response(200, {"items": self.configmaps, "metadata": {}})
        raise AssertionError(url)

    def delete(self, url: str, **_kwargs: Any) -> _Response:
        self.deletes.append(url)
        return _Response(self.delete_statuses.pop(0) if self.delete_statuses else 200)

    def close(self) -> None:
        self.closed = True


def _job(
    name: str,
    *,
    app: str = "blast",
    created: str = "2026-01-01T00:00:00Z",
    active: int = 0,
    condition: str | None = "Complete",
) -> dict[str, Any]:
    conditions = [] if condition is None else [{"type": condition, "status": "True"}]
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": created,
            "labels": {"app": app},
        },
        "status": {"active": active, "conditions": conditions},
    }


def _configmap(
    name: str,
    *,
    status: str,
    created: str,
    updated: str | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": created,
            "labels": {"elb-job": "true", "status": status},
        },
        "data": {
            "job": json.dumps(
                {
                    "job_id": name,
                    "status": status,
                    "updated_at": updated or created,
                }
            )
        },
    }


def test_runtime_gc_deletes_only_old_terminal_objects(monkeypatch) -> None:
    from api.services.k8s import runtime_gc

    session = _Session(
        jobs=[
            _job("old-complete"),
            _job("old-failed", condition="Failed"),
            _job("active", active=1, condition=None),
            _job("unknown", condition=None),
            _job("recent", created="2026-08-23T11:30:00Z"),
        ],
        configmaps=[
            _configmap("old-cm", status="completed", created="2026-07-01T00:00:00Z"),
            _configmap("active-cm", status="running", created="2026-07-01T00:00:00Z"),
            _configmap("recent-cm", status="failed", created="2026-08-20T00:00:00Z"),
            _configmap("invalid-cm", status="completed", created="not-a-time"),
            _configmap(
                "old-created-recently-completed",
                status="completed",
                created="2026-01-01T00:00:00Z",
                updated="2026-08-23T11:55:00Z",
            ),
            {
                "metadata": {
                    "name": "malformed-payload",
                    "creationTimestamp": "2026-01-01T00:00:00Z",
                    "labels": {"elb-job": "true", "status": "completed"},
                },
                "data": {"job": "not-json"},
            },
        ],
    )
    monkeypatch.setattr(
        "api.services.k8s.credentials._get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )

    result = runtime_gc.collect_runtime_garbage(
        object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="aks",
        now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )

    assert sorted(url.rsplit("/", 1)[-1] for url in session.deletes) == [
        "old-cm",
        "old-complete",
        "old-failed",
    ]
    assert result["jobs_deleted"] == 2
    assert result["configmaps_deleted"] == 1
    assert result["errors"] == []
    assert session.closed is True


def test_runtime_gc_obeys_global_delete_bound(monkeypatch) -> None:
    from api.services.k8s import runtime_gc

    session = _Session(
        jobs=[_job(f"job-{index}") for index in range(10)],
        configmaps=[
            _configmap(f"cm-{index}", status="completed", created="2026-01-01T00:00:00Z")
            for index in range(10)
        ],
    )
    monkeypatch.setattr(
        "api.services.k8s.credentials._get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )

    result = runtime_gc.collect_runtime_garbage(
        object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="aks",
        max_deletes=6,
        now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )

    assert len(session.deletes) <= 6
    assert result["jobs_deleted"] <= 3
    assert result["configmaps_deleted"] <= 3


def test_runtime_gc_retries_one_transient_delete(monkeypatch) -> None:
    from api.services.k8s import runtime_gc

    session = _Session(jobs=[_job("retry-me")], configmaps=[])
    session.delete_statuses = [503, 200]
    monkeypatch.setattr(
        "api.services.k8s.credentials._get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )
    monkeypatch.setattr(runtime_gc.time, "sleep", lambda _seconds: None)

    result = runtime_gc.collect_runtime_garbage(
        object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="aks",
        now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )

    assert result["jobs_deleted"] == 1
    assert result["errors"] == []
    assert len(session.deletes) == 2


def test_runtime_gc_preserves_malformed_status_counts(monkeypatch) -> None:
    from api.services.k8s import runtime_gc

    malformed = _job("malformed")
    malformed["status"]["active"] = "not-a-count"
    session = _Session(jobs=[malformed], configmaps=[])
    monkeypatch.setattr(
        "api.services.k8s.credentials._get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )

    result = runtime_gc.collect_runtime_garbage(
        object(),
        subscription_id="sub",
        resource_group="rg",
        cluster_name="aks",
        now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )

    assert result["jobs_deleted"] == 0
    assert session.deletes == []

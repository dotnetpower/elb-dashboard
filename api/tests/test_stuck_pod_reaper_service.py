"""Tests for the single-cluster stuck-pod reaper orchestration.

Responsibility: Verify reap_stuck_blast_pods_in_cluster (a) never deletes in
dry-run, (b) deletes the OWNER JOB of a wedged pod when armed, (c) never selects
a Running pod, (d) degrades (no raise) on K8s errors.
Edit boundaries: Uses a fake K8s session; no live network.
Key entry points: the test functions below.
Risky contracts: A Running pod appearing in `reaped_jobs` is a safety regression
and must fail loudly.
Validation: `uv run pytest -q api/tests/test_stuck_pod_reaper_service.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.k8s import monitoring, stuck_pod_reaper_service
from api.services.k8s.stuck_pod_reaper_service import reap_stuck_blast_pods_in_cluster


def _pod(name: str, *, status_reason: str, age_iso: str, job: str) -> dict[str, Any]:
    """Minimal pod dict whose compute_pod_display_status -> status_reason."""
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": age_iso,
            "labels": {"app": "blast", "job-name": job},
        },
        "status": {
            "phase": "Pending" if status_reason == "Pending" else "Running",
            "containerStatuses": [
                {"state": {"waiting": {"reason": status_reason}}}
            ]
            if status_reason not in ("Pending", "Running")
            else [],
        },
    }


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.closed = False

    def get(self, url: str, timeout: int = 10) -> _FakeResp:
        return _FakeResp(self._payload)

    def close(self) -> None:
        self.closed = True


def _arm_session(monkeypatch: pytest.MonkeyPatch, pods: list[dict[str, Any]]) -> None:
    session = _FakeSession({"items": pods})
    monkeypatch.setattr(
        monitoring,
        "_get_k8s_session",
        lambda *a, **k: (session, "https://k8s.example"),
    )


_OLD = "2000-01-01T00:00:00Z"  # decades old -> always past threshold


def test_dry_run_reports_but_never_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm_session(
        monkeypatch,
        [
            _pod("p1", status_reason="CrashLoopBackOff", age_iso=_OLD, job="blast-batch-0"),
            _pod("p2", status_reason="Running", age_iso=_OLD, job="blast-batch-1"),
        ],
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        stuck_pod_reaper_service,
        "k8s_job_delete",
        lambda *a, **k: deleted.append(a[-1]),
    )
    summary = reap_stuck_blast_pods_in_cluster(
        object(), "sub", "rg", "elb-cluster", dry_run=True
    )
    assert summary["scanned"] == 2
    assert summary["reaped_jobs"] == ["blast-batch-0"]  # only the CrashLoop one
    assert deleted == []  # dry-run never deletes


def test_armed_deletes_owner_job_of_wedged_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm_session(
        monkeypatch,
        [
            _pod("p1", status_reason="ImagePullBackOff", age_iso=_OLD, job="blast-batch-7"),
            _pod("p2", status_reason="Running", age_iso=_OLD, job="blast-batch-8"),
        ],
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        stuck_pod_reaper_service,
        "k8s_job_delete",
        lambda *a, **k: deleted.append(a[-1]),
    )
    summary = reap_stuck_blast_pods_in_cluster(
        object(), "sub", "rg", "elb-cluster", dry_run=False
    )
    assert deleted == ["blast-batch-7"]
    assert summary["reaped_jobs"] == ["blast-batch-7"]
    assert "blast-batch-8" not in summary["reaped_jobs"]  # Running never reaped


def test_session_failure_degrades_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("kubeconfig unavailable")

    monkeypatch.setattr(monitoring, "_get_k8s_session", _boom)
    summary = reap_stuck_blast_pods_in_cluster(object(), "sub", "rg", "elb-cluster")
    assert summary["errors"] == 1
    assert summary["reaped_jobs"] == []
    assert summary["scanned"] == 0


def test_young_wedged_pod_is_not_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    fresh = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    _arm_session(
        monkeypatch,
        [_pod("p1", status_reason="CrashLoopBackOff", age_iso=fresh, job="blast-batch-0")],
    )
    summary = reap_stuck_blast_pods_in_cluster(
        object(), "sub", "rg", "elb-cluster", dry_run=True
    )
    assert summary["reaped_jobs"] == []  # 60s < 900s threshold

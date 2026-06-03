"""Unit tests for `k8s_get_deployments` / `k8s_get_jobs` parsing and scoping.

Responsibility: Verify the cluster Deployments and Jobs listings hit the
correct apps/v1 and batch/v1 endpoints, scope the URL by namespace when
requested, and parse the replica/completion/status fields the Workloads
tabs render — all without contacting a real cluster.
Edit boundaries: Stay focused on `k8s_get_deployments` / `k8s_get_jobs`.
Session pooling/auth is covered by `test_k8s_session_pool.py`; pod parsing
by `test_k8s_get_pods.py` — do not duplicate.
Key entry points: `test_k8s_get_deployments_parses_replicas`,
`test_k8s_get_deployments_namespace_scopes_url`,
`test_k8s_get_jobs_derives_status`, `test_k8s_get_jobs_namespace_scopes_url`.
Risky contracts: Must not require network access or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_k8s_get_deployments_jobs.py`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from api.services.k8s import monitoring as m


def _patch_session(items: list[dict[str, Any]]):
    response = MagicMock()
    response.json.return_value = {"items": items}
    response.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.return_value = response
    session.close = MagicMock()
    return session, patch(
        "api.services.k8s.monitoring._get_k8s_session",
        return_value=(session, "https://aks"),
    )


def test_k8s_get_deployments_parses_replicas() -> None:
    items = [
        {
            "metadata": {
                "namespace": "default",
                "name": "blast-frontend",
                "creationTimestamp": "2026-06-01T00:00:00Z",
            },
            "spec": {"replicas": 3},
            "status": {
                "readyReplicas": 2,
                "updatedReplicas": 3,
                "availableReplicas": 2,
            },
        },
        {
            "metadata": {"namespace": "kube-system", "name": "coredns"},
            "spec": {},
            "status": {},
        },
    ]
    session, patcher = _patch_session(items)
    with patcher:
        out = m.k8s_get_deployments(MagicMock(), "sub", "rg", "aks")
    args, _kwargs = session.get.call_args
    assert args[0] == "https://aks/apis/apps/v1/deployments"
    assert len(out) == 2
    first = out[0]
    assert first["namespace"] == "default"
    assert first["name"] == "blast-frontend"
    assert first["ready"] == "2/3"
    assert first["up_to_date"] == 3
    assert first["available"] == 2
    assert first["age"] == "2026-06-01T00:00:00Z"
    # Missing status fields default to zero, not KeyError.
    assert out[1]["ready"] == "0/0"
    assert out[1]["up_to_date"] == 0
    assert out[1]["available"] == 0
    session.close.assert_called_once()


def test_k8s_get_deployments_namespace_scopes_url() -> None:
    session, patcher = _patch_session([])
    with patcher:
        m.k8s_get_deployments(MagicMock(), "sub", "rg", "aks", namespace="default")
    args, _kwargs = session.get.call_args
    assert args[0] == "https://aks/apis/apps/v1/namespaces/default/deployments"


def test_k8s_get_jobs_derives_status() -> None:
    items = [
        {
            "metadata": {
                "namespace": "default",
                "name": "blast-complete",
                "creationTimestamp": "2026-06-01T00:00:00Z",
            },
            "spec": {"completions": 4},
            "status": {
                "succeeded": 4,
                "startTime": "2026-06-01T00:00:00Z",
                "completionTime": "2026-06-01T00:05:00Z",
                "conditions": [{"type": "Complete", "status": "True"}],
            },
        },
        {
            "metadata": {"namespace": "default", "name": "blast-failed"},
            "spec": {"completions": 2},
            "status": {
                "failed": 2,
                "conditions": [{"type": "Failed", "status": "True"}],
            },
        },
        {
            "metadata": {"namespace": "default", "name": "blast-running"},
            "spec": {},
            "status": {"active": 1},
        },
        {
            "metadata": {"namespace": "default", "name": "blast-pending"},
            "spec": {},
            "status": {},
        },
    ]
    session, patcher = _patch_session(items)
    with patcher:
        out = m.k8s_get_jobs(MagicMock(), "sub", "rg", "aks")
    args, _kwargs = session.get.call_args
    assert args[0] == "https://aks/apis/batch/v1/jobs"
    assert len(out) == 4

    done = out[0]
    assert done["completions"] == "4/4"
    assert done["status"] == "Complete"
    assert done["succeeded"] == 4
    assert done["start_time"] == "2026-06-01T00:00:00Z"
    assert done["completion_time"] == "2026-06-01T00:05:00Z"

    failed = out[1]
    assert failed["completions"] == "0/2"
    assert failed["status"] == "Failed"
    assert failed["failed"] == 2

    # active > 0 → Running
    assert out[2]["status"] == "Running"
    # spec.completions defaults to 1 when absent
    assert out[2]["completions"] == "0/1"
    # no conditions, no active → Pending
    assert out[3]["status"] == "Pending"
    session.close.assert_called_once()


def test_k8s_get_jobs_namespace_scopes_url() -> None:
    session, patcher = _patch_session([])
    with patcher:
        m.k8s_get_jobs(MagicMock(), "sub", "rg", "aks", namespace="blast")
    args, _kwargs = session.get.call_args
    assert args[0] == "https://aks/apis/batch/v1/namespaces/blast/jobs"

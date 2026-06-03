"""Unit tests for `k8s_get_pods` parsing and namespace scoping.

Responsibility: Verify the cluster pods listing returns every phase (no
`status.phase` field selector), surfaces `pod_ip` / `node_ip`, and scopes the
URL by namespace when requested — all without contacting a real cluster.
Edit boundaries: Stay focused on `k8s_get_pods`. Session pooling/auth is
covered by `test_k8s_session_pool.py`; do not duplicate.
Key entry points: `test_k8s_get_pods_returns_all_phases_with_ips`,
`test_k8s_get_pods_no_phase_field_selector`,
`test_k8s_get_pods_namespace_scopes_url`.
Risky contracts: Must not require network access or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_k8s_get_pods.py`.
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


def test_k8s_get_pods_returns_all_phases_with_ips() -> None:
    items = [
        {
            "metadata": {
                "namespace": "default",
                "name": "blast-search-abc",
                "creationTimestamp": "2026-06-01T00:00:00Z",
            },
            "spec": {
                "nodeName": "aks-blastpool-123-vmss000001",
                "containers": [{"name": "blastn"}],
            },
            "status": {
                "phase": "Succeeded",
                "podIP": "10.244.1.5",
                "hostIP": "10.224.0.4",
                "containerStatuses": [{"ready": False, "restartCount": 2}],
            },
        },
        {
            "metadata": {"namespace": "kube-system", "name": "coredns-xyz"},
            "spec": {
                "nodeName": "aks-systempool-9-vmss000000",
                "containers": [{"name": "coredns"}],
            },
            "status": {
                "phase": "Running",
                "podIP": "10.244.5.31",
                "hostIP": "10.224.0.5",
                "containerStatuses": [{"ready": True, "restartCount": 0}],
            },
        },
    ]
    session, patcher = _patch_session(items)
    with patcher:
        out = m.k8s_get_pods(MagicMock(), "sub", "rg", "aks")
    assert len(out) == 2
    succeeded = out[0]
    assert succeeded["status"] == "Succeeded"
    assert succeeded["ready"] == "0/1"
    assert succeeded["restarts"] == 2
    assert succeeded["node"] == "aks-blastpool-123-vmss000001"
    assert succeeded["pod_ip"] == "10.244.1.5"
    assert succeeded["node_ip"] == "10.224.0.4"
    assert out[1]["status"] == "Running"
    assert out[1]["pod_ip"] == "10.244.5.31"
    session.close.assert_called_once()


def test_k8s_get_pods_no_phase_field_selector() -> None:
    """Azure-portal parity: every phase is returned, so no field selector."""

    session, patcher = _patch_session([])
    with patcher:
        m.k8s_get_pods(MagicMock(), "sub", "rg", "aks")
    args, kwargs = session.get.call_args
    assert args[0] == "https://aks/api/v1/pods"
    # No fieldSelector — Succeeded/Completed pods must be included.
    assert "params" not in kwargs or kwargs.get("params") is None


def test_k8s_get_pods_namespace_scopes_url() -> None:
    session, patcher = _patch_session([])
    with patcher:
        m.k8s_get_pods(MagicMock(), "sub", "rg", "aks", namespace="default")
    args, _kwargs = session.get.call_args
    assert args[0] == "https://aks/api/v1/namespaces/default/pods"

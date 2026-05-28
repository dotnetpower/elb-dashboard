"""Tests for `api.services.k8s.node_pressure`.

Module docstring (natural):
Pin the per-pool CPU/memory request pressure calculation that the
dashboard surfaces as the "systempool is 99% requested" early warning
(missing-toleration scheduling regression, 2026-05-28). The Kubernetes
API responses are faked via a stub session so the test exercises the
parsing + aggregation logic without needing a real cluster.

Responsibility: Unit tests for `k8s_node_request_pressure`. Covers
    happy path (mixed pools, one over threshold), session failure,
    API failure, and the "no nodes" empty cluster case.
Edit boundaries: Pure logic checks. Real Kubernetes integration is
    exercised end-to-end by the existing live-debug tooling.
Key entry points: `test_pressure_flags_warning_when_cpu_above_threshold`,
    `test_pressure_session_failure_returns_reachable_false`.
Risky contracts: Stub session must mirror the real
    `_get_k8s_session` tuple return shape (session, server URL).
Validation: `uv run pytest -q api/tests/test_k8s_node_pressure.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.k8s import node_pressure


class _StubResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _StubSession:
    def __init__(self, nodes: list[dict[str, Any]], pods: list[dict[str, Any]]) -> None:
        self._nodes = nodes
        self._pods = pods

    def get(self, url: str, timeout: int = 10) -> _StubResponse:
        del timeout
        if "/api/v1/nodes" in url:
            return _StubResponse({"items": self._nodes})
        return _StubResponse({"items": self._pods})

    def close(self) -> None:
        return None


def _install_stub(
    monkeypatch: pytest.MonkeyPatch,
    nodes: list[dict[str, Any]],
    pods: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(
        node_pressure,
        "_get_k8s_session",
        lambda *_args, **_kw: (_StubSession(nodes, pods), "https://k8s.fake"),
    )


def test_pressure_flags_warning_when_cpu_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = [
        {
            "metadata": {
                "name": "aks-systempool-1",
                "labels": {"agentpool": "systempool"},
            },
            "status": {"allocatable": {"cpu": "1900m", "memory": "5926144Ki"}},
        },
        {
            "metadata": {
                "name": "aks-blastpool-2",
                "labels": {"agentpool": "blastpool"},
            },
            "status": {"allocatable": {"cpu": "15740m", "memory": "60565256Ki"}},
        },
    ]
    pods = [
        # systempool: total 1800m → ~94% pressure
        {
            "spec": {
                "nodeName": "aks-systempool-1",
                "containers": [
                    {"resources": {"requests": {"cpu": "900m", "memory": "1Gi"}}},
                    {"resources": {"requests": {"cpu": "900m", "memory": "1Gi"}}},
                ],
            },
            "status": {"phase": "Running"},
        },
        # blastpool: total 200m → very low
        {
            "spec": {
                "nodeName": "aks-blastpool-2",
                "containers": [{"resources": {"requests": {"cpu": "200m"}}}],
            },
            "status": {"phase": "Running"},
        },
    ]
    _install_stub(monkeypatch, nodes, pods)
    result = node_pressure.k8s_node_request_pressure(object(), "sub", "rg", "elb-cluster-01")
    assert result["reachable"] is True
    sys_pool = result["pools"]["systempool"]
    blast_pool = result["pools"]["blastpool"]
    assert sys_pool["warning"] is True
    assert sys_pool["cpu_request_pct"] >= 90
    assert sys_pool["max_node"] == "aks-systempool-1"
    assert blast_pool["warning"] is False
    assert blast_pool["cpu_request_pct"] < 5


def test_pressure_session_failure_returns_reachable_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kw: Any) -> tuple[Any, str]:
        raise RuntimeError("AKS cluster not found")

    monkeypatch.setattr(node_pressure, "_get_k8s_session", boom)
    result = node_pressure.k8s_node_request_pressure(object(), "sub", "rg", "elb")
    assert result == {
        "reachable": False,
        "reason": "k8s_session_failed: RuntimeError",
    }


def test_pressure_empty_cluster_returns_no_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stub(monkeypatch, [], [])
    result = node_pressure.k8s_node_request_pressure(object(), "sub", "rg", "elb")
    assert result["reachable"] is True
    assert result["pools"] == {}

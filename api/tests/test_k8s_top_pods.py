"""Unit tests for `k8s_top_pods` parsing and filtering.

Responsibility: Verify pod-level metrics parsing (cpu/memory unit handling, per-container
aggregation, namespace/labelSelector forwarding) without contacting a real cluster.
Edit boundaries: Stay focused on `k8s_top_pods`. Cluster-level pooling/auth is covered by
`test_k8s_session_pool.py`; do not duplicate.
Key entry points: `test_k8s_top_pods_aggregates_containers_and_parses_units`,
`test_k8s_top_pods_namespace_url_and_label_selector`,
`test_k8s_top_pods_cluster_scope_when_namespace_missing`.
Risky contracts: Must not require network access or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_k8s_top_pods.py`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from api.services.k8s import metrics as m


def _patch_session(items: list[dict[str, Any]]):
    response = MagicMock()
    response.json.return_value = {"items": items}
    response.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.return_value = response
    session.close = MagicMock()
    return session, patch.object(m, "_get_k8s_session", new=None) if False else patch(
        "api.services.k8s.monitoring._get_k8s_session",
        return_value=(session, "https://aks"),
    )


def test_k8s_top_pods_aggregates_containers_and_parses_units() -> None:
    items = [
        {
            "metadata": {"namespace": "default", "name": "blast-search-abc"},
            "window": "30s",
            "timestamp": "2026-05-25T00:00:00Z",
            "containers": [
                {"name": "blastn", "usage": {"cpu": "1500m", "memory": "32Gi"}},
                {"name": "sidecar", "usage": {"cpu": "250000000n", "memory": "512Mi"}},
            ],
        },
    ]
    session, patcher = _patch_session(items)
    with patcher:
        out = m.k8s_top_pods(MagicMock(), "sub", "rg", "aks", namespace="default")
    assert len(out) == 1
    pod = out[0]
    assert pod["namespace"] == "default"
    assert pod["name"] == "blast-search-abc"
    # 1500m + 250m (250e6 nanocores = 250m) = 1750m
    assert pod["cpu_m"] == 1750
    # 32Gi = 32 * 1024 * 1024 Ki; +512Mi = 512 * 1024 Ki
    expected_ki = 32 * 1024 * 1024 + 512 * 1024
    assert pod["mem_ki"] == expected_ki
    assert pod["mem_mi"] == expected_ki // 1024
    assert len(pod["containers"]) == 2
    assert pod["containers"][0]["name"] == "blastn"
    assert pod["containers"][0]["cpu_m"] == 1500
    assert pod["containers"][1]["cpu_m"] == 250
    session.close.assert_called_once()


def test_k8s_top_pods_namespace_url_and_label_selector() -> None:
    session, patcher = _patch_session([])
    with patcher:
        m.k8s_top_pods(
            MagicMock(),
            "sub",
            "rg",
            "aks",
            namespace="elb-openapi",
            label_selector="app=elastic-blast",
        )
    args, kwargs = session.get.call_args
    assert args[0] == "https://aks/apis/metrics.k8s.io/v1beta1/namespaces/elb-openapi/pods"
    assert kwargs.get("params") == {"labelSelector": "app=elastic-blast"}


def test_k8s_top_pods_cluster_scope_when_namespace_missing() -> None:
    session, patcher = _patch_session([])
    with patcher:
        m.k8s_top_pods(MagicMock(), "sub", "rg", "aks")
    args, kwargs = session.get.call_args
    assert args[0] == "https://aks/apis/metrics.k8s.io/v1beta1/pods"
    # No params should be forwarded when label_selector is None.
    assert kwargs.get("params") is None


def test_k8s_top_pods_skips_pods_without_containers() -> None:
    items = [
        {"metadata": {"namespace": "ns", "name": "no-containers"}},
        {
            "metadata": {"namespace": "ns", "name": "has-containers"},
            "containers": [{"name": "c", "usage": {"cpu": "10m", "memory": "20Mi"}}],
        },
    ]
    _session, patcher = _patch_session(items)
    with patcher:
        out = m.k8s_top_pods(MagicMock(), "sub", "rg", "aks")
    assert len(out) == 2
    assert out[0]["cpu_m"] == 0
    assert out[0]["mem_ki"] == 0
    assert out[0]["containers"] == []
    assert out[1]["cpu_m"] == 10
    assert out[1]["mem_ki"] == 20 * 1024

"""Integration-ish test for cache enrichment of `k8s_top_nodes`.

Responsibility: Verify that `k8s_top_nodes` merges kubelet `/stats/summary`
page-cache (`cache_ki` / `cache_pct`) onto each node row, and that a failing
proxy leaves the working-set-only payload intact (no `cache_*` keys, no raise).
Edit boundaries: Only the cache merge path. Quantity parsing is covered by
`test_k8s_metrics_parse.py`; the sampler itself by `test_k8s_node_cache.py`.
Key entry points: `test_top_nodes_merges_cache`, `test_top_nodes_proxy_denied_degrades`.
Risky contracts: Enrichment must never break the existing node payload.
Validation: `uv run pytest -q api/tests/test_k8s_top_nodes_cache.py`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from api.services.k8s import metrics as m

_GIB = 1024**3


class _Resp:
    def __init__(self, payload: Any, *, ok: bool = True) -> None:
        self._payload = payload
        self._ok = ok

    def raise_for_status(self) -> None:
        if not self._ok:
            raise RuntimeError("403 Forbidden")

    def json(self) -> Any:
        return self._payload


class _RoutingSession:
    """Routes `session.get` by URL to the three surfaces k8s_top_nodes hits."""

    def __init__(self, *, proxy_ok: bool = True) -> None:
        self._proxy_ok = proxy_ok
        self.close = MagicMock()

    def get(self, url: str, timeout: float = 0, **_kw: Any) -> Any:
        if url.endswith("/api/v1/nodes"):
            return _Resp(
                {
                    "items": [
                        {
                            "metadata": {
                                "name": "node-a",
                                "labels": {"agentpool": "blastpool"},
                            },
                            "status": {
                                "capacity": {"cpu": "16", "memory": "131072000Ki"},
                                "conditions": [{"type": "Ready", "status": "True"}],
                            },
                        }
                    ]
                }
            )
        if url.endswith("/apis/metrics.k8s.io/v1beta1/nodes"):
            return _Resp(
                {
                    "items": [
                        {
                            "metadata": {"name": "node-a"},
                            "usage": {"cpu": "1000m", "memory": "2097152Ki"},
                        }
                    ]
                }
            )
        if "/proxy/stats/summary" in url:
            # usage 30 GiB, working set 2 GiB -> cache 28 GiB
            return _Resp(
                {
                    "node": {
                        "memory": {
                            "usageBytes": 30 * _GIB,
                            "workingSetBytes": 2 * _GIB,
                        }
                    }
                },
                ok=self._proxy_ok,
            )
        raise AssertionError(f"unexpected URL {url}")


def _run(session: _RoutingSession) -> list[dict[str, Any]]:
    with patch(
        "api.services.k8s.monitoring._get_k8s_session",
        return_value=(session, "https://aks"),
    ):
        return m.k8s_top_nodes(MagicMock(), "sub", "rg", "aks")


def test_top_nodes_merges_cache() -> None:
    nodes = _run(_RoutingSession(proxy_ok=True))
    assert len(nodes) == 1
    node = nodes[0]
    assert node["name"] == "node-a"
    # cache 28 GiB in KiB
    assert node["cache_ki"] == 28 * 1024 * 1024
    # capacity 131072000 Ki -> cache_pct = round(cache_ki / cap * 100)
    expected_pct = round(node["cache_ki"] / node["mem_capacity_ki"] * 100)
    assert node["cache_pct"] == expected_pct
    session_close = node  # sanity that working-set fields survive
    assert session_close["memory_pct"] >= 0


def test_top_nodes_proxy_denied_degrades() -> None:
    nodes = _run(_RoutingSession(proxy_ok=False))
    assert len(nodes) == 1
    node = nodes[0]
    # No cache keys when the proxy is denied; payload otherwise intact.
    assert "cache_ki" not in node
    assert "cache_pct" not in node
    assert node["name"] == "node-a"

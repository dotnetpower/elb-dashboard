"""Unit tests for the kubelet page-cache sampler in `api.services.k8s.node_cache`.

Responsibility: Verify `fetch_node_cache_ki` derives reclaimable page cache from
`usageBytes - workingSetBytes`, parallelises per-node proxy reads, and degrades
to a partial / empty result (never raises) when the kubelet proxy denies, times
out, or returns malformed JSON.
Edit boundaries: Stay focused on the sampler; the merge into the top-nodes
payload is covered by `test_k8s_top_nodes_cache.py`.
Key entry points: `test_fetch_node_cache_ki_happy_path`, `test_partial_failure`,
`test_total_failure_returns_empty`.
Risky contracts: The sampler MUST NOT raise for any transport / RBAC error.
Validation: `uv run pytest -q api/tests/test_k8s_node_cache.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.k8s.node_cache import fetch_node_cache_ki


class _FakeResponse:
    def __init__(self, payload: Any, *, status_ok: bool = True) -> None:
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise RuntimeError("403 Forbidden")

    def json(self) -> Any:
        return self._payload


class _FakeSession:
    """Maps node name (parsed from the proxy URL) to a canned response/raiser."""

    def __init__(self, by_node: dict[str, Any]) -> None:
        self._by_node = by_node

    def get(self, url: str, timeout: float = 0) -> Any:
        # URL shape: {server}/api/v1/nodes/{name}/proxy/stats/summary
        name = url.split("/api/v1/nodes/", 1)[1].split("/proxy/", 1)[0]
        result = self._by_node[name]
        if isinstance(result, Exception):
            raise result
        return result


def _summary(usage_bytes: int, working_set_bytes: int) -> _FakeResponse:
    return _FakeResponse(
        {"node": {"memory": {"usageBytes": usage_bytes, "workingSetBytes": working_set_bytes}}}
    )


def test_fetch_node_cache_ki_happy_path() -> None:
    # usage 30 GiB, working set 2 GiB -> cache 28 GiB -> 28 * 1024 * 1024 KiB
    gib = 1024**3
    session = _FakeSession(
        {
            "node-a": _summary(30 * gib, 2 * gib),
            "node-b": _summary(5 * gib, 5 * gib),  # no cache
        }
    )
    out = fetch_node_cache_ki(session, "https://k8s", ["node-a", "node-b"])
    assert out["node-a"] == 28 * 1024 * 1024
    assert out["node-b"] == 0


def test_negative_cache_clamped_to_zero() -> None:
    gib = 1024**3
    session = _FakeSession({"node-a": _summary(2 * gib, 5 * gib)})
    out = fetch_node_cache_ki(session, "https://k8s", ["node-a"])
    assert out["node-a"] == 0


def test_partial_failure_skips_only_bad_node() -> None:
    gib = 1024**3
    session = _FakeSession(
        {
            "good": _summary(10 * gib, 1 * gib),
            "denied": _FakeResponse(None, status_ok=False),
            "boom": RuntimeError("connection reset"),
        }
    )
    out = fetch_node_cache_ki(session, "https://k8s", ["good", "denied", "boom"])
    assert out == {"good": 9 * 1024 * 1024}


@pytest.mark.parametrize(
    "payload",
    [
        {},  # no node key
        {"node": {}},  # no memory
        {"node": {"memory": {"usageBytes": "x", "workingSetBytes": 1}}},  # wrong type
        {"node": {"memory": {"workingSetBytes": 1}}},  # missing usage
    ],
)
def test_malformed_payload_is_dropped(payload: dict[str, Any]) -> None:
    session = _FakeSession({"node-a": _FakeResponse(payload)})
    out = fetch_node_cache_ki(session, "https://k8s", ["node-a"])
    assert out == {}


def test_empty_node_list_returns_empty() -> None:
    out = fetch_node_cache_ki(_FakeSession({}), "https://k8s", [])
    assert out == {}


def test_blank_names_filtered() -> None:
    out = fetch_node_cache_ki(_FakeSession({}), "https://k8s", ["", "   ".strip()])
    assert out == {}

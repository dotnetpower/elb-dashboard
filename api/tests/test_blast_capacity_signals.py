"""Unit tests for the BLAST capacity gate signal resolver (Stage 3, issue #23).

Responsibility: Exercise ``api.services.blast.capacity_signals`` — the
side-effectful K8s signal resolver that feeds the pure ``evaluate_capacity_gate``
decision tree.
Edit boundaries: Patch the three K8s helpers + ``cached_snapshot_with_cluster_gate``
directly so the test suite never reaches a live cluster or Redis.
Key entry points: ``test_resolve_capacity_signals_happy_path``,
``test_resolve_capacity_signals_pressure_failure_degrades``,
``test_resolve_capacity_signals_counts_pending_pods``,
``test_signal_cache_ttl_clamping``.
Risky contracts: ``resolve_capacity_signals`` must never raise. If a helper
explodes the snapshot must degrade to ``CapacitySignals(None, None, 0)``.
Validation: ``uv run pytest -q api/tests/test_blast_capacity_signals.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.blast import capacity_signals as signals
from api.services.blast.capacity_signals import (
    CapacitySignals,
    resolve_capacity_signals,
    signal_cache_stale_s,
    signal_cache_ttl_s,
)


class _FakeCredential:
    def get_token(self, *_scopes: str) -> Any:  # pragma: no cover - never called
        raise AssertionError("credential should not be used in unit tests")


def _patch_cache_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``cached_snapshot_with_cluster_gate`` always call the loader."""

    def _direct(
        _cache_key: str,
        loader: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return loader()

    monkeypatch.setattr(signals, "cached_snapshot_with_cluster_gate", _direct)


def test_resolve_capacity_signals_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cache_passthrough(monkeypatch)
    monkeypatch.setattr(
        signals,
        "_safe_node_request_pressure",
        lambda *_a, **_k: {"reachable": True, "pools": {"blastpool": {"cpu_request_pct": 40}}},
    )
    monkeypatch.setattr(
        signals,
        "_safe_top_nodes",
        lambda *_a, **_k: [
            {"name": "n0", "pool": "blastpool", "cpu_capacity_m": 4000, "cpu_m": 1000}
        ],
    )
    monkeypatch.setattr(signals, "_safe_pending_pods_count", lambda *_a, **_k: 0)

    snap = resolve_capacity_signals(_FakeCredential(), "sub", "rg", "clu")

    assert isinstance(snap, CapacitySignals)
    assert snap.pressure == {"reachable": True, "pools": {"blastpool": {"cpu_request_pct": 40}}}
    assert snap.top_nodes is not None and snap.top_nodes[0]["name"] == "n0"
    assert snap.pending_pods == 0


def test_resolve_capacity_signals_pressure_failure_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cache_passthrough(monkeypatch)
    monkeypatch.setattr(signals, "_safe_node_request_pressure", lambda *_a, **_k: None)
    monkeypatch.setattr(signals, "_safe_top_nodes", lambda *_a, **_k: None)
    monkeypatch.setattr(signals, "_safe_pending_pods_count", lambda *_a, **_k: 0)

    snap = resolve_capacity_signals(_FakeCredential(), "sub", "rg", "clu")

    assert snap.pressure is None
    assert snap.top_nodes is None
    assert snap.pending_pods == 0


def test_resolve_capacity_signals_counts_pending_pods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cache_passthrough(monkeypatch)
    monkeypatch.setattr(
        signals,
        "_safe_node_request_pressure",
        lambda *_a, **_k: {"reachable": True, "pools": {}},
    )
    monkeypatch.setattr(signals, "_safe_top_nodes", lambda *_a, **_k: [])

    # Simulate the inner k8s_get_pods helper returning mixed phases.
    def _fake_pending(*_a: Any, **_k: Any) -> int:
        return 3

    monkeypatch.setattr(signals, "_safe_pending_pods_count", _fake_pending)
    snap = resolve_capacity_signals(_FakeCredential(), "sub", "rg", "clu")
    assert snap.pending_pods == 3


def test_safe_pending_pods_count_filters_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    # Directly exercise the count helper to confirm the Pending filter.
    fake_pods = [
        {"status": "Running"},
        {"status": "Pending"},
        {"status": "Pending"},
        {"status": "Failed"},
        {"status": ""},
        "not-a-dict",
    ]

    def _fake_k8s_get_pods(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return fake_pods  # type: ignore[return-value]

    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_get_pods", _fake_k8s_get_pods
    )
    count = signals._safe_pending_pods_count(_FakeCredential(), "s", "r", "c", "blastpool")
    assert count == 2


def test_safe_pending_pods_count_degrades_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("kapi down")

    monkeypatch.setattr("api.services.k8s.monitoring.k8s_get_pods", _boom)
    assert signals._safe_pending_pods_count(_FakeCredential(), "s", "r", "c", "blastpool") == 0


def test_signal_cache_ttl_defaults() -> None:
    assert signal_cache_ttl_s() == signals.GATE_DEFAULT_SIGNAL_CACHE_S
    assert signal_cache_stale_s() == signals.GATE_DEFAULT_SIGNAL_STALE_S


def test_signal_cache_ttl_clamping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_GATE_SIGNAL_CACHE_S", "1")
    assert signal_cache_ttl_s() == 5
    monkeypatch.setenv("BLAST_GATE_SIGNAL_CACHE_S", "99999")
    assert signal_cache_ttl_s() == 300
    monkeypatch.setenv("BLAST_GATE_SIGNAL_STALE_S", "1")
    assert signal_cache_stale_s() == 10
    monkeypatch.setenv("BLAST_GATE_SIGNAL_STALE_S", "99999")
    assert signal_cache_stale_s() == 600

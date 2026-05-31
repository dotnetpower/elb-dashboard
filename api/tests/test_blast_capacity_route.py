"""Tests for the ``/api/blast/capacity`` snapshot route (issue #23 Stage 4).

Responsibility: Verify the capacity gate snapshot route returns the documented
JSON shape, never raises on K8s degradation, and exposes the gate's enabled /
disabled state independently of the actual admission decision.
Edit boundaries: Route + sanitisation only; do not exercise live Azure or K8s.
Key entry points: ``test_capacity_snapshot_*``.
Risky contracts: ``require_caller`` must guard the route; tests opt in via
``AUTH_DEV_BYPASS=true``.
Validation: ``uv run pytest -q api/tests/test_blast_capacity_route.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.blast import capacity_gate, capacity_signals
from fastapi.testclient import TestClient


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    from api.main import app

    return TestClient(app)


_QS = "?subscription_id=sub-1&resource_group=rg-elb&cluster_name=aks-elb"


@pytest.fixture(autouse=True)
def _reset_counters() -> None:
    capacity_gate._reset_counters_for_tests()
    yield
    capacity_gate._reset_counters_for_tests()


def test_capacity_snapshot_default_disabled_returns_admit_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BLAST_GATE_ENABLED", raising=False)
    monkeypatch.setattr(
        capacity_signals,
        "resolve_capacity_signals",
        lambda *_a, **_k: capacity_signals.CapacitySignals(
            pressure={
                "reachable": True,
                "pools": {
                    "blastpool": {
                        "cpu_request_pct": 10,
                        "memory_request_pct": 15,
                    }
                },
            },
            top_nodes=[
                {
                    "name": "aks-node-1",
                    "pool": "blastpool",
                    "cpu_m": 200,
                    "cpu_capacity_m": 8000,
                    "mem_ki": 1_000_000,
                    "mem_capacity_ki": 16_777_216,
                    "ready": True,
                }
            ],
            pending_pods=0,
        ),
    )
    monkeypatch.setattr(capacity_gate, "list_active_reservations", lambda *_a, **_k: [])

    client = _client(monkeypatch)
    resp = client.get(f"/api/blast/capacity{_QS}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()["data"]
    assert payload["enabled"] is False
    assert payload["pool"] == "blastpool"
    assert payload["slots"]["in_use"] == 0
    assert payload["slots"]["max"] >= 1
    assert payload["cpu_request_pct"] == 10
    assert payload["memory_request_pct"] == 15
    assert payload["pending_pods"] == 0
    assert payload["decision_preview"] == "admit"
    assert payload["decision_reason"] is None
    assert payload["signals_degraded"] is False
    assert payload["active_reservations"] == []
    assert payload["counters"] == {
        "admit_total": 0,
        "deny_total": 0,
        "release_total": 0,
        "reserve_lost_total": 0,
        "deny_by_reason": {},
        "last_event_at": None,
    }


def test_capacity_snapshot_pressure_failure_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")
    monkeypatch.setattr(
        capacity_signals,
        "resolve_capacity_signals",
        lambda *_a, **_k: capacity_signals.CapacitySignals(
            pressure=None, top_nodes=None, pending_pods=0
        ),
    )
    monkeypatch.setattr(capacity_gate, "list_active_reservations", lambda *_a, **_k: [])

    client = _client(monkeypatch)
    resp = client.get(f"/api/blast/capacity{_QS}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()["data"]
    assert payload["enabled"] is True
    assert payload["signals_degraded"] is True
    # When pressure is missing, the gate denies with ``aks_unreachable``.
    assert payload["decision_preview"] == "deny"
    assert payload["decision_reason"] == "aks_unreachable"
    assert payload["decision_retryable"] is True


def test_capacity_snapshot_lists_active_reservations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")
    monkeypatch.setattr(
        capacity_signals,
        "resolve_capacity_signals",
        lambda *_a, **_k: capacity_signals.CapacitySignals(
            pressure={
                "reachable": True,
                "pools": {"blastpool": {"cpu_request_pct": 50, "memory_request_pct": 50}},
            },
            top_nodes=[
                {
                    "name": "aks-node-1",
                    "pool": "blastpool",
                    "cpu_m": 200,
                    "cpu_capacity_m": 8000,
                    "mem_ki": 1_000_000,
                    "mem_capacity_ki": 16_777_216,
                    "ready": True,
                }
            ],
            pending_pods=0,
        ),
    )
    reservations = [
        capacity_gate.Reservation(
            job_id="job-A",
            cpu_m=1000,
            mem_mib=2048,
            reserved_at="2026-05-31T00:00:00Z",
        ),
    ]
    monkeypatch.setattr(capacity_gate, "list_active_reservations", lambda *_a, **_k: reservations)

    client = _client(monkeypatch)
    resp = client.get(f"/api/blast/capacity{_QS}")
    assert resp.status_code == 200
    payload = resp.json()["data"]
    assert payload["slots"]["in_use"] == 1
    assert len(payload["active_reservations"]) == 1
    assert payload["active_reservations"][0]["job_id"] == "job-A"
    assert payload["active_reservations"][0]["cpu_m"] == 1000


def test_capacity_snapshot_missing_query_param_returns_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capacity_signals,
        "resolve_capacity_signals",
        lambda *_a, **_k: capacity_signals.CapacitySignals(None, None, 0),
    )
    monkeypatch.setattr(capacity_gate, "list_active_reservations", lambda *_a, **_k: [])
    client = _client(monkeypatch)
    resp = client.get("/api/blast/capacity?subscription_id=sub-1")
    # FastAPI validation 422 on missing required ``resource_group`` /
    # ``cluster_name`` — never lets the route swallow a misformed call.
    assert resp.status_code == 422


def test_capacity_snapshot_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setattr(
        capacity_signals,
        "resolve_capacity_signals",
        lambda *_a, **_k: capacity_signals.CapacitySignals(None, None, 0),
    )

    from api.main import app

    unauth_client = TestClient(app)
    resp = unauth_client.get(f"/api/blast/capacity{_QS}")
    assert resp.status_code == 401


def test_capacity_snapshot_signal_resolver_exception_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLAST_GATE_ENABLED", "true")

    def _boom(*_a: Any, **_k: Any) -> capacity_signals.CapacitySignals:
        raise RuntimeError("boom")

    monkeypatch.setattr(capacity_signals, "resolve_capacity_signals", _boom)
    monkeypatch.setattr(capacity_gate, "list_active_reservations", lambda *_a, **_k: [])

    client = _client(monkeypatch)
    resp = client.get(f"/api/blast/capacity{_QS}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()["data"]
    assert payload["signals_error"] == "RuntimeError"
    assert payload["signals_degraded"] is True
    assert payload["decision_preview"] == "deny"


def test_capacity_snapshot_surfaces_telemetry_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pre-bump some counters on the target cluster so the route can echo them.
    capacity_gate.bump_admit("aks-elb")
    capacity_gate.bump_admit("aks-elb")
    capacity_gate.bump_deny("aks-elb", "cpu_watermark")
    capacity_gate.bump_release("aks-elb")
    capacity_gate.bump_reserve_lost("aks-elb")

    monkeypatch.setattr(
        capacity_signals,
        "resolve_capacity_signals",
        lambda *_a, **_k: capacity_signals.CapacitySignals(
            pressure={"reachable": True, "pools": {}}, top_nodes=[], pending_pods=0
        ),
    )
    monkeypatch.setattr(capacity_gate, "list_active_reservations", lambda *_a, **_k: [])

    client = _client(monkeypatch)
    resp = client.get(f"/api/blast/capacity{_QS}")
    assert resp.status_code == 200, resp.text
    counters = resp.json()["data"]["counters"]
    assert counters["admit_total"] == 2
    assert counters["deny_total"] == 1
    assert counters["release_total"] == 1
    assert counters["reserve_lost_total"] == 1
    assert counters["deny_by_reason"] == {"cpu_watermark": 1}
    assert counters["last_event_at"] is not None

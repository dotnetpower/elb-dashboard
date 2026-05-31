"""Tests for capacity gate telemetry counters (issue #23 Stage 5).

Responsibility: Verify the in-process counters increment exactly once per
admit/deny/release/reserve_lost event and that the snapshot returns the
zero-filled defaults when no events have happened on a cluster.
Edit boundaries: Counter helpers only — no Redis, no Celery, no FastAPI.
Key entry points: ``test_counters_*``.
Risky contracts: Counters are stored in a module-level dict guarded by
``threading.Lock`` — tests must call ``_reset_counters_for_tests`` to keep
state isolated.
Validation: ``uv run pytest -q api/tests/test_blast_capacity_gate_counters.py``.
"""

from __future__ import annotations

import threading

import pytest
from api.services.blast import capacity_gate


@pytest.fixture(autouse=True)
def _isolate() -> None:
    capacity_gate._reset_counters_for_tests()
    yield
    capacity_gate._reset_counters_for_tests()


def test_counters_default_to_zero_for_unknown_cluster() -> None:
    snap = capacity_gate.gate_counters_snapshot("aks-unknown")
    assert snap == {
        "admit_total": 0,
        "deny_total": 0,
        "release_total": 0,
        "reserve_lost_total": 0,
        "deny_by_reason": {},
        "last_event_at": None,
    }


def test_counters_admit_increments_and_stamps_last_event_at() -> None:
    capacity_gate.bump_admit("aks-1")
    capacity_gate.bump_admit("aks-1")
    snap = capacity_gate.gate_counters_snapshot("aks-1")
    assert snap["admit_total"] == 2
    assert snap["deny_total"] == 0
    assert snap["last_event_at"] is not None


def test_counters_deny_groups_by_reason_and_handles_none() -> None:
    capacity_gate.bump_deny("aks-1", "cpu_watermark")
    capacity_gate.bump_deny("aks-1", "cpu_watermark")
    capacity_gate.bump_deny("aks-1", "slot_cap_reached")
    capacity_gate.bump_deny("aks-1", None)
    snap = capacity_gate.gate_counters_snapshot("aks-1")
    assert snap["deny_total"] == 4
    assert snap["deny_by_reason"] == {
        "cpu_watermark": 2,
        "slot_cap_reached": 1,
        "unknown": 1,
    }


def test_counters_release_and_reserve_lost_are_separate() -> None:
    capacity_gate.bump_release("aks-1")
    capacity_gate.bump_reserve_lost("aks-1")
    capacity_gate.bump_reserve_lost("aks-1")
    snap = capacity_gate.gate_counters_snapshot("aks-1")
    assert snap["release_total"] == 1
    assert snap["reserve_lost_total"] == 2
    assert snap["admit_total"] == 0
    assert snap["deny_total"] == 0


def test_counters_isolated_per_cluster() -> None:
    capacity_gate.bump_admit("aks-A")
    capacity_gate.bump_admit("aks-A")
    capacity_gate.bump_deny("aks-B", "cpu_watermark")
    snap_a = capacity_gate.gate_counters_snapshot("aks-A")
    snap_b = capacity_gate.gate_counters_snapshot("aks-B")
    assert snap_a["admit_total"] == 2
    assert snap_a["deny_total"] == 0
    assert snap_b["admit_total"] == 0
    assert snap_b["deny_total"] == 1


def test_counters_snapshot_is_defensive_copy() -> None:
    capacity_gate.bump_deny("aks-1", "cpu_watermark")
    snap = capacity_gate.gate_counters_snapshot("aks-1")
    snap["deny_by_reason"]["mutated"] = 999
    # Mutating the returned dict must not affect the live store.
    snap2 = capacity_gate.gate_counters_snapshot("aks-1")
    assert "mutated" not in snap2["deny_by_reason"]


def test_counters_unknown_cluster_falls_back_to_placeholder_bucket() -> None:
    # ``bump_*`` accepts empty cluster names by folding them into "_unknown"
    # so a misconfigured payload still records something rather than blowing
    # up the worker.
    capacity_gate.bump_admit("")
    snap = capacity_gate.gate_counters_snapshot("_unknown")
    assert snap["admit_total"] == 1


def test_counters_are_threadsafe_under_concurrent_bumps() -> None:
    threads = [
        threading.Thread(target=capacity_gate.bump_admit, args=("aks-T",))
        for _ in range(200)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = capacity_gate.gate_counters_snapshot("aks-T")
    assert snap["admit_total"] == 200

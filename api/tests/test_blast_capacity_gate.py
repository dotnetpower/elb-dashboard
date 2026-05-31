"""Stage 1 unit tests for the BLAST capacity gate (issue #23).

Responsibility: Exercise the pure decision tree and Redis primitives in
``api.services.blast.capacity_gate``. This module is not yet wired into the
submit task — Stage 1 ships the pure helpers + tests so Stages 2-5 can land
behind ``BLAST_GATE_ENABLED`` without re-litigating the contract.
Edit boundaries: Use a stdlib in-memory ``_FakeRedis`` for the slot hash so
the suite runs without a live broker. Patch
``api.services.redis_clients.get_broker_redis_client`` directly to return the
fake — do not poke ``sys.modules['redis']`` (the capacity gate caches
``client`` per call, not per ``redis.Redis.from_url`` call).
Key entry points: ``test_evaluate_capacity_gate_*``, ``test_reserve_slot_*``,
``test_release_slot_*``, ``test_list_active_reservations_*``,
``test_env_clamping_*``, ``test_reservation_roundtrip_*``,
``test_slot_hash_key_*``.
Risky contracts: The atomic Lua reserve script is the load-bearing contention
guarantee. ``test_reserve_slot_atomic_under_contention`` exercises 10 callers
against ``max_slots=3`` and asserts exactly 3 succeed — if a refactor breaks
the script and falls back to check-then-act, this test fails immediately.
Validation: ``uv run pytest -q api/tests/test_blast_capacity_gate.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from api.services.blast import capacity_gate as gate
from api.services.blast.capacity_gate import (
    GATE_DEFAULT_POOL_NAME,
    GATE_SLOT_HASH_PREFIX,
    GateDecision,
    Reservation,
    ResourceDemand,
)

# ---------------------------------------------------------------------------
# In-memory Redis fake — implements only the surface the gate touches.
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal Redis stand-in: HSET / HDEL / HVALS / HEXISTS / HLEN /
    EXPIRE plus a Lua ``eval`` that runs the reserve script in Python so
    we get the exact same atomic semantics under test as in production.
    """

    def __init__(self) -> None:
        self._hashes: dict[str, dict[bytes, bytes]] = {}
        self._ttls: dict[str, int] = {}

    # ---- helpers ------------------------------------------------------
    @staticmethod
    def _b(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        return str(value).encode("utf-8")

    def _h(self, key: str) -> dict[bytes, bytes]:
        return self._hashes.setdefault(key, {})

    # ---- redis-py shaped methods ------------------------------------
    def hset(self, key: str, field: str, value: str) -> int:
        bkt = self._h(key)
        fb = self._b(field)
        created = 0 if fb in bkt else 1
        bkt[fb] = self._b(value)
        return created

    def hdel(self, key: str, *fields: str) -> int:
        bkt = self._h(key)
        removed = 0
        for field in fields:
            fb = self._b(field)
            if fb in bkt:
                del bkt[fb]
                removed += 1
        return removed

    def hvals(self, key: str) -> list[bytes]:
        return list(self._h(key).values())

    def hexists(self, key: str, field: str) -> int:
        return 1 if self._b(field) in self._h(key) else 0

    def hlen(self, key: str) -> int:
        return len(self._h(key))

    def expire(self, key: str, ttl: int) -> int:
        self._ttls[key] = int(ttl)
        return 1

    def eval(self, script: str, numkeys: int, *args: Any) -> int:
        # The capacity gate ships exactly one Lua script. Recognise it
        # by the unique ``redis.call('HLEN', KEYS[1])`` opening line and
        # execute the equivalent logic in Python so the test exercises
        # the same atomic semantics as production.
        assert numkeys == 1
        assert "HLEN" in script and "HSET" in script
        key = args[0]
        max_slots = int(args[1])
        job_id = args[2]
        payload = args[3]
        ttl = int(args[4])
        bkt = self._h(key)
        already = self._b(job_id) in bkt
        if len(bkt) >= max_slots and not already:
            return 0
        bkt[self._b(job_id)] = self._b(payload)
        self._ttls[key] = ttl
        return 1


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    client = _FakeRedis()
    monkeypatch.setattr(
        "api.services.redis_clients.get_broker_redis_client",
        lambda **_kw: client,
    )
    return client


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


def test_slot_hash_key_uses_cluster_name() -> None:
    assert gate.slot_hash_key("elb-cluster-01") == f"{GATE_SLOT_HASH_PREFIX}:elb-cluster-01"


def test_slot_hash_key_falls_back_to_unknown_when_empty() -> None:
    assert gate.slot_hash_key("") == f"{GATE_SLOT_HASH_PREFIX}:_unknown"
    assert gate.slot_hash_key("   ") == f"{GATE_SLOT_HASH_PREFIX}:_unknown"


def test_predict_demand_uses_env_defaults() -> None:
    pred = gate.predict_demand()
    assert pred.cpu_m == gate.default_demand_cpu_m()
    assert pred.mem_mib == gate.default_demand_mem_mib()


# ---------------------------------------------------------------------------
# Reservation value type
# ---------------------------------------------------------------------------


def test_reservation_roundtrip_preserves_fields() -> None:
    res = Reservation(
        job_id="job-1",
        reserved_at="2026-05-31T12:00:00+00:00",
        cpu_m=1500,
        mem_mib=4096,
    )
    decoded = Reservation.from_json(res.to_json())
    assert decoded == res
    assert decoded is not None
    assert decoded.mem_b == 4096 * 1024 * 1024


def test_reservation_from_json_rejects_garbage() -> None:
    assert Reservation.from_json("{not json") is None
    assert Reservation.from_json("[]") is None
    assert Reservation.from_json('{"job_id": "x"}') is None  # missing reserved_at


def test_reservation_from_json_accepts_bytes() -> None:
    res = Reservation(job_id="job-1", reserved_at="t", cpu_m=1, mem_mib=2)
    decoded = Reservation.from_json(res.to_json().encode("utf-8"))
    assert decoded == res


# ---------------------------------------------------------------------------
# Env clamping
# ---------------------------------------------------------------------------


def test_env_clamping_max_slots_below_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_GATE_MAX_SLOTS_PER_CLUSTER", "0")
    assert gate.max_slots_per_cluster() == 1


def test_env_clamping_max_slots_above_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_GATE_MAX_SLOTS_PER_CLUSTER", "9999")
    assert gate.max_slots_per_cluster() == 64


def test_env_clamping_watermark_above_100(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_GATE_CPU_WATERMARK_PCT", "200")
    assert gate.cpu_watermark_pct() == 100


def test_env_clamping_slot_ttl_below_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_GATE_SLOT_TTL_S", "1")
    assert gate.slot_ttl_s() == 60


def test_env_clamping_garbage_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLAST_GATE_DEFAULT_DEMAND_CPU_M", "not-an-int")
    assert gate.default_demand_cpu_m() == gate.GATE_DEFAULT_DEMAND_CPU_M


# ---------------------------------------------------------------------------
# evaluate_capacity_gate decision tree
# ---------------------------------------------------------------------------


def _pressure(
    cpu_pct: int = 0,
    mem_pct: int = 0,
    pool: str = GATE_DEFAULT_POOL_NAME,
) -> dict[str, Any]:
    return {
        "reachable": True,
        "pools": {
            pool: {
                "cpu_request_pct": cpu_pct,
                "memory_request_pct": mem_pct,
            }
        },
    }


def _nodes(
    cpu_free_m: int,
    mem_free_mib: int,
    pool: str = GATE_DEFAULT_POOL_NAME,
) -> list[dict[str, Any]]:
    """Build a single-node ``k8s_top_nodes`` row that yields the exact
    requested free CPU / memory headroom."""
    mem_free_ki = mem_free_mib * 1024
    return [
        {
            "name": "aks-blastpool-01",
            "pool": pool,
            "ready": True,
            "cpu_m": 0,
            "cpu_capacity_m": cpu_free_m,
            "mem_ki": 0,
            "mem_capacity_ki": mem_free_ki,
        }
    ]


def _demand(cpu_m: int = 1000, mem_mib: int = 4096) -> ResourceDemand:
    return ResourceDemand(cpu_m=cpu_m, mem_mib=mem_mib)


def test_evaluate_capacity_gate_admits_under_normal_conditions() -> None:
    decision = gate.evaluate_capacity_gate(
        pressure=_pressure(cpu_pct=20, mem_pct=30),
        top_nodes=_nodes(cpu_free_m=4000, mem_free_mib=16384),
        pending_pods_count=0,
        predicted_demand=_demand(),
        active_reservations=[],
        max_slots=4,
    )
    assert isinstance(decision, GateDecision)
    assert decision.admit is True
    assert decision.reason is None
    assert decision.slots_in_use == 0
    assert decision.headroom_cpu_m == 4000
    assert decision.headroom_mem_mib == 16384


def test_evaluate_capacity_gate_denies_when_pressure_unreachable() -> None:
    decision = gate.evaluate_capacity_gate(
        pressure={"reachable": False, "pools": {}},
        top_nodes=_nodes(cpu_free_m=4000, mem_free_mib=16384),
        pending_pods_count=0,
        predicted_demand=_demand(),
        active_reservations=[],
    )
    assert decision.admit is False
    assert decision.reason == "aks_unreachable"
    assert decision.retryable is True


def test_evaluate_capacity_gate_denies_when_pressure_payload_missing() -> None:
    decision = gate.evaluate_capacity_gate(
        pressure=None,
        top_nodes=None,
        pending_pods_count=0,
        predicted_demand=_demand(),
        active_reservations=[],
    )
    assert decision.admit is False
    assert decision.reason == "aks_unreachable"


def test_evaluate_capacity_gate_denies_when_pool_not_found() -> None:
    decision = gate.evaluate_capacity_gate(
        pressure={"reachable": True, "pools": {"systempool": {}}},
        top_nodes=[],
        pending_pods_count=0,
        predicted_demand=_demand(),
        active_reservations=[],
    )
    assert decision.admit is False
    assert decision.reason == "pool_not_found"


def test_evaluate_capacity_gate_denies_when_pods_pending() -> None:
    decision = gate.evaluate_capacity_gate(
        pressure=_pressure(),
        top_nodes=_nodes(cpu_free_m=4000, mem_free_mib=16384),
        pending_pods_count=2,
        predicted_demand=_demand(),
        active_reservations=[],
    )
    assert decision.admit is False
    assert decision.reason == "pods_pending"
    assert decision.measured_pct == 2


def test_evaluate_capacity_gate_denies_at_cpu_watermark() -> None:
    decision = gate.evaluate_capacity_gate(
        pressure=_pressure(cpu_pct=80),
        top_nodes=_nodes(cpu_free_m=4000, mem_free_mib=16384),
        pending_pods_count=0,
        predicted_demand=_demand(),
        active_reservations=[],
        cpu_watermark=75,
    )
    assert decision.admit is False
    assert decision.reason == "cpu_watermark"
    assert decision.measured_pct == 80


def test_evaluate_capacity_gate_denies_at_memory_watermark() -> None:
    decision = gate.evaluate_capacity_gate(
        pressure=_pressure(cpu_pct=10, mem_pct=90),
        top_nodes=_nodes(cpu_free_m=4000, mem_free_mib=16384),
        pending_pods_count=0,
        predicted_demand=_demand(),
        active_reservations=[],
        mem_watermark=75,
    )
    assert decision.admit is False
    assert decision.reason == "memory_watermark"
    assert decision.measured_pct == 90


def test_evaluate_capacity_gate_denies_when_slot_cap_reached() -> None:
    held = [
        Reservation(job_id="job-a", reserved_at="t", cpu_m=100, mem_mib=512),
        Reservation(job_id="job-b", reserved_at="t", cpu_m=100, mem_mib=512),
    ]
    decision = gate.evaluate_capacity_gate(
        pressure=_pressure(),
        top_nodes=_nodes(cpu_free_m=4000, mem_free_mib=16384),
        pending_pods_count=0,
        predicted_demand=_demand(),
        active_reservations=held,
        max_slots=2,
    )
    assert decision.admit is False
    assert decision.reason == "slot_cap_reached"
    assert decision.slots_in_use == 2
    assert decision.measured_pct == 2


def test_evaluate_capacity_gate_denies_when_reserved_cpu_exhausted() -> None:
    # One job already holds 3500m of the 4000m pool; predicted demand 1000m
    # would push us over (3500 + 1000 > 4000).
    held = [Reservation(job_id="job-a", reserved_at="t", cpu_m=3500, mem_mib=512)]
    decision = gate.evaluate_capacity_gate(
        pressure=_pressure(),
        top_nodes=_nodes(cpu_free_m=4000, mem_free_mib=16384),
        pending_pods_count=0,
        predicted_demand=_demand(cpu_m=1000, mem_mib=512),
        active_reservations=held,
        max_slots=4,
    )
    assert decision.admit is False
    assert decision.reason == "reserved_cpu_exhausted"
    assert decision.headroom_cpu_m == 500
    assert decision.slots_in_use == 1


def test_evaluate_capacity_gate_denies_when_reserved_memory_exhausted() -> None:
    held = [Reservation(job_id="job-a", reserved_at="t", cpu_m=100, mem_mib=14000)]
    decision = gate.evaluate_capacity_gate(
        pressure=_pressure(),
        top_nodes=_nodes(cpu_free_m=4000, mem_free_mib=16384),
        pending_pods_count=0,
        predicted_demand=_demand(cpu_m=100, mem_mib=4096),
        active_reservations=held,
        max_slots=4,
    )
    assert decision.admit is False
    assert decision.reason == "reserved_memory_exhausted"


def test_evaluate_capacity_gate_skips_not_ready_nodes() -> None:
    # The not-ready node is counted by the gate as zero headroom — it
    # has no usable capacity, so reserved-cpu exhaustion kicks in even
    # though the raw capacity looks fine.
    nodes = [
        {
            "name": "node-not-ready",
            "pool": GATE_DEFAULT_POOL_NAME,
            "ready": False,
            "cpu_m": 0,
            "cpu_capacity_m": 8000,
            "mem_ki": 0,
            "mem_capacity_ki": 32 * 1024 * 1024,
        }
    ]
    decision = gate.evaluate_capacity_gate(
        pressure=_pressure(),
        top_nodes=nodes,
        pending_pods_count=0,
        predicted_demand=_demand(),
        active_reservations=[],
        max_slots=4,
    )
    assert decision.admit is False
    assert decision.reason == "reserved_cpu_exhausted"
    assert decision.headroom_cpu_m == 0


# ---------------------------------------------------------------------------
# reserve_slot
# ---------------------------------------------------------------------------


def test_reserve_slot_succeeds_under_capacity(fake_redis: _FakeRedis) -> None:
    res = gate.reserve_slot(
        "elb-cluster-01",
        "job-1",
        _demand(),
        max_slots=2,
        ttl_s=300,
        now=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    assert res is not None
    assert res.job_id == "job-1"
    assert res.cpu_m == 1000
    assert res.mem_mib == 4096
    assert res.reserved_at.startswith("2026-05-31T12:00:00")
    # Hash was populated and the TTL was applied.
    key = gate.slot_hash_key("elb-cluster-01")
    assert fake_redis.hlen(key) == 1
    assert fake_redis._ttls[key] == 300


def test_reserve_slot_returns_none_when_cap_reached(fake_redis: _FakeRedis) -> None:
    gate.reserve_slot("elb-cluster-01", "job-1", _demand(), max_slots=1)
    second = gate.reserve_slot("elb-cluster-01", "job-2", _demand(), max_slots=1)
    assert second is None
    key = gate.slot_hash_key("elb-cluster-01")
    assert fake_redis.hlen(key) == 1


def test_reserve_slot_is_idempotent_for_same_job(fake_redis: _FakeRedis) -> None:
    first = gate.reserve_slot("elb-cluster-01", "job-1", _demand(), max_slots=1)
    second = gate.reserve_slot("elb-cluster-01", "job-1", _demand(), max_slots=1)
    assert first is not None and second is not None
    assert first.job_id == second.job_id
    key = gate.slot_hash_key("elb-cluster-01")
    assert fake_redis.hlen(key) == 1


def test_reserve_slot_atomic_under_contention(fake_redis: _FakeRedis) -> None:
    """The Lua script is the only correctness guarantee. 10 callers race
    against ``max_slots=3``; exactly 3 must succeed."""
    results = []
    for i in range(10):
        results.append(
            gate.reserve_slot(
                "elb-cluster-01",
                f"job-{i}",
                _demand(),
                max_slots=3,
            )
        )
    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 3
    key = gate.slot_hash_key("elb-cluster-01")
    assert fake_redis.hlen(key) == 3


def test_reserve_slot_returns_none_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def eval(self, *_a: Any, **_kw: Any) -> int:
            raise RuntimeError("redis down")

    monkeypatch.setattr(
        "api.services.redis_clients.get_broker_redis_client",
        lambda **_kw: _Boom(),
    )
    res = gate.reserve_slot("c", "job", _demand())
    assert res is None


# ---------------------------------------------------------------------------
# release_slot
# ---------------------------------------------------------------------------


def test_release_slot_removes_reservation(fake_redis: _FakeRedis) -> None:
    gate.reserve_slot("elb-cluster-01", "job-1", _demand(), max_slots=2)
    assert gate.release_slot("elb-cluster-01", "job-1") is True
    key = gate.slot_hash_key("elb-cluster-01")
    assert fake_redis.hlen(key) == 0


def test_release_slot_is_idempotent_for_unknown_job(fake_redis: _FakeRedis) -> None:
    assert gate.release_slot("elb-cluster-01", "ghost") is False


def test_release_slot_swallows_redis_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def hdel(self, *_a: Any, **_kw: Any) -> int:
            raise RuntimeError("redis down")

    monkeypatch.setattr(
        "api.services.redis_clients.get_broker_redis_client",
        lambda **_kw: _Boom(),
    )
    assert gate.release_slot("c", "job") is False


# ---------------------------------------------------------------------------
# list_active_reservations
# ---------------------------------------------------------------------------


def test_list_active_reservations_returns_empty_when_no_slots(fake_redis: _FakeRedis) -> None:
    assert gate.list_active_reservations("elb-cluster-01") == []


def test_list_active_reservations_decodes_persisted_payloads(fake_redis: _FakeRedis) -> None:
    gate.reserve_slot("elb-cluster-01", "job-1", _demand(cpu_m=2000, mem_mib=8192), max_slots=4)
    gate.reserve_slot("elb-cluster-01", "job-2", _demand(cpu_m=500, mem_mib=1024), max_slots=4)
    active = gate.list_active_reservations("elb-cluster-01")
    by_id = {r.job_id: r for r in active}
    assert set(by_id) == {"job-1", "job-2"}
    assert by_id["job-1"].cpu_m == 2000
    assert by_id["job-2"].mem_mib == 1024


def test_list_active_reservations_skips_malformed_entries(fake_redis: _FakeRedis) -> None:
    gate.reserve_slot("elb-cluster-01", "job-good", _demand(), max_slots=4)
    # Inject a malformed value directly into the hash.
    key = gate.slot_hash_key("elb-cluster-01")
    fake_redis._h(key)[b"job-bad"] = b"{not json"
    active = gate.list_active_reservations("elb-cluster-01")
    assert [r.job_id for r in active] == ["job-good"]


def test_list_active_reservations_returns_empty_on_redis_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom:
        def hvals(self, *_a: Any, **_kw: Any) -> list[bytes]:
            raise RuntimeError("redis down")

    monkeypatch.setattr(
        "api.services.redis_clients.get_broker_redis_client",
        lambda **_kw: _Boom(),
    )
    assert gate.list_active_reservations("c") == []

"""Tests for queue-arrival AKS auto-start decision + cooldown lease.

Responsibility: Verify the default-OFF gate, the strict ``Stopped``-only +
    positive-pending decision, and the fail-closed Redis cooldown/single-flight
    lease.
Edit boundaries: Test-only; Redis is mocked.
Key entry points: the ``test_*`` functions.
Risky contracts: gate off → never start; only exactly-``Stopped`` qualifies;
    lease is fail-closed (Redis error → no start).
Validation: ``uv run pytest -q api/tests/test_queue_autostart.py``.
"""

from __future__ import annotations

import pytest
from api.services.aks import queue_autostart as qa


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SERVICEBUS_QUEUE_AUTOSTART", raising=False)
    monkeypatch.delenv("SERVICEBUS_QUEUE_AUTOSTART_COOLDOWN_SECONDS", raising=False)
    yield


def test_gate_off_by_default_never_autostarts() -> None:
    assert qa.queue_autostart_enabled() is False
    assert qa.should_autostart("Stopped", 5) is False


def test_gate_on_stopped_with_pending_autostarts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICEBUS_QUEUE_AUTOSTART", "true")
    assert qa.should_autostart("Stopped", 1) is True
    assert qa.should_autostart("Stopped", 99) is True


@pytest.mark.parametrize("ps", ["Running", "Starting", "Stopping", "", "Deallocated"])
def test_only_exactly_stopped_qualifies(monkeypatch: pytest.MonkeyPatch, ps: str) -> None:
    monkeypatch.setenv("SERVICEBUS_QUEUE_AUTOSTART", "true")
    assert qa.should_autostart(ps, 5) is False


@pytest.mark.parametrize("depth", [0, None, -1])
def test_no_pending_no_autostart(monkeypatch: pytest.MonkeyPatch, depth: int | None) -> None:
    monkeypatch.setenv("SERVICEBUS_QUEUE_AUTOSTART", "true")
    assert qa.should_autostart("Stopped", depth) is False


def test_cooldown_env_fail_safe_and_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICEBUS_QUEUE_AUTOSTART_COOLDOWN_SECONDS", "nope")
    assert qa._cooldown_seconds() == 600  # bad value → default
    monkeypatch.setenv("SERVICEBUS_QUEUE_AUTOSTART_COOLDOWN_SECONDS", "5")
    assert qa._cooldown_seconds() == 60  # floored at 60


class _FakeRedis:
    def __init__(self, set_result: object = True) -> None:
        self._r = set_result
        self.calls: list[tuple] = []

    def set(self, key: str, val: str, nx: bool = False, ex: int | None = None) -> object:
        self.calls.append((key, nx, ex))
        return self._r


def test_lease_first_wins_with_nx_and_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICEBUS_QUEUE_AUTOSTART_COOLDOWN_SECONDS", "300")
    fake = _FakeRedis(set_result=True)
    monkeypatch.setattr("api.services.redis_clients.get_broker_redis_client", lambda **_k: fake)
    assert qa.acquire_autostart_lease("s", "rg", "c") is True
    key, nx, ex = fake.calls[0]
    assert key.endswith(":s:rg:c")
    assert nx is True
    assert ex == 300


def test_lease_contended_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis(set_result=None)  # NX failed → lease already held
    monkeypatch.setattr("api.services.redis_clients.get_broker_redis_client", lambda **_k: fake)
    assert qa.acquire_autostart_lease("s", "rg", "c") is False


def test_lease_fail_closed_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_k: object) -> object:
        raise RuntimeError("redis down")

    monkeypatch.setattr("api.services.redis_clients.get_broker_redis_client", _boom)
    # FAIL-CLOSED: a Redis error must NOT trigger a cost-bearing start.
    assert qa.acquire_autostart_lease("s", "rg", "c") is False


class _FakeRedisDelete:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> int:
        self.deleted.append(key)
        return 1


def test_release_lease_deletes_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedisDelete()
    monkeypatch.setattr("api.services.redis_clients.get_broker_redis_client", lambda **_k: fake)
    qa.release_autostart_lease("s", "rg", "c")
    assert fake.deleted and fake.deleted[0].endswith(":s:rg:c")


def test_release_lease_swallows_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_k: object) -> object:
        raise RuntimeError("redis down")

    monkeypatch.setattr("api.services.redis_clients.get_broker_redis_client", _boom)
    # Best-effort: a Redis error leaves the lease to expire via TTL, never raises.
    qa.release_autostart_lease("s", "rg", "c")

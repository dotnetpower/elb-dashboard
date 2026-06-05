"""Tests for `api/services/peering_nsg_lock.py`.

Responsibility: Cover the distributed-lock surface used by the Settings
``apply-nsg-rule`` route: (1) deterministic key derivation per NSG id,
(2) mutual exclusion against the same NSG id, (3) parallelism across
distinct NSG ids, (4) handle release semantics (idempotency + Lua CAS
on the Redis backend), (5) Redis happy path + SET-EX TTL, (6) graceful
fallback to the in-process ``threading.Lock`` dict when Redis is
unavailable or raises mid-acquire, (7) acquire timeout returns
``None`` rather than raising, (8) fake Redis enforces TTL so a regression
that drops ``ex=`` is caught, (9) release prefers EVALSHA + falls back
to EVAL on NOSCRIPT.

Edit boundaries: Pure unit tests, no live Redis. The Redis backend is
exercised via a small in-test fake that mirrors the ``set(nx=True,
ex=…)`` / ``evalsha`` / ``eval`` surface the production code calls,
plus a controllable monotonic clock so TTL expiration is deterministic.
The in-process fallback is verified via the public test hook
``reset_memory_locks_for_tests``.

Key entry points: tests under ``pytest`` defaults.

Risky contracts: The fake Redis client mirrors the real
``client.set(key, value, nx=True, ex=ttl)`` /
``client.evalsha(sha, 1, key, token)`` / ``client.eval(LUA, 1, key,
token)`` contracts. If the production code starts using a different
Redis call shape (``setnx`` / ``getset`` / ``delete``), the fake must
be expanded to match — a silent contract drift would let the
production lock regress without a test failure.

Validation: ``uv run pytest -q api/tests/test_peering_nsg_lock.py``.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from api.services import peering_nsg_lock as lock_mod
from api.services.peering_nsg_lock import (
    NSG_LOCK_KEY_PREFIX,
    NsgLockHandle,
    _short_key,
    acquire_nsg_lock,
    reset_memory_locks_for_tests,
)

# ---------------------------------------------------------------------------
# Common fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_memory_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default each test to the in-process fallback. Tests that exercise
    the Redis backend opt in explicitly by re-patching
    ``_redis_client_or_none``."""
    monkeypatch.setattr(lock_mod, "_redis_client_or_none", lambda: None)
    reset_memory_locks_for_tests()


_NSG_ID_A = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/"
    "rg-a/providers/Microsoft.Network/networkSecurityGroups/nsg-a"
)
_NSG_ID_B = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/"
    "rg-a/providers/Microsoft.Network/networkSecurityGroups/nsg-b"
)


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def test_short_key_is_deterministic_and_prefixed() -> None:
    """Same NSG id always derives the same key, distinct NSGs derive
    distinct keys, and every key carries the canonical prefix so a
    misbehaving caller can't escape the namespace."""
    k1 = _short_key(_NSG_ID_A)
    k2 = _short_key(_NSG_ID_A)
    k3 = _short_key(_NSG_ID_B)
    assert k1 == k2
    assert k1 != k3
    assert k1.startswith(NSG_LOCK_KEY_PREFIX + ":")
    suffix = k1.split(":")[-1]
    assert len(suffix) == 16
    assert all(c in "0123456789abcdef" for c in suffix)


def test_lock_token_is_uniformly_random() -> None:
    """The lock token must come from a CSPRNG (no `time_ns` or `id()`
    collisions) so the Lua CAS release can never accidentally match a
    different holder's token."""
    seen: set[str] = set()
    for _ in range(64):
        handle = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
        assert handle is not None
        assert len(handle.token) == 32
        assert all(c in "0123456789abcdef" for c in handle.token)
        assert handle.token not in seen
        seen.add(handle.token)
        handle.release()


# ---------------------------------------------------------------------------
# In-process fallback — mutual exclusion + ordering
# ---------------------------------------------------------------------------


def test_acquire_same_nsg_twice_returns_none_when_held() -> None:
    """Second acquire against the same NSG id must time out (return
    ``None``) while the first handle is still held."""
    first = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert isinstance(first, NsgLockHandle)
    assert first.backend == "memory"

    second = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert second is None

    first.release()


def test_acquire_after_release_succeeds() -> None:
    """Releasing the first handle frees the lock for the next caller."""
    first = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert first is not None
    first.release()

    second = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert second is not None
    second.release()


def test_distinct_nsg_ids_do_not_block_each_other() -> None:
    """Per-NSG lock — different NSG ids must hold concurrent locks."""
    h_a = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    h_b = acquire_nsg_lock(_NSG_ID_B, timeout_seconds=0.05)
    assert h_a is not None and h_b is not None
    assert h_a.key != h_b.key
    h_a.release()
    h_b.release()


def test_release_is_idempotent() -> None:
    """Calling ``release`` twice must not raise and must not break the
    next acquire."""
    first = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert first is not None
    first.release()
    first.release()  # second release is a no-op

    second = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert second is not None
    second.release()


def test_handle_works_as_context_manager() -> None:
    """``with acquire_nsg_lock(...)`` must release on exit even when the
    body raises."""
    with pytest.raises(RuntimeError, match="boom"):
        handle = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
        assert handle is not None
        with handle:
            raise RuntimeError("boom")

    again = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert again is not None
    again.release()


def test_concurrent_threads_serialise_on_same_nsg() -> None:
    """Two threads contending for the same NSG must observe strict
    mutual exclusion — never two handles in flight at the same time."""
    overlap = {"concurrent": 0, "max": 0}
    overlap_guard = threading.Lock()
    barrier = threading.Barrier(2, timeout=2.0)

    def _worker() -> None:
        barrier.wait(timeout=2.0)
        handle = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=2.0)
        assert handle is not None
        with overlap_guard:
            overlap["concurrent"] += 1
            overlap["max"] = max(overlap["max"], overlap["concurrent"])
        time.sleep(0.05)
        with overlap_guard:
            overlap["concurrent"] -= 1
        handle.release()

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert not t1.is_alive() and not t2.is_alive()
    assert overlap["max"] == 1


def test_acquire_past_deadline_returns_none_without_blocking() -> None:
    """If the deadline is already in the past we must return ``None``
    immediately — ``threading.Lock.acquire(timeout=0)`` would do a
    non-blocking single attempt and that's not what callers passing
    ``timeout_seconds=0`` mean ("give up", not "try once")."""
    held = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert held is not None
    try:
        # Force the deadline into the past by giving the second acquire
        # a negative-relative `now`.
        offset = time.monotonic() + 1.0
        result = acquire_nsg_lock(
            _NSG_ID_A,
            timeout_seconds=0.0,
            now=lambda: offset,
        )
        assert result is None
    finally:
        held.release()


# ---------------------------------------------------------------------------
# In-process fallback — TTL eviction of free entries
# ---------------------------------------------------------------------------


def test_free_memory_entries_are_evicted_past_grace_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A released entry should disappear from the fallback dict once it
    has been free past the eviction grace window so the api sidecar
    does not leak ``threading.Lock`` objects for retired NSGs."""
    handle = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert handle is not None
    handle.release()
    assert _short_key(_NSG_ID_A) in lock_mod._MEM_LOCKS

    future = time.monotonic() + 60.0
    monkeypatch.setattr(lock_mod.time, "monotonic", lambda: future)

    other = acquire_nsg_lock(_NSG_ID_B, timeout_seconds=0.05)
    assert other is not None
    other.release()

    assert _short_key(_NSG_ID_A) not in lock_mod._MEM_LOCKS


# ---------------------------------------------------------------------------
# Redis backend — happy path, contention, fallback on error
# ---------------------------------------------------------------------------


class _Clock:
    """Monotonic-clock stub the FakeRedis + tests can advance manually."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeRedis:
    """In-memory stand-in for the production Redis client.

    Mirrors the surface the lock module touches: ``set(key, value,
    nx=True, ex=ttl)`` and ``evalsha(sha, 1, key, token)`` /
    ``eval(LUA, 1, key, token)``. The store records each entry's
    expiry against a swap-in monotonic clock so a missing ``ex=`` would
    immediately fail the TTL test. Anything else is intentionally
    absent so unexpected calls blow up loudly in tests.
    """

    def __init__(self, *, clock: _Clock | None = None) -> None:
        self._clock = clock or _Clock()
        self._store: dict[str, tuple[str, float]] = {}
        self.set_calls: list[tuple[str, str, int]] = []
        self.evalsha_calls: list[tuple[str, str, str]] = []
        self.eval_calls: list[tuple[str, str]] = []
        self.noscript_first_call = False
        self._loaded_scripts: set[str] = set()

    def _expire_due(self) -> None:
        now = self._clock()
        for k in [k for k, (_, exp) in self._store.items() if exp <= now]:
            self._store.pop(k, None)

    def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
        assert nx is True
        assert isinstance(ex, int) and ex > 0
        self.set_calls.append((key, value, ex))
        self._expire_due()
        if key in self._store:
            return False
        self._store[key] = (value, self._clock() + ex)
        return True

    def evalsha(self, sha: str, numkeys: int, key: str, token: str) -> int:
        assert numkeys == 1
        self.evalsha_calls.append((sha, key, token))
        if self.noscript_first_call and sha not in self._loaded_scripts:
            self._loaded_scripts.add(sha)
            raise RuntimeError("NOSCRIPT No matching script. Please use EVAL.")
        self._expire_due()
        entry = self._store.get(key)
        if entry is not None and entry[0] == token:
            del self._store[key]
            return 1
        return 0

    def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        # The production code passes the same `_RELEASE_LUA` constant —
        # match it via the module-level SHA1 so the test is decoupled
        # from formatting / whitespace changes to the script string.
        assert numkeys == 1
        assert script is lock_mod._RELEASE_LUA or _matches_release_lua(script)
        self.eval_calls.append((key, token))
        self._expire_due()
        entry = self._store.get(key)
        if entry is not None and entry[0] == token:
            del self._store[key]
            return 1
        return 0


def _matches_release_lua(script: str) -> bool:
    import hashlib

    return (
        hashlib.sha1(script.encode("utf-8")).hexdigest()  # noqa: S324
        == lock_mod._RELEASE_LUA_SHA1
    )


def test_redis_backend_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Redis is available, ``acquire_nsg_lock`` SETs the deterministic
    key with NX+EX and ``release`` runs the Lua CAS delete via EVALSHA."""
    fake = _FakeRedis()
    monkeypatch.setattr(lock_mod, "_redis_client_or_none", lambda: fake)

    handle = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05, ttl_seconds=42)
    assert handle is not None
    assert handle.backend == "redis"
    expected_key = _short_key(_NSG_ID_A)
    assert fake.set_calls == [(expected_key, handle.token, 42)]
    assert expected_key in fake._store

    handle.release()
    # EVALSHA preferred; EVAL only used as NOSCRIPT fallback.
    assert fake.evalsha_calls == [(lock_mod._RELEASE_LUA_SHA1, expected_key, handle.token)]
    assert fake.eval_calls == []
    assert expected_key not in fake._store


def test_redis_release_falls_back_to_eval_on_noscript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First EVALSHA against a fresh Redis raises NOSCRIPT; the lock
    must retry the full EVAL once so the script gets cached."""
    fake = _FakeRedis()
    fake.noscript_first_call = True
    monkeypatch.setattr(lock_mod, "_redis_client_or_none", lambda: fake)

    handle = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert handle is not None
    handle.release()
    assert len(fake.evalsha_calls) == 1
    assert len(fake.eval_calls) == 1


def test_redis_release_handles_redis_py_noscript_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """redis-py 5.x raises ``NoScriptError`` whose ``str()`` no longer
    starts with ``NOSCRIPT`` (the prefix is stripped during parsing).
    The lock must detect the error by type and still fall back to EVAL —
    otherwise every release after a Redis restart would re-raise instead
    of auto-reloading the script."""
    from redis.exceptions import NoScriptError

    class _RedisPyEvictedFake(_FakeRedis):
        def evalsha(self, sha: str, numkeys: int, key: str, token: str) -> int:
            self.evalsha_calls.append((sha, key, token))
            raise NoScriptError("No matching script. Please use [E]VAL.")

    fake = _RedisPyEvictedFake()
    monkeypatch.setattr(lock_mod, "_redis_client_or_none", lambda: fake)

    handle = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert handle is not None
    handle.release()
    assert len(fake.evalsha_calls) == 1
    assert len(fake.eval_calls) == 1


def test_redis_lock_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: dropping ``ex=ttl_seconds`` would let the key
    linger forever and a second acquire would never succeed. The fake
    clock advances past the TTL so a second acquire must take over.
    """
    clock = _Clock()
    fake = _FakeRedis(clock=clock)
    monkeypatch.setattr(lock_mod, "_redis_client_or_none", lambda: fake)

    first = acquire_nsg_lock(
        _NSG_ID_A,
        timeout_seconds=0.05,
        ttl_seconds=10,
        now=clock,
    )
    assert first is not None
    # Do not release; pretend the holder crashed.

    # Advance past the TTL.
    clock.advance(11.0)

    second = acquire_nsg_lock(
        _NSG_ID_A,
        timeout_seconds=0.05,
        ttl_seconds=10,
        now=clock,
    )
    assert second is not None
    assert second.backend == "redis"
    second.release()


def test_redis_backend_second_acquire_times_out_when_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second caller against the same NSG id must time out and
    return ``None`` when the Redis key is held by the first caller."""
    fake = _FakeRedis()
    monkeypatch.setattr(lock_mod, "_redis_client_or_none", lambda: fake)
    sleeps: list[float] = []

    def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    first = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert first is not None
    second = acquire_nsg_lock(
        _NSG_ID_A,
        timeout_seconds=0.05,
        sleep=_record_sleep,
    )
    assert second is None
    assert sleeps and all(s <= 0.25 for s in sleeps)

    first.release()


def test_redis_backend_falls_back_to_memory_on_set_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Redis SET raises, the lock module must transparently fall
    back to the in-process dict so the caller still gets a handle."""

    class _RaisingRedis:
        def set(self, *_a: Any, **_kw: Any) -> bool:
            raise RuntimeError("redis is down")

        def evalsha(self, *_a: Any, **_kw: Any) -> int:  # pragma: no cover
            raise AssertionError("evalsha must not be called after SET failed")

        def eval(self, *_a: Any, **_kw: Any) -> int:  # pragma: no cover
            raise AssertionError("eval must not be called after SET failed")

    monkeypatch.setattr(lock_mod, "_redis_client_or_none", lambda: _RaisingRedis())

    handle = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert handle is not None
    assert handle.backend == "memory"
    handle.release()


def test_redis_release_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure inside the Redis release path must not propagate up —
    the lock TTL will retire the key on its own and we must not crash
    the route's ``finally`` block."""

    class _ReleaseRaisingRedis(_FakeRedis):
        def evalsha(self, *_a: Any, **_kw: Any) -> int:
            raise RuntimeError("connection reset")

        def eval(self, *_a: Any, **_kw: Any) -> int:
            raise RuntimeError("connection reset")

    fake = _ReleaseRaisingRedis()
    monkeypatch.setattr(lock_mod, "_redis_client_or_none", lambda: fake)

    handle = acquire_nsg_lock(_NSG_ID_A, timeout_seconds=0.05)
    assert handle is not None
    handle.release()
    handle.release()

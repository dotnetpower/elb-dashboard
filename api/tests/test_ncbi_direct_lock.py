"""Tests for the deployment-wide NCBI Direct Redis lock.

Responsibility: Verify bounded acquisition and owner-checked refresh/release
    calls without connecting to Redis.
Edit boundaries: Pure fake-client tests for `api.services.ncbi_direct_lock`.
Key entry points: Tests for acquire, refresh, and release.
Risky contracts: A stale owner must never refresh or delete a replacement lock.
Validation: `uv run pytest -q api/tests/test_ncbi_direct_lock.py`.
"""

from typing import Any

import pytest
from api.services import ncbi_direct_lock


class _Redis:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.set_result = True
        self.eval_result = 1

    def set(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("set", args, kwargs))
        return self.set_result

    def eval(self, *args: Any) -> int:
        self.calls.append(("eval", *args))
        return self.eval_result


def test_direct_lock_acquire_is_bounded_and_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Redis()
    monkeypatch.setattr(ncbi_direct_lock, "_client", lambda: client)

    assert ncbi_direct_lock.acquire_direct_lock("owner") is True
    call = client.calls[0]
    assert call[0] == "set"
    assert call[2]["nx"] is True
    assert 3600 <= call[2]["ex"] <= 24 * 60 * 60


def test_direct_lock_refresh_and_release_are_owner_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Redis()
    monkeypatch.setattr(ncbi_direct_lock, "_client", lambda: client)

    assert ncbi_direct_lock.refresh_direct_lock("owner") is True
    assert ncbi_direct_lock.release_direct_lock("owner") is True
    scripts = [call[1] for call in client.calls if call[0] == "eval"]
    assert any("EXPIRE" in script and "GET" in script for script in scripts)
    assert any("DEL" in script and "GET" in script for script in scripts)

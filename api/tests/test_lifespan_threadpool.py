"""Tests for the api lifespan thread-pool capacity configuration.

Module summary: Verifies `_configure_threadpool_capacity` widens BOTH the
AnyIO limiter and the asyncio loop default executor from
`API_THREADPOOL_TOKENS`, and is a no-op when the env var is unset/invalid.

Responsibility: Pin the dual-pool sizing contract so `asyncio.to_thread`
  (JWT validation, SSE log streams) cannot be starved on the small asyncio
  default executor while only AnyIO is tuned.
Edit boundaries: Only the threadpool sizing helper.
Key entry points: `test_configures_both_pools`, `test_noop_when_unset`.
Risky contracts: Must run inside a running event loop (asyncio executor swap).
Validation: `uv run pytest -q api/tests/test_lifespan_threadpool.py`.
"""

from __future__ import annotations

import asyncio

import pytest
from api.app.lifespan import _configure_threadpool_capacity


@pytest.mark.asyncio
async def test_configures_both_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_THREADPOOL_TOKENS", "77")
    _configure_threadpool_capacity()

    import anyio.to_thread

    assert anyio.to_thread.current_default_thread_limiter().total_tokens == 77

    # The asyncio default executor was replaced with one sized to the tokens.
    loop = asyncio.get_running_loop()
    executor = loop._default_executor  # type: ignore[attr-defined]
    assert executor is not None
    assert executor._max_workers == 77  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_noop_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_THREADPOOL_TOKENS", raising=False)
    loop = asyncio.get_running_loop()
    before = loop._default_executor  # type: ignore[attr-defined]
    _configure_threadpool_capacity()
    after = loop._default_executor  # type: ignore[attr-defined]
    # Unset env must not swap the executor.
    assert after is before


@pytest.mark.asyncio
async def test_noop_when_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_THREADPOOL_TOKENS", "not-an-int")
    loop = asyncio.get_running_loop()
    before = loop._default_executor  # type: ignore[attr-defined]
    _configure_threadpool_capacity()
    after = loop._default_executor  # type: ignore[attr-defined]
    assert after is before

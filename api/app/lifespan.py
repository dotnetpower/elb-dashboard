"""FastAPI app lifespan — credential warm-up, subscriber start, clean shutdown.

Responsibility: Pre-warm the managed-identity credential, start the BLAST DB
metadata Redis subscriber, and on shutdown close the sidecar SSE broadcaster,
the frontend reverse-proxy client, and the shared httpx pool.
Edit boundaries: Keep this module focused on app-level start/stop work. Per-
sidecar background loops (cgroup reporter etc.) belong in `create_app()`.
Key entry points: `_lifespan`, `_configure_threadpool_capacity`.
Risky contracts: Every shutdown step is wrapped in try/except so a single
failure cannot block the rest. Subscriber stop is conditional on the start
having succeeded.
Validation: `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

LOGGER = logging.getLogger("api.app.lifespan")

# AnyIO's documented default worker-thread limit. Used as the fallback so an
# unset / invalid API_THREADPOOL_TOKENS leaves behaviour identical to today.
_DEFAULT_THREADPOOL_TOKENS = 40


def _configure_threadpool_capacity() -> None:
    """Set the AnyIO default thread-limiter capacity from the environment.

    Reads ``API_THREADPOOL_TOKENS`` (a positive int). Unset, non-numeric, or
    non-positive values leave the limiter at AnyIO's default so the historical
    behaviour is preserved. Failures are swallowed — a missing/renamed AnyIO
    internal must never block startup.
    """
    import os

    raw = os.environ.get("API_THREADPOOL_TOKENS", "").strip()
    if not raw:
        return
    try:
        tokens = int(raw)
    except ValueError:
        LOGGER.warning("API_THREADPOOL_TOKENS=%r is not an integer; ignoring", raw)
        return
    if tokens <= 0:
        LOGGER.warning("API_THREADPOOL_TOKENS=%d must be positive; ignoring", tokens)
        return
    try:
        import anyio.to_thread

        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = tokens
        LOGGER.info(
            "AnyIO thread-pool capacity set to %d (was %s)",
            tokens,
            previous,
        )
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning(
            "failed to set AnyIO thread-pool capacity to %d: %s",
            tokens,
            type(exc).__name__,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan — currently only used to drain the sidecar SSE
    broadcaster cleanly on shutdown so subscribers see an EOF instead of
    a half-closed socket. See `api.routes.monitor._SidecarBroadcaster`.
    """
    # Size the AnyIO worker-thread pool that backs every sync route, every
    # `run_in_threadpool` / `asyncio.to_thread` call, and the Azure SDK
    # blocking calls those wrap. FastAPI/Starlette leave AnyIO at its default
    # of 40 tokens; under a burst of monitor/data-plane requests (each of
    # which offloads several blocking Azure SDK calls) that ceiling can become
    # the throughput bottleneck before CPU does. Allow operators to raise it
    # via API_THREADPOOL_TOKENS without touching code; the default preserves
    # the historical 40-token behaviour exactly.
    _configure_threadpool_capacity()

    # Warm the managed-identity credential at startup so the first
    # bearer-authed call (auth + Storage/Tables/AKS) does not block on a
    # cold IMDS / `az login` token fetch. The fetch happens off-loop so
    # uvicorn keeps accepting connections while the token is being
    # acquired; any failure is logged at debug only — the first real
    # request will retry the token fetch through the normal path.
    try:
        import asyncio

        from api.app.global_exception_logging import install_asyncio_exception_handler
        from api.services import get_credential

        install_asyncio_exception_handler(asyncio.get_running_loop())

        async def _prime() -> None:
            try:
                credential = await asyncio.to_thread(get_credential)
                await asyncio.to_thread(
                    credential.get_token,
                    "https://management.azure.com/.default",
                )
            except Exception as exc:
                LOGGER.debug("credential warm-up skipped: %s", type(exc).__name__)

        # Keep a reference so the task is not GC'd while pending — ruff
        # RUF006 catches the "fire-and-forget without retention" pattern
        # that otherwise lets the asyncio loop cancel the task early.
        app.state._cred_warmup_task = asyncio.create_task(_prime())
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("credential warm-up scheduling skipped: %s", type(exc).__name__)
    try:
        from api.services.blast.db_metadata import (
            start_invalidate_subscriber,
            stop_invalidate_subscriber,
        )

        start_invalidate_subscriber()
    except Exception as exc:
        LOGGER.warning(
            "blast db metadata invalidate subscriber start failed: %s",
            type(exc).__name__,
        )
        stop_invalidate_subscriber = None  # type: ignore[assignment]
    try:
        yield
    finally:
        try:
            from api.routes.monitor import _SIDECAR_BROADCASTER

            await _SIDECAR_BROADCASTER.close()
        except Exception as exc:
            LOGGER.warning("sidecar broadcaster shutdown failed: %s", exc, exc_info=True)
        try:
            from api.routes.frontend_proxy import close_client

            await close_client()
        except Exception as exc:
            LOGGER.debug("frontend_proxy close skipped: %s", type(exc).__name__, exc_info=True)
        try:
            from api.services.httpx_pool import close_all_clients

            close_all_clients()
        except Exception as exc:
            LOGGER.debug("httpx_pool close skipped: %s", type(exc).__name__, exc_info=True)
        if stop_invalidate_subscriber is not None:
            try:
                stop_invalidate_subscriber()
            except Exception as exc:
                LOGGER.debug(
                    "blast db metadata invalidate subscriber stop failed: %s",
                    type(exc).__name__,
                    exc_info=True,
                )

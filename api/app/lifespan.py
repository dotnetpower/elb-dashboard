"""FastAPI app lifespan — credential warm-up, subscriber start, clean shutdown.

Responsibility: Pre-warm the managed-identity credential, start the BLAST DB
metadata Redis subscriber, start the process-local BLAST DB catalogue cache
warmer, and on shutdown stop the warmer, close the sidecar SSE broadcaster,
the frontend reverse-proxy client, and the shared httpx pool.
Edit boundaries: Keep this module focused on app-level start/stop work. Per-
sidecar background loops (cgroup reporter etc.) belong in `create_app()`. The
catalogue warmer's loop logic lives in `api.services.storage.catalog_warmer`.
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
    """Size BOTH worker-thread pools the api offloads blocking work to.

    There are two *separate* pools and historically only one was tunable:

    * **AnyIO default thread-limiter** — backs Starlette sync routes,
      ``run_in_threadpool``, and FastAPI ``Depends`` that block. Tuned via
      ``limiter.total_tokens``.
    * **asyncio loop default executor** — backs every ``asyncio.to_thread(...)``
      call (JWT validation in ``api.auth``, the SSE log-stream Redis/K8s
      blocking reads, ``_load_state``, the credential warm-up). This is a
      ``ThreadPoolExecutor(max_workers=min(32, cpu+4))`` that AnyIO does NOT
      govern, so on a small-vCPU Container App it can be as low as ~5 threads.
      A burst of concurrent SSE log streams (each pinning a blocking
      ``xread`` / pod-log thread) could otherwise starve JWT validation on the
      same tiny pool and stall every authenticated request.

    Reads ``API_THREADPOOL_TOKENS`` (a positive int) and applies it to BOTH
    pools so they widen together. Unset / non-numeric / non-positive leaves
    both at their library defaults (historical behaviour). Must be called from
    inside the running event loop (the asyncio executor swap needs the loop).
    Failures are swallowed — startup must never be blocked by a tuning hint.
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
    try:
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        loop = asyncio.get_running_loop()
        # Replace the loop's default executor so `asyncio.to_thread` gets the
        # same widened capacity. Safe at startup: no work has been submitted to
        # the default executor yet, and the executor lazily spawns threads, so
        # idle capacity costs little. opt-in only — unset env leaves the
        # asyncio default (min(32, cpu+4)) untouched.
        loop.set_default_executor(
            ThreadPoolExecutor(max_workers=tokens, thread_name_prefix="api-to-thread")
        )
        LOGGER.info("asyncio default executor capacity set to %d", tokens)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning(
            "failed to set asyncio default executor capacity to %d: %s",
            tokens,
            type(exc).__name__,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan — currently only used to drain the sidecar SSE
    broadcaster cleanly on shutdown so subscribers see an EOF instead of
    a half-closed socket. See `api.routes.monitor._SidecarBroadcaster`.
    """
    # Size BOTH worker-thread pools the api offloads blocking work to: the
    # AnyIO limiter (Starlette sync routes / `run_in_threadpool` / `Depends`)
    # AND the asyncio loop default executor (`asyncio.to_thread` — JWT
    # validation, SSE log-stream blocking reads, `_load_state`). FastAPI leaves
    # AnyIO at 40 tokens and asyncio at min(32, cpu+4); under a burst of
    # monitor/data-plane requests or concurrent SSE log streams either ceiling
    # can become the bottleneck before CPU does. `API_THREADPOOL_TOKENS` raises
    # both together without touching code; unset preserves both defaults.
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
    # Cross-sidecar jobs / message-flow cache invalidation. The Service Bus
    # request-queue drain runs in the worker sidecar and materialises a durable
    # jobstate row there; this subscriber lets the worker's publish drop the api
    # process's in-process jobs-list / message-flow / external-jobs caches so a
    # queue-ingested job surfaces on the next poll instead of waiting out the
    # cache TTL. Best-effort: a start failure leaves the caches TTL-bounded.
    try:
        from api.services.blast.jobs_cache_signal import (
            start_jobs_cache_subscriber,
            stop_jobs_cache_subscriber,
        )

        start_jobs_cache_subscriber()
    except Exception as exc:
        LOGGER.warning(
            "jobs cache invalidate subscriber start failed: %s",
            type(exc).__name__,
        )
        stop_jobs_cache_subscriber = None  # type: ignore[assignment]
    # Keep the BLAST DB catalogue cache hot in THIS (api) process so the first
    # `GET /api/blast/databases` after the cache TTL expires does not make the
    # user wait on the ~4 s cold enumeration. The cache is process-local, so a
    # worker/beat fill would not help the api read path — the warmer must run
    # here. Failure to start is non-fatal: the read path still works, just cold.
    try:
        from api.services.storage.catalog_warmer import start_catalog_warmer

        start_catalog_warmer(app)
    except Exception as exc:
        LOGGER.debug("catalog warmer scheduling skipped: %s", type(exc).__name__)
    # Opt-in memory diagnostics sampler (default OFF). When
    # API_MEMTRACE_INTERVAL_SECONDS is set it periodically logs RSS + GC stats
    # (and optionally tracemalloc top-N / malloc_trim) so a suspected leak on
    # the single long-lived api process can be confirmed as unbounded growth vs
    # a bounded plateau. No-op and zero cost when unset.
    app.state._memtrace_stop = None
    try:
        from api.app.memory_diagnostics import start_memory_sampler

        app.state._memtrace_stop = start_memory_sampler()
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("memory sampler scheduling skipped: %s", type(exc).__name__)
    try:
        yield
    finally:
        stop_event = getattr(app.state, "_memtrace_stop", None)
        if stop_event is not None:
            stop_event.set()
        try:
            from api.services.storage.catalog_warmer import stop_catalog_warmer

            await stop_catalog_warmer(app)
        except Exception as exc:
            LOGGER.debug(
                "catalog warmer shutdown skipped: %s", type(exc).__name__, exc_info=True
            )
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
        if stop_jobs_cache_subscriber is not None:
            try:
                stop_jobs_cache_subscriber()
            except Exception as exc:
                LOGGER.debug(
                    "jobs cache invalidate subscriber stop failed: %s",
                    type(exc).__name__,
                    exc_info=True,
                )

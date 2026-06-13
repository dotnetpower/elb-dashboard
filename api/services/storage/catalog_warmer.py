"""Background warmer that keeps the BLAST DB catalogue cache hot in the api process.

Responsibility: Periodically populate the *api sidecar's* process-local BLAST
    database catalogue cache (``database_catalog_cache.list_databases_cached``)
    slightly ahead of its TTL expiry, so an interactive ``GET /api/blast/databases``
    served by this same process never pays the cold enumeration (~4 s) a
    freshly-expired cache would otherwise force onto the user. Runs in the api
    process — NOT a Celery/beat task — because the catalogue cache is
    process-local: a worker-side fill never reaches the api process that serves
    the read path.
Edit boundaries: Read-only with respect to Storage — it only fills an in-process
    read cache. Owns account/interval resolution, the no-raise tick contract, and
    the start/stop lifecycle hooks called from ``api.app.lifespan``. The
    enumeration + single-flight + TTL live in ``database_catalog_cache``.
Key entry points: ``start_catalog_warmer`` / ``stop_catalog_warmer`` (lifespan
    hooks) and ``warm_catalog_once`` (one tick, unit-testable).
Risky contracts: ``warm_catalog_once`` must never raise — it runs in a background
    loop whose only job is to keep the cache warm, and a raised tick would still
    be swallowed by the loop but is degraded here for clarity. Must NOT pass
    ``force_refresh`` so a still-fresh cache (or a concurrent single-flight fill
    from a real request) is reused instead of re-enumerating Storage. Exactly one
    warmer task exists per app (stored on ``app.state``).
Validation: ``uv run pytest -q api/tests/test_catalog_warmer.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import FastAPI

LOGGER = logging.getLogger(__name__)

# Warm slightly ahead of the catalogue cache TTL (BLAST_DB_CATALOG_CACHE_TTL,
# default 300 s) so the entry is refreshed before it can expire under an idle
# dashboard. Override with BLAST_DB_CATALOG_WARM_SECONDS; <= 0 disables warming.
_DEFAULT_INTERVAL_SECONDS = 240.0
_STATE_ATTR = "_catalog_warmer"


def _resolve_account() -> str:
    """Resolve the workload Storage account the same way the route's caller does.

    The SPA passes this account to ``GET /api/blast/databases`` and the cache key
    is the account name only, so warming the env-resolved account fills the exact
    entry the read path consults.
    """
    return os.environ.get("STORAGE_ACCOUNT_NAME", "") or os.environ.get(
        "AZURE_STORAGE_ACCOUNT", ""
    )


def _resolve_interval() -> float:
    """Return the warm interval in seconds (default 240; <= 0 disables)."""
    raw = os.environ.get("BLAST_DB_CATALOG_WARM_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning(
            "BLAST_DB_CATALOG_WARM_SECONDS=%r is not numeric; using default", raw
        )
        return _DEFAULT_INTERVAL_SECONDS


async def warm_catalog_once() -> dict[str, Any]:
    """Warm the catalogue cache once for the resolved Storage account.

    No-ops (``skipped``) when no account is configured — the local-dev case.
    Never raises: any failure degrades to a ``failed`` payload so the background
    loop keeps ticking. Deliberately omits ``force_refresh`` so a still-fresh
    cache or a concurrent single-flight fill is reused.
    """
    account = _resolve_account()
    if not account:
        return {"status": "skipped", "reason": "no_storage_account"}
    try:
        from api.services import get_credential
        from api.services.storage.database_catalog_cache import list_databases_cached

        cred = await asyncio.to_thread(get_credential)
        databases = await asyncio.to_thread(list_databases_cached, cred, account)
        return {
            "status": "completed",
            "storage_account": account,
            "database_count": len(databases),
        }
    except Exception as exc:
        LOGGER.debug("catalog warm tick failed for %s: %s", account, type(exc).__name__)
        return {
            "status": "failed",
            "storage_account": account,
            "error": str(exc)[:300],
        }


async def _warm_loop(interval: float, stop: asyncio.Event) -> None:
    """Warm immediately, then re-warm every ``interval`` seconds until stopped.

    The initial tick hides the cold enumeration on the first dashboard load
    after an api restart. Each subsequent tick waits on ``stop`` with a timeout
    so shutdown is prompt (no fixed sleep to drain).
    """
    while not stop.is_set():
        await warm_catalog_once()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


def start_catalog_warmer(app: FastAPI) -> None:
    """Start the single background warmer task, retained on ``app.state``.

    Skips silently when warming is disabled (interval <= 0) or no Storage
    account is configured (local dev). Idempotent: a second call while a warmer
    is already running is a no-op.
    """
    if getattr(app.state, _STATE_ATTR, None) is not None:
        return
    interval = _resolve_interval()
    if interval <= 0:
        LOGGER.info("catalog warmer disabled (interval <= 0)")
        return
    if not _resolve_account():
        LOGGER.debug("catalog warmer not started: no storage account configured")
        return
    stop = asyncio.Event()
    task = asyncio.create_task(_warm_loop(interval, stop))
    setattr(app.state, _STATE_ATTR, (task, stop))
    LOGGER.info("catalog warmer started (interval=%.0fs)", interval)


async def stop_catalog_warmer(app: FastAPI) -> None:
    """Signal the warmer to stop and await it, cancelling if it does not drain."""
    state = getattr(app.state, _STATE_ATTR, None)
    if state is None:
        return
    setattr(app.state, _STATE_ATTR, None)
    task, stop = state
    stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except TimeoutError:
        task.cancel()
    except asyncio.CancelledError:
        task.cancel()
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("catalog warmer stop error: %s", type(exc).__name__)

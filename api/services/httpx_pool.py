"""Shared, module-level pooled ``httpx.Client`` instances.

Responsibility: Provide reusable sync httpx clients so request handlers and Celery tasks do
not pay TLS handshake / connection-pool setup overhead on every call. The async path already
uses ``api/routes/frontend_proxy._client`` as the canonical pattern; this module mirrors it
for sync handlers (FastAPI sync def routes run in the threadpool) and Celery tasks.
Edit boundaries: Only sync ``httpx.Client`` singletons + their close hook. Add new client
slots when a new use case appears with a materially different timeout profile; do not bypass
this module by calling ``httpx.Client(...)`` inline.
Key entry points: `get_pooled_client`, `close_all_clients`
Risky contracts: Singletons are reused across threads (httpx clients are thread-safe). Closing
during a live request raises ``RuntimeError`` on the calling thread; ``close_all_clients`` is
only called from FastAPI ``shutdown`` (and best-effort during pytest teardown). Per-call
timeout overrides are honoured via ``client.request(..., timeout=...)``.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)

_CLIENTS: dict[str, httpx.Client] = {}
_LOCK = threading.Lock()


def get_pooled_client(
    name: str,
    *,
    timeout: float | httpx.Timeout = 10.0,
    **client_kwargs: Any,
) -> httpx.Client:
    """Return (creating on first use) a process-wide pooled ``httpx.Client``.

    ``name`` keys the pool slot so callers with different timeout profiles
    (e.g. ``"terminal-exec-run"``, ``"terminal-exec-stream"``) get separate
    instances; the same ``name`` always returns the same client.

    ``client_kwargs`` are passed to ``httpx.Client(...)`` on first creation
    only — subsequent calls with the same ``name`` ignore them. This is
    intentional: a slot's connection pool must not be re-keyed mid-process.

    Returned client is thread-safe (httpx documents this) so the same
    instance can be shared across threadpool workers and Celery prefork
    children that reuse the parent's import-time pool. The pool re-warms
    transparently after fork because httpx uses lazy connection setup.
    """
    client = _CLIENTS.get(name)
    if client is not None:
        return client
    with _LOCK:
        client = _CLIENTS.get(name)
        if client is not None:
            return client
        client = httpx.Client(timeout=timeout, **client_kwargs)
        _CLIENTS[name] = client
        LOGGER.debug("httpx_pool: created client name=%s timeout=%s", name, timeout)
        return client


def close_all_clients() -> None:
    """Close every pooled client. Called from FastAPI shutdown / test teardown."""
    with _LOCK:
        items = list(_CLIENTS.items())
        _CLIENTS.clear()
    for name, client in items:
        try:
            client.close()
        except Exception as exc:
            LOGGER.debug("httpx_pool: close skipped name=%s err=%s", name, type(exc).__name__)

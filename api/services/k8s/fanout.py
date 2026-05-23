"""Shared thread pool for Kubernetes monitor fan-outs.

Responsibility: Own the process-wide ThreadPoolExecutor used by repeated
Kubernetes monitoring fan-outs.
Edit boundaries: Executor lifecycle only. Do not add Kubernetes HTTP calls here.
Key entry points: `_k8s_fanout_pool`, `_shutdown_k8s_fanout_pool`.
Risky contracts: The pool is process-wide and registered with `atexit`; callers
must not shut it down per request.
Validation: `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py`.
"""

from __future__ import annotations

import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor

_K8S_FANOUT_POOL_MAX_WORKERS = 16
_K8S_FANOUT_POOL: ThreadPoolExecutor | None = None
_K8S_FANOUT_POOL_LOCK = threading.Lock()


def _resolve_k8s_fanout_max_workers() -> int:
    raw = os.environ.get("K8S_FANOUT_POOL_MAX_WORKERS", "")
    if raw:
        try:
            return max(1, min(int(raw), 128))
        except ValueError:
            return _K8S_FANOUT_POOL_MAX_WORKERS
    return _K8S_FANOUT_POOL_MAX_WORKERS


def _k8s_fanout_pool() -> ThreadPoolExecutor:
    """Return the process-shared executor for monitor fan-outs."""
    global _K8S_FANOUT_POOL
    pool = _K8S_FANOUT_POOL
    if pool is not None:
        return pool
    with _K8S_FANOUT_POOL_LOCK:
        if _K8S_FANOUT_POOL is None:
            _K8S_FANOUT_POOL = ThreadPoolExecutor(
                max_workers=_resolve_k8s_fanout_max_workers(),
                thread_name_prefix="k8s-fanout",
            )
        return _K8S_FANOUT_POOL


def _shutdown_k8s_fanout_pool() -> None:
    global _K8S_FANOUT_POOL
    with _K8S_FANOUT_POOL_LOCK:
        pool = _K8S_FANOUT_POOL
        _K8S_FANOUT_POOL = None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown_k8s_fanout_pool)

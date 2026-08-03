"""Shared thread pool for Kubernetes monitor fan-outs.

Responsibility: Own the process-wide, prefork-safe ThreadPoolExecutor used by
repeated Kubernetes monitoring fan-outs.
Edit boundaries: Executor lifecycle only. Do not add Kubernetes HTTP calls here.
Key entry points: `_k8s_fanout_pool`, `_shutdown_k8s_fanout_pool`.
Risky contracts: The pool is process-wide and registered with `atexit`; callers
must not shut it down per request. A forked child must discard the inherited
executor without waiting for parent-owned threads and create its own pool.
Validation: `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py`.
"""

from __future__ import annotations

import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor

_K8S_FANOUT_POOL_MAX_WORKERS = 16
_K8S_FANOUT_POOL: ThreadPoolExecutor | None = None
_K8S_FANOUT_POOL_PID = 0
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
    """Return the current process's executor for monitor fan-outs.

    Celery replacement children can fork after the Service Bus parent has
    already populated all executor threads. Threads do not survive ``fork()``,
    but the copied executor still counts them toward ``max_workers`` and never
    starts a replacement, so every submitted future waits forever. The PID
    guard is a fallback for runtimes that do not expose ``register_at_fork``;
    the registered child hook below normally clears the inherited state first.
    """
    global _K8S_FANOUT_POOL, _K8S_FANOUT_POOL_PID
    current_pid = os.getpid()
    if _K8S_FANOUT_POOL is not None and _K8S_FANOUT_POOL_PID != current_pid:
        _reset_k8s_fanout_pool_after_fork()
    pool = _K8S_FANOUT_POOL
    if pool is not None:
        return pool
    with _K8S_FANOUT_POOL_LOCK:
        if _K8S_FANOUT_POOL is None:
            _K8S_FANOUT_POOL = ThreadPoolExecutor(
                max_workers=_resolve_k8s_fanout_max_workers(),
                thread_name_prefix="k8s-fanout",
            )
            _K8S_FANOUT_POOL_PID = current_pid
        return _K8S_FANOUT_POOL


def _reset_k8s_fanout_pool_after_fork() -> None:
    """Drop inherited executor state in a child without joining dead threads."""
    global _K8S_FANOUT_POOL, _K8S_FANOUT_POOL_PID, _K8S_FANOUT_POOL_LOCK
    _K8S_FANOUT_POOL = None
    _K8S_FANOUT_POOL_PID = 0
    # The parent may have forked while another thread held this lock. Replacing
    # it is required; acquiring the copied locked object would deadlock too.
    _K8S_FANOUT_POOL_LOCK = threading.Lock()


def _shutdown_k8s_fanout_pool() -> None:
    global _K8S_FANOUT_POOL, _K8S_FANOUT_POOL_PID
    with _K8S_FANOUT_POOL_LOCK:
        pool = _K8S_FANOUT_POOL
        _K8S_FANOUT_POOL = None
        _K8S_FANOUT_POOL_PID = 0
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_k8s_fanout_pool_after_fork)

atexit.register(_shutdown_k8s_fanout_pool)

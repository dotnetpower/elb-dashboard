"""Bounded daemon worker pool for fire-and-forget background cache refreshes.

Responsibility: Run fire-and-forget background cache-refresh callables on a
    bounded set of daemon worker threads that never block interpreter / pytest-
    xdist worker shutdown.
Edit boundaries: Pure threading utility; no Azure SDK or HTTP here. Callers
    (``monitor_cache``, ``storage.usage_cache``) own the refresh logic and any
    credential / network access.
Key entry points: ``DaemonRefreshPool`` (``submit``).
Risky contracts: Worker threads are daemon and intentionally NOT created via
    ``concurrent.futures.ThreadPoolExecutor`` — its workers register in
    ``concurrent.futures.thread._threads_queues`` and are *joined* by the
    interpreter-shutdown hook ``_python_exit``. A pool worker blocked on a stuck
    network call (e.g. a background ARM refresh on a CI runner where IMDS is
    reachable) then hangs that join forever, which surfaces as an xdist worker
    that "won't terminate" at ~98-100% and a CI job that runs to its timeout.
    Daemon threads are skipped by both ``_python_exit`` and the interpreter's
    final non-daemon-thread join, so shutdown is always clean. Overflow is
    dropped (the stale cache is still served) rather than queued unbounded.
Validation: ``uv run pytest -q api/tests/test_background_refresh.py``.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

LOGGER = logging.getLogger(__name__)


class DaemonRefreshPool:
    """A fixed set of daemon worker threads draining a bounded job queue.

    ``submit`` is non-blocking: it enqueues the job if there is room and returns
    ``True``, or drops it and returns ``False`` when the queue is full (every
    worker is busy / blocked). Dropping is the correct degraded behaviour for a
    cache refresh — the caller already served the stale value and the next poll
    re-attempts — and it bounds memory under a sustained upstream outage.

    Workers are created lazily on first ``submit`` so importing this module (and
    its callers) never spawns threads, and they are daemon so process / xdist
    worker shutdown never waits on an in-flight refresh.
    """

    def __init__(self, *, max_workers: int, max_queue: int, name: str) -> None:
        self._max_workers = max(1, int(max_workers))
        self._name = name
        self._queue: queue.Queue[Callable[[], object] | None] = queue.Queue(
            maxsize=max(1, int(max_queue))
        )
        self._lock = threading.Lock()
        self._started = False

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            for index in range(self._max_workers):
                thread = threading.Thread(
                    target=self._worker_loop,
                    name=f"{self._name}-{index}",
                    daemon=True,
                )
                thread.start()
            self._started = True

    def _worker_loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                job()
            except Exception:
                LOGGER.warning("%s refresh job failed", self._name, exc_info=True)
            finally:
                self._queue.task_done()

    def submit(self, target: Callable[[], object]) -> bool:
        """Enqueue ``target`` for background execution.

        Returns ``True`` if accepted, ``False`` if the queue was full and the
        job was dropped.
        """
        self._ensure_started()
        try:
            self._queue.put_nowait(target)
            return True
        except queue.Full:
            LOGGER.debug("%s queue full; dropping background refresh", self._name)
            return False

"""Tests for the bounded daemon refresh pool.

Responsibility: Verify ``DaemonRefreshPool`` runs jobs, bounds its backlog, and —
    the load-bearing property — never blocks interpreter / xdist-worker exit even
    when a submitted job is blocked forever.
Edit boundaries: Pure threading behaviour; no Azure/network here.
Key entry points: ``test_*`` functions.
Risky contracts: The exit-does-not-hang test spawns a real subprocess so it
    exercises actual interpreter shutdown (``_python_exit`` + daemon-thread
    handling), which an in-process test cannot.
Validation: ``uv run pytest -q api/tests/test_background_refresh.py``.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest
from api.services.background_refresh import DaemonRefreshPool


def test_submit_runs_job() -> None:
    pool = DaemonRefreshPool(max_workers=2, max_queue=8, name="test-runs")
    done = threading.Event()
    assert pool.submit(done.set) is True
    assert done.wait(timeout=5.0), "submitted job must run on a worker thread"


def test_submit_drops_when_queue_full() -> None:
    # One worker, queue of one. Block the worker, fill the queue, then the next
    # submit must be dropped (returns False) rather than block the caller.
    pool = DaemonRefreshPool(max_workers=1, max_queue=1, name="test-full")
    release = threading.Event()
    started = threading.Event()

    def _block() -> None:
        started.set()
        release.wait(timeout=10.0)

    assert pool.submit(_block) is True
    assert started.wait(timeout=5.0), "the single worker should pick up the first job"
    # Worker is now busy; fill the 1-slot queue, then overflow.
    accepted = pool.submit(lambda: None)
    dropped = pool.submit(lambda: None)
    assert accepted is True
    assert dropped is False
    release.set()


def test_workers_are_daemon() -> None:
    pool = DaemonRefreshPool(max_workers=2, max_queue=4, name="test-daemon")
    pool.submit(lambda: None)
    # Give the lazy workers a moment to spawn.
    deadline = time.time() + 5.0
    workers = []
    while time.time() < deadline:
        workers = [t for t in threading.enumerate() if t.name.startswith("test-daemon-")]
        if workers:
            break
        time.sleep(0.01)
    assert workers, "submit must spawn worker threads"
    assert all(t.daemon for t in workers), "every refresh worker must be a daemon thread"


@pytest.mark.subprocess
@pytest.mark.slow
def test_blocked_job_does_not_block_interpreter_exit() -> None:
    """Regression guard for the CI hang: a refresh blocked on a stuck network
    call must NOT keep the interpreter (or an xdist worker) from terminating.

    A ``concurrent.futures.ThreadPoolExecutor`` would hang here because
    ``_python_exit`` joins its non-daemon worker forever; the daemon pool exits
    promptly and leaves the OS to reclaim the blocked thread.
    """
    program = (
        "import time\n"
        "from api.services.background_refresh import DaemonRefreshPool\n"
        "pool = DaemonRefreshPool(max_workers=2, max_queue=4, name='exit-guard')\n"
        "pool.submit(lambda: time.sleep(600))\n"
        "time.sleep(0.3)\n"
        "print('main-done', flush=True)\n"
    )
    start = time.monotonic()
    proc = subprocess.run(  # noqa: S603 -- fixed argv, runs the project interpreter.
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=20,
    )
    elapsed = time.monotonic() - start
    assert "main-done" in proc.stdout
    assert proc.returncode == 0, proc.stderr
    # Without the daemon-pool fix this process would hang until the 20 s
    # subprocess timeout. It should exit in well under a second of wall time.
    assert elapsed < 10.0, f"interpreter exit was blocked ({elapsed:.1f}s)"

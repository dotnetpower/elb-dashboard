#!/usr/bin/env python3
"""Run latency-critical and artifact Celery workers as isolated processes.

Responsibility: Run latency-critical and artifact Celery workers as isolated processes
Edit boundaries: Keep changes scoped to this module responsibility and update nearby tests.
Key entry points: `_validated`, `_worker_command`, `_terminate`, `main`
Risky contracts: Keep imports lightweight and preserve existing public contracts.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

# The `reconcile` queue carries the beat-scheduled maintenance tasks
# (auto-warmup reconcile, stale-job sweep, upgrade reconcile, AKS autostop,
# …). Routing them off the latency-critical interactive queues keeps a slow
# reconcile pass from queueing behind an operator-triggered BLAST submit. The
# same worker-main process still consumes it, so the isolation is logical
# today and lets a future deployment peel it onto a dedicated low-priority
# worker without code changes.
MAIN_QUEUES = os.environ.get(
    "CELERY_MAIN_QUEUES",
    "default,acr,azure,blast,storage,reconcile",
)
ARTIFACT_QUEUES = os.environ.get("CELERY_ARTIFACT_QUEUES", "blast-artifacts")
MAIN_CONCURRENCY = os.environ.get("CELERY_MAIN_CONCURRENCY", "4")
ARTIFACT_CONCURRENCY = os.environ.get("CELERY_ARTIFACT_CONCURRENCY", "2")
# Pool implementation. Default `prefork` is intentional: the ARM long-running
# pollers in api/tasks/azure/* call `poller.result()` without a per-call
# timeout and rely on Celery's prefork signal-based hard time limit
# (CELERY_TASK_TIME_LIMIT) as the only backstop. A `threads`/`gevent` pool
# would silently disable that safety net, so switching is an explicit opt-in.
WORKER_POOL = os.environ.get("CELERY_POOL", "prefork")
# Recycle a prefork child once its resident memory exceeds this many KiB. This
# is a hard backstop against slow leaks OOM-killing the whole worker sidecar
# (the container memory limit is shared by every prefork child). Unset/`0`
# keeps Celery's default (no memory-based recycling). prefork-only — billiard
# ignores it on threads/gevent pools.
WORKER_MAX_MEMORY_PER_CHILD_KB = os.environ.get("CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB", "")
_QUEUE_RE = re.compile(r"^[A-Za-z0-9_,.-]+$")
_CONCURRENCY_RE = re.compile(r"^[1-9][0-9]{0,2}$")
_POOL_RE = re.compile(r"^[a-z]+$")
_MAX_MEMORY_RE = re.compile(r"^[1-9][0-9]{0,8}$")


def _validated(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _worker_command(
    name: str,
    queues: str,
    concurrency: str,
    *,
    pool: str = WORKER_POOL,
    max_memory_per_child_kb: str = WORKER_MAX_MEMORY_PER_CHILD_KB,
) -> list[str]:
    queues = _validated(queues, _QUEUE_RE, "queues")
    concurrency = _validated(concurrency, _CONCURRENCY_RE, "concurrency")
    pool = _validated(pool, _POOL_RE, "pool")
    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "api.celery_app:celery_app",
        "worker",
        "--loglevel=info",
        "--hostname",
        f"{name}@%h",
        "-Q",
        queues,
        "--pool",
        pool,
        "--concurrency",
        concurrency,
    ]
    # billiard only honours --max-memory-per-child on the prefork pool.
    if pool == "prefork" and max_memory_per_child_kb and max_memory_per_child_kb != "0":
        kb = _validated(max_memory_per_child_kb, _MAX_MEMORY_RE, "max-memory-per-child")
        command.extend(["--max-memory-per-child", kb])
    if os.environ.get("CELERY_WORKER_GOSSIP", "false").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        command.extend(["--without-gossip", "--without-mingle", "--without-heartbeat"])
    return command


def _terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 20
    for process in processes:
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
    for process in processes:
        if process.poll() is None:
            process.kill()


def main() -> int:
    processes = [
        subprocess.Popen(  # noqa: S603 -- argv is static except validated env values.
            _worker_command("worker-main", MAIN_QUEUES, MAIN_CONCURRENCY)
        ),
        subprocess.Popen(  # noqa: S603 -- argv is static except validated env values.
            _worker_command("worker-artifacts", ARTIFACT_QUEUES, ARTIFACT_CONCURRENCY)
        ),
    ]
    stopping = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        _terminate(processes)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not stopping:
        for process in processes:
            code = process.poll()
            if code is not None:
                _terminate(processes)
                return int(code or 0)
        time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

MAIN_QUEUES = os.environ.get(
    "CELERY_MAIN_QUEUES",
    "default,acr,azure,blast,storage",
)
ARTIFACT_QUEUES = os.environ.get("CELERY_ARTIFACT_QUEUES", "blast-artifacts")
MAIN_CONCURRENCY = os.environ.get("CELERY_MAIN_CONCURRENCY", "4")
ARTIFACT_CONCURRENCY = os.environ.get("CELERY_ARTIFACT_CONCURRENCY", "2")
_QUEUE_RE = re.compile(r"^[A-Za-z0-9_,.-]+$")
_CONCURRENCY_RE = re.compile(r"^[1-9][0-9]{0,2}$")


def _validated(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _worker_command(name: str, queues: str, concurrency: str) -> list[str]:
    queues = _validated(queues, _QUEUE_RE, "queues")
    concurrency = _validated(concurrency, _CONCURRENCY_RE, "concurrency")
    return [
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
        "--concurrency",
        concurrency,
    ]


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

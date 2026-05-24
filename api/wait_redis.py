#!/usr/bin/env python3
"""Wait for Redis to become reachable, then exec the remaining arguments.

Responsibility: Wait for Redis to become reachable, then exec the remaining arguments
Edit boundaries: Keep changes scoped to this module responsibility and update nearby tests.
Key entry points: Module import side effects and constants.
Risky contracts: Keep imports lightweight and preserve existing public contracts.
Validation: `uv run pytest -q api/tests`.
"""

import os
import socket
import sys
import time

host = os.environ.get("REDIS_HOST", "127.0.0.1")
port = int(os.environ.get("REDIS_PORT", "6379"))
timeout = int(os.environ.get("REDIS_WAIT_TIMEOUT", "120"))
max_attempts = max(1, int(os.environ.get("REDIS_WAIT_MAX_ATTEMPTS", "300")))

deadline = time.monotonic() + timeout
attempt = 0
while time.monotonic() < deadline:
    attempt += 1
    if attempt > max_attempts:
        print(
            f"FATAL: Redis wait exceeded {max_attempts} attempts for {host}:{port}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    try:
        s = socket.socket()
        s.settimeout(2)
        s.connect((host, port))
        s.close()
        print(f"Redis ready at {host}:{port} (attempt {attempt})", flush=True)
        break
    except OSError:
        print(f"  waiting for Redis {host}:{port} (attempt {attempt})...", flush=True)
        time.sleep(2)
else:
    print(
        f"FATAL: Redis not reachable at {host}:{port} after {timeout}s "
        f"({attempt} attempts)",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)

os.execvp(sys.argv[1], sys.argv[1:])  # noqa: S606

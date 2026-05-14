#!/usr/bin/env python3
"""Standalone cgroup v2 reporter for the terminal sidecar.

Mirror of api/services/cgroup_reporter.py — kept here as a self-contained
script because the terminal image's build context is the `terminal/`
directory, not the repo root, so it cannot import from `api.*`.

If you change the protocol (Redis key shape or payload fields), update
both files in the same commit; the api endpoint
(api/services/sidecar_metrics.py) reads the result.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time

import redis  # provided by /opt/elb/venv

CGROUP_ROOT = "/sys/fs/cgroup"
KEY_PREFIX = "sidecar:metrics:"


def _read_cpu_usec() -> int:
    with open(f"{CGROUP_ROOT}/cpu.stat", encoding="utf-8") as f:
        for line in f:
            if line.startswith("usage_usec"):
                return int(line.split()[1])
    raise RuntimeError("usage_usec missing from cpu.stat")


def _read_mem_bytes() -> int:
    with open(f"{CGROUP_ROOT}/memory.current", encoding="utf-8") as f:
        return int(f.read().strip())


def _read_mem_max() -> int | None:
    try:
        with open(f"{CGROUP_ROOT}/memory.max", encoding="utf-8") as f:
            raw = f.read().strip()
        return None if raw == "max" else int(raw)
    except FileNotFoundError:
        return None


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SIDECAR_NAME", "terminal")
    interval = float(os.environ.get("REPORT_INTERVAL", "5"))
    ttl = int(os.environ.get("REPORT_TTL", "30"))
    url = os.environ.get("OPS_REDIS_URL", "redis://127.0.0.1:6379/2")

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"cgroup_reporter","msg":"%(message)s"}',
    )
    log = logging.getLogger(__name__)

    if os.environ.get("SIDECAR_REPORTER_DISABLED", "").lower() == "true":
        log.info("disabled via SIDECAR_REPORTER_DISABLED=true")
        return 0

    try:
        client = redis.Redis.from_url(url, socket_timeout=1.5)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not init redis client: %s", exc)
        return 1

    host = socket.gethostname()
    mem_max = _read_mem_max()
    log.info("started name=%s host=%s interval=%.1fs ttl=%ds mem_max=%s", name, host, interval, ttl, mem_max)

    try:
        prev_cpu = _read_cpu_usec()
        prev_ts = time.time()
    except Exception as exc:  # noqa: BLE001
        log.warning("initial cgroup read failed (cgroup v1?): %s", exc)
        return 1

    while True:
        time.sleep(interval)
        try:
            cur_cpu = _read_cpu_usec()
            cur_ts = time.time()
            cur_mem = _read_mem_bytes()
            dt = max(1e-6, cur_ts - prev_ts)
            cpu_pct = round(max(0, cur_cpu - prev_cpu) / dt / 10_000, 1)
            mem_pct = round(cur_mem / mem_max * 100, 1) if mem_max else None
            payload = {
                "name": name,
                "host": host,
                "ts": cur_ts,
                "cpu_pct": cpu_pct,
                "mem_bytes": cur_mem,
                "mem_max_bytes": mem_max,
                "mem_pct": mem_pct,
            }
            client.setex(f"{KEY_PREFIX}{name}", ttl, json.dumps(payload))
            prev_cpu, prev_ts = cur_cpu, cur_ts
        except redis.RedisError as exc:
            log.warning("redis error: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("tick failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main() or 0)

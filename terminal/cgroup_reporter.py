#!/usr/bin/env python3
"""Standalone cgroup v2 reporter for the terminal sidecar.

Responsibility: Standalone cgroup v2 reporter for the terminal sidecar
Edit boundaries: Keep terminal-side behavior here; api/worker callers should use service
wrappers.
Key entry points: `_read_cpu_usec`, `_read_mem_bytes`, `_read_mem_max`, `main`
Risky contracts: Do not expose terminal services directly to the internet or log secrets.
Validation: `uv run pytest -q api/tests/test_terminal_toolchain.py
api/tests/test_terminal_command_guard.py`.
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

try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError):
    _CLK_TCK = 100


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


def _read_proc_self_cpu_usec() -> int:
    with open("/proc/self/stat", encoding="utf-8") as f:
        raw = f.read()
    rhs = raw.rsplit(")", 1)[1].split()
    utime_jiffies = int(rhs[11])
    stime_jiffies = int(rhs[12])
    return (utime_jiffies + stime_jiffies) * 1_000_000 // _CLK_TCK


def _read_proc_self_rss_bytes() -> int:
    with open("/proc/self/status", encoding="utf-8") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("VmRSS missing from /proc/self/status")


def _select_reader(log: logging.Logger):
    try:
        prev_cpu = _read_cpu_usec()
        return "cgroup", _read_cpu_usec, _read_mem_bytes, _read_mem_max(), prev_cpu
    except Exception as cgroup_exc:
        try:
            prev_cpu = _read_proc_self_cpu_usec()
        except Exception as procfs_exc:
            log.warning(
                "initial read failed (cgroup=%s, procfs=%s)",
                cgroup_exc,
                procfs_exc,
            )
            raise
        log.info("cgroup unavailable (%s); using procfs fallback", cgroup_exc)
        return "procfs", _read_proc_self_cpu_usec, _read_proc_self_rss_bytes, None, prev_cpu


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
    except Exception as exc:
        log.warning("could not init redis client: %s", exc)
        return 1

    try:
        mode, read_cpu, read_mem, mem_max, prev_cpu = _select_reader(log)
    except Exception as exc:
        log.warning("initial metrics read failed: %s", exc)
        return 1

    host = socket.gethostname()
    prev_ts = time.time()
    log.info(
        "started name=%s host=%s mode=%s interval=%.1fs ttl=%ds mem_max=%s",
        name,
        host,
        mode,
        interval,
        ttl,
        mem_max if mem_max is not None else "unbounded",
    )

    # Suppress redis-connection-refused warnings while the redis sidecar is still warming.
    grace_ticks = max(0, int(os.environ.get("CGROUP_REDIS_GRACE_TICKS", "12")))
    consecutive_redis_failures = 0
    while True:
        time.sleep(interval)
        try:
            cur_cpu = read_cpu()
            cur_ts = time.time()
            cur_mem = read_mem()
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
                "source": mode,
            }
            client.setex(f"{KEY_PREFIX}{name}", ttl, json.dumps(payload))
            prev_cpu, prev_ts = cur_cpu, cur_ts
            if consecutive_redis_failures:
                log.info(
                    "redis recovered after %d failed ticks", consecutive_redis_failures
                )
            consecutive_redis_failures = 0
        except redis.RedisError as exc:
            consecutive_redis_failures += 1
            if consecutive_redis_failures <= grace_ticks:
                log.debug(
                    "redis warming (attempt %d/%d): %s",
                    consecutive_redis_failures,
                    grace_ticks,
                    exc,
                )
            else:
                log.warning("redis error: %s", exc)
        except Exception as exc:
            log.warning("tick failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main() or 0)

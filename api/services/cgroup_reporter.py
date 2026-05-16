"""cgroup v2 metrics reporter for control-plane sidecars.

Each sidecar (api, worker, beat, terminal, frontend) reads its own cgroup
files (`/sys/fs/cgroup/cpu.stat` + `/sys/fs/cgroup/memory.current`) every
``REPORT_INTERVAL`` seconds and SETEXes the snapshot into the in-revision
Redis (db 2) under ``sidecar:metrics:<name>``. ``REPORT_TTL`` controls how
long a stale entry survives if the sidecar process dies — the api treats a
missing key as "down".

Why this instead of App Insights?
  * App Insights `customMetrics` are aggregated at 1-minute intervals which
    breaks the dashboard's "Near real-time · 30s" promise.
  * cgroup v2 files are kernel-maintained, free to read, and exact.
  * We already host an in-revision Redis sidecar with zero network cost
    over the loopback interface.

Why a *push* model and not the api scraping each sidecar's loopback?
  * Some sidecars (nginx, ttyd, redis) don't naturally expose Prometheus
    endpoints. Adding an HTTP server to each of them would be more code
    and more attack surface than a tiny SET-only Redis client.
  * A reporter that crashes silently is detected by TTL expiry; an api
    scrape that times out is harder to distinguish from a slow sidecar.

Multi-process / async safety:
  * The reporter runs in its own daemon thread (api/worker/beat) or as a
    background process (terminal/frontend). It never touches application
    state.
  * Redis writes are independent SETEX calls — no transactions, no Lua,
    no contention.

cgroup v2 path detection:
  * Container Apps on the v2 stack mounts cgroup v2 at /sys/fs/cgroup
    (single hierarchy). v1 is rejected with a clear log message because
    the contract differs (the worker would silently report 0%).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional

import redis

LOGGER = logging.getLogger(__name__)

CGROUP_ROOT = "/sys/fs/cgroup"
DEFAULT_INTERVAL = 5.0
DEFAULT_TTL = 30
DEFAULT_DB = 2  # Reserve db 0 for Celery broker, db 1 for results, db 2 for ops metrics.
REDIS_KEY_PREFIX = "sidecar:metrics:"

# Linux clock-tick rate. Used to convert /proc/<pid>/stat utime+stime jiffies
# to microseconds so the procfs fallback can reuse `compute_cpu_pct` unchanged.
try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError):
    _CLK_TCK = 100  # POSIX default; matches every Linux distro we run on.


@dataclass(frozen=True)
class CgroupReading:
    """A single cgroup snapshot. Two consecutive readings give a CPU%."""

    cpu_usec: int
    mem_bytes: int
    ts: float


def _read_cpu_stat() -> int:
    """Return monotonic ``usage_usec`` from /sys/fs/cgroup/cpu.stat."""
    try:
        with open(f"{CGROUP_ROOT}/cpu.stat", encoding="utf-8") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except FileNotFoundError:
        # cgroup v1 layout — different filenames; we don't support it.
        raise RuntimeError("cgroup v2 not available; v1 layout not supported")
    raise RuntimeError("usage_usec missing from cpu.stat")


def _read_proc_self_cpu_usec() -> int:
    """Return self-process cumulative cpu usage in microseconds.

    Reads fields 14 (utime) + 15 (stime) from /proc/self/stat — the same
    counters ``ps`` and ``top`` use. Cheap (~30 µs).
    """
    with open("/proc/self/stat", encoding="utf-8") as f:
        raw = f.read()
    # The comm field (field 2) is parenthesised and may contain spaces;
    # split on the trailing ')' to keep field indexing correct.
    rhs = raw.rsplit(")", 1)[1].split()
    # rhs[0] is field 3 (state). utime=14 → rhs[11], stime=15 → rhs[12].
    utime_jiffies = int(rhs[11])
    stime_jiffies = int(rhs[12])
    return (utime_jiffies + stime_jiffies) * 1_000_000 // _CLK_TCK


def _read_proc_self_rss_bytes() -> int:
    """Return self-process resident set size in bytes from /proc/self/status."""
    with open("/proc/self/status", encoding="utf-8") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                # Format: "VmRSS:\t   12345 kB"
                kb = int(line.split()[1])
                return kb * 1024
    raise RuntimeError("VmRSS missing from /proc/self/status")


def _read_memory_current() -> int:
    with open(f"{CGROUP_ROOT}/memory.current", encoding="utf-8") as f:
        return int(f.read().strip())


def _read_memory_max() -> Optional[int]:
    """Memory limit in bytes (None if unbounded)."""
    try:
        with open(f"{CGROUP_ROOT}/memory.max", encoding="utf-8") as f:
            raw = f.read().strip()
        if raw == "max":
            return None
        return int(raw)
    except FileNotFoundError:
        return None


def read_cgroup() -> CgroupReading:
    """One synchronous cgroup snapshot. Cheap (~50 µs)."""
    return CgroupReading(
        cpu_usec=_read_cpu_stat(),
        mem_bytes=_read_memory_current(),
        ts=time.time(),
    )


def read_procfs_self() -> CgroupReading:
    """Self-process snapshot via /proc/self/* — used when cgroup v2 is not
    available (e.g. WSL2 host root cgroup, macOS dev laptops via WSL).

    Reuses the ``CgroupReading`` shape so the rest of the loop is unchanged.
    Reports per-process metrics, not the whole cgroup, but for local dev that
    is exactly what the operator wants to see for the api/worker/beat python
    processes anyway.
    """
    return CgroupReading(
        cpu_usec=_read_proc_self_cpu_usec(),
        mem_bytes=_read_proc_self_rss_bytes(),
        ts=time.time(),
    )


def compute_cpu_pct(prev: CgroupReading, cur: CgroupReading) -> float:
    """CPU% over the [prev, cur] window. 100% == one full core fully used."""
    dt = cur.ts - prev.ts
    if dt <= 0:
        return 0.0
    delta_usec = max(0, cur.cpu_usec - prev.cpu_usec)
    return round(delta_usec / dt / 10_000, 1)  # usec/sec → pct (1 core = 1_000_000 usec/sec)


def _redis_url() -> str:
    return os.environ.get("OPS_REDIS_URL", "redis://127.0.0.1:6379/2")


def _publish(client: redis.Redis, name: str, payload: dict, ttl: int) -> None:
    client.setex(f"{REDIS_KEY_PREFIX}{name}", ttl, json.dumps(payload))


def report_loop(
    name: str,
    interval: float = DEFAULT_INTERVAL,
    ttl: int = DEFAULT_TTL,
    redis_url: Optional[str] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Blocking reporter loop. Run in a daemon thread or dedicated process.

    On any error the loop logs and continues — never raises out, since the
    sidecar's primary job (serving traffic / running tasks) must keep going
    even if metrics publishing breaks. A TTL-expired key signals "no data"
    to the api endpoint.
    """
    url = redis_url or _redis_url()
    client = redis.Redis.from_url(url, socket_timeout=1.5)
    host = socket.gethostname()
    mem_max = _read_memory_max()

    # Prefer the container's own cgroup; fall back to /proc/self/* on hosts
    # where cgroup v2 is mounted but the root cgroup lacks per-controller
    # files (WSL2 host) or where this process isn't in its own scope. The
    # fallback reports self-process metrics, which is what a developer
    # running `uvicorn` / `celery` from a laptop actually wants to see.
    reader = read_cgroup
    mode = "cgroup"
    try:
        prev = read_cgroup()
    except Exception as cgroup_exc:  # noqa: BLE001
        try:
            prev = read_procfs_self()
        except Exception as procfs_exc:  # noqa: BLE001
            LOGGER.warning(
                "cgroup_reporter[%s]: initial read failed (cgroup=%s, procfs=%s)",
                name,
                cgroup_exc,
                procfs_exc,
            )
            return
        reader = read_procfs_self
        mode = "procfs"
        # mem_max only makes sense in cgroup mode — in procfs mode the
        # process is bounded by the host, not a container limit.
        mem_max = None
        LOGGER.info(
            "cgroup_reporter[%s]: cgroup unavailable (%s); using procfs fallback",
            name,
            cgroup_exc,
        )

    LOGGER.info(
        "cgroup_reporter[%s]: started (host=%s mode=%s interval=%.1fs ttl=%ds mem_max=%s)",
        name,
        host,
        mode,
        interval,
        ttl,
        mem_max if mem_max is not None else "unbounded",
    )

    while True:
        if stop_event is not None and stop_event.wait(timeout=interval):
            LOGGER.info("cgroup_reporter[%s]: stop requested", name)
            return
        elif stop_event is None:
            time.sleep(interval)
        try:
            cur = reader()
            cpu_pct = compute_cpu_pct(prev, cur)
            mem_pct = round(cur.mem_bytes / mem_max * 100, 1) if mem_max else None
            payload = {
                "name": name,
                "host": host,
                "ts": cur.ts,
                "cpu_pct": cpu_pct,
                "mem_bytes": cur.mem_bytes,
                "mem_max_bytes": mem_max,
                "mem_pct": mem_pct,
                "source": mode,
            }
            _publish(client, name, payload, ttl)
            prev = cur
        except redis.RedisError as exc:
            LOGGER.warning("cgroup_reporter[%s]: redis error: %s", name, exc)
            # Don't update prev so the next successful publish reflects the
            # true CPU window since the last good reading.
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("cgroup_reporter[%s]: tick failed: %s", name, exc)


def start_in_thread(name: str, **kwargs) -> threading.Event:
    """Spawn the reporter as a daemon thread. Returns its stop event."""
    stop = threading.Event()
    t = threading.Thread(
        target=report_loop,
        name=f"cgroup-reporter-{name}",
        kwargs={"name": name, "stop_event": stop, **kwargs},
        daemon=True,
    )
    t.start()
    return stop


def main() -> None:
    """Entry point for non-Python sidecars (terminal, frontend) that run
    this script via ``python3 -m api.services.cgroup_reporter <name>``.
    """
    import sys

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )
    name = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SIDECAR_NAME")
    if not name:
        raise SystemExit("usage: python3 -m api.services.cgroup_reporter <sidecar_name>")
    interval = float(os.environ.get("REPORT_INTERVAL", DEFAULT_INTERVAL))
    ttl = int(os.environ.get("REPORT_TTL", DEFAULT_TTL))
    report_loop(name, interval=interval, ttl=ttl)


if __name__ == "__main__":
    main()

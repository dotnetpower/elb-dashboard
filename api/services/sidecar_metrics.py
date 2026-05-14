"""Aggregator: read sidecar:metrics:* from Redis db 2 and add health flags.

Sidecars are expected to publish snapshots to Redis db 2 every
``REPORT_INTERVAL`` (default 5s). This module:
  * fans out a single ``MGET`` to retrieve all of them in one round-trip,
  * pulls Redis's own CPU/MEM via ``INFO`` (Redis itself does not run a
    cgroup reporter — saves us a sidecar shell change),
  * derives a coarse health enum (ok / degraded / down) from staleness +
    presence,
  * returns a stable JSON shape that both the snapshot endpoint
    (``GET /api/monitor/sidecars``) and the SSE stream
    (``GET /api/monitor/sidecars/events``) consume.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import redis

LOGGER = logging.getLogger(__name__)

KEY_PREFIX = "sidecar:metrics:"
KNOWN_SIDECARS = ("frontend", "api", "worker", "beat", "terminal")
DEFAULT_STALE_AFTER_SEC = 15.0  # 3× the 5s reporter interval.
DEFAULT_DEGRADED_AFTER_SEC = 10.0


def _redis_url() -> str:
    return os.environ.get("OPS_REDIS_URL", "redis://127.0.0.1:6379/2")


def _classify(now: float, payload: Optional[dict]) -> str:
    if payload is None:
        return "down"
    age = now - float(payload.get("ts", 0))
    if age > DEFAULT_STALE_AFTER_SEC:
        return "down"
    if age > DEFAULT_DEGRADED_AFTER_SEC:
        return "degraded"
    return "ok"


def _redis_self_snapshot(client: redis.Redis) -> dict[str, Any]:
    """Build a redis-sidecar entry directly from INFO (no reporter needed).

    Notes:
      * ``used_memory`` is the in-process working set; that's what ops
        actually care about for the broker.
      * Redis does not expose CPU% as a ratio, only counters (``used_cpu_sys``,
        ``used_cpu_user``); compute a delta against the previous call so
        the dashboard sees a meaningful number. We cache the previous
        sample on the function attribute so the first call returns 0.0
        and subsequent calls return real percentages.
    """
    try:
        info_mem = client.info("memory")
        info_cpu = client.info("cpu")
        info_server = client.info("server")
    except redis.RedisError as exc:
        LOGGER.warning("redis self-info failed: %s", exc)
        return {
            "name": "redis",
            "ts": time.time(),
            "cpu_pct": 0.0,
            "mem_bytes": 0,
            "mem_max_bytes": None,
            "mem_pct": None,
            "_error": str(exc)[:120],
        }

    now = time.time()
    cpu_total = float(info_cpu.get("used_cpu_sys", 0)) + float(info_cpu.get("used_cpu_user", 0))

    prev = getattr(_redis_self_snapshot, "_prev", None)
    if prev is not None:
        dt = max(1e-3, now - prev["ts"])
        cpu_pct = round(max(0.0, cpu_total - prev["cpu_total"]) / dt * 100.0, 1)
    else:
        cpu_pct = 0.0
    _redis_self_snapshot._prev = {"ts": now, "cpu_total": cpu_total}  # type: ignore[attr-defined]

    return {
        "name": "redis",
        "ts": now,
        "cpu_pct": cpu_pct,
        "mem_bytes": int(info_mem.get("used_memory", 0)),
        "mem_max_bytes": int(info_mem.get("maxmemory", 0)) or None,
        "mem_pct": None,
        "redis_version": info_server.get("redis_version"),
    }


def collect_snapshot(redis_url: Optional[str] = None) -> dict[str, Any]:
    """Return the unified payload consumed by the SPA card.

    Shape::

        {
          "ts": 1715745600.123,
          "revision": "ca-elb-control--r0042",
          "sidecars": {
             "frontend": {"name": "frontend", "health": "ok",
                          "cpu_pct": 1.2, "mem_bytes": 18874368, ...},
             "api":      { ... },
             ...
             "redis":    { ... },
          }
        }

    Reads are best-effort: a missing or malformed entry surfaces as
    ``health = "down"`` so the UI can render an explicit failure rather
    than a blank tile.
    """
    client = redis.Redis.from_url(redis_url or _redis_url(), socket_timeout=1.5)
    now = time.time()

    keys = [f"{KEY_PREFIX}{n}" for n in KNOWN_SIDECARS]
    raw = client.mget(keys)
    sidecars: dict[str, dict[str, Any]] = {}
    for name, blob in zip(KNOWN_SIDECARS, raw):
        if blob is None:
            sidecars[name] = {"name": name, "health": "down", "ts": None}
            continue
        try:
            payload = json.loads(blob)
        except (TypeError, ValueError):
            sidecars[name] = {"name": name, "health": "down", "ts": None, "_error": "bad_json"}
            continue
        payload["health"] = _classify(now, payload)
        sidecars[name] = payload

    # Redis itself — no reporter, computed from INFO.
    redis_entry = _redis_self_snapshot(client)
    redis_entry["health"] = _classify(now, redis_entry)
    sidecars["redis"] = redis_entry

    return {
        "ts": now,
        "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
        "sidecars": sidecars,
    }

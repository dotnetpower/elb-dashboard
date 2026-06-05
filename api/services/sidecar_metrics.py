"""Aggregate control-plane sidecar metrics from Redis db 2.

Responsibility: Aggregate control-plane sidecar metrics from Redis db 2
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `HealthThresholds`, `RedisCpuSampler`, `_redis_url`, `collect_snapshot`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from typing import Any
from urllib.parse import urlsplit

import redis

LOGGER = logging.getLogger(__name__)

KEY_PREFIX = "sidecar:metrics:"
KNOWN_SIDECARS = ("frontend", "api", "worker", "beat", "terminal")
ALL_SIDECARS = (*KNOWN_SIDECARS, "redis")
DEFAULT_STALE_AFTER_SEC = 15.0  # 3x the 5s reporter interval.
DEFAULT_DEGRADED_AFTER_SEC = 10.0
LOCAL_PROBE_TARGETS = {
    "frontend": ("LOCAL_FRONTEND_HEALTH_URL", "http://127.0.0.1:8090/"),
    "terminal": ("LOCAL_TERMINAL_HEALTH_URL", "http://127.0.0.1:7682/healthz"),
}
LOCAL_PROBE_TIMEOUT_SEC = 0.5


@dataclass(frozen=True, slots=True)
class HealthThresholds:
    degraded_after_sec: float = DEFAULT_DEGRADED_AFTER_SEC
    stale_after_sec: float = DEFAULT_STALE_AFTER_SEC

    def classify_ts(self, now: float, ts: float | None) -> str:
        if ts is None or ts <= 0:
            return "down"
        age = max(0.0, now - ts)
        if age > self.stale_after_sec:
            return "down"
        if age > self.degraded_after_sec:
            return "degraded"
        return "ok"


DEFAULT_THRESHOLDS = HealthThresholds()


@dataclass(slots=True)
class RedisCpuSampler:
    previous_ts: float | None = None
    previous_cpu_total: float | None = None

    def percent(self, now: float, cpu_total: float) -> float:
        if self.previous_ts is None or self.previous_cpu_total is None:
            self.previous_ts = now
            self.previous_cpu_total = cpu_total
            return 0.0

        dt = max(1e-3, now - self.previous_ts)
        cpu_pct = round(max(0.0, cpu_total - self.previous_cpu_total) / dt * 100.0, 1)
        self.previous_ts = now
        self.previous_cpu_total = cpu_total
        return cpu_pct


_REDIS_CPU_SAMPLER = RedisCpuSampler()

# Dedup window for the "sidecar metrics mget failed" warning. The /api/monitor
# /sidecars route polls every few seconds; without dedup, a sustained Redis
# sidecar restart produces one App Insights warning per poll tick. Re-emit
# the WARNING (with stack) once per window so a real new outage class still
# surfaces; repeats inside the window log at DEBUG only.
_REDIS_UNAVAILABLE_DEDUP_WINDOW_SECONDS = 300.0
_REDIS_UNAVAILABLE_LAST_WARNED: dict[str, float] = {}


def _log_redis_unavailable(exc: BaseException) -> None:
    """Log Redis-unreachable failures with per-error-class deduplication."""
    key = type(exc).__name__
    now = time.monotonic()
    last = _REDIS_UNAVAILABLE_LAST_WARNED.get(key)
    if last is None or (now - last) >= _REDIS_UNAVAILABLE_DEDUP_WINDOW_SECONDS:
        LOGGER.warning("sidecar metrics mget failed: %s", exc)
        _REDIS_UNAVAILABLE_LAST_WARNED[key] = now
    else:
        LOGGER.debug("sidecar metrics mget failed (deduped): %s", exc)


def _reset_redis_unavailable_dedup() -> None:
    """Test-only: clear the dedup map so tests are deterministic."""
    _REDIS_UNAVAILABLE_LAST_WARNED.clear()


def _redis_url() -> str:
    return os.environ.get("OPS_REDIS_URL", "redis://127.0.0.1:6379/2")


def _ts_from_payload(payload: Mapping[str, Any]) -> float | None:
    raw = payload.get("ts")
    try:
        ts = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return ts if math.isfinite(ts) else None


def _classify(
    now: float,
    payload: Mapping[str, Any] | None,
    thresholds: HealthThresholds = DEFAULT_THRESHOLDS,
) -> str:
    if payload is None:
        return "down"
    return thresholds.classify_ts(now, _ts_from_payload(payload))


def _down_entry(name: str, reason: str, error: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "health": "down", "ts": None, "_error": reason}
    if error:
        entry["_detail"] = error[:120]
    return entry


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _local_probe_enabled() -> bool:
    """Enable host-mode sidecar probes only for local development.

    Container Apps and docker-compose sidecars publish their own Redis reporter
    entries. The host-mode dev loop runs Vite and terminal/exec_server.py as
    plain host processes, so no ``sidecar:metrics:<name>`` key exists for them.
    In that local-only case, probing the known loopback ports keeps the
    dashboard topology honest without masking production telemetry failures.
    """

    revision = os.environ.get("CONTAINER_APP_REVISION", "local")
    return _env_bool("LOCAL_SIDECAR_PROBES", revision == "local")


def _probe_http_ok(url: str, timeout_sec: float = LOCAL_PROBE_TIMEOUT_SEC) -> bool:
    connection: HTTPConnection | None = None
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return False
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout_sec)
        connection.request("GET", path, headers={"User-Agent": "elb-dashboard-local-probe"})
        response = connection.getresponse()
        return 200 <= int(response.status) < 400
    except (HTTPException, OSError, TimeoutError, ValueError):
        return False
    finally:
        if connection is not None:
            connection.close()


def _apply_local_probe_fallbacks(
    sidecars: dict[str, dict[str, Any]],
    now: float,
    *,
    probe_http_ok: Any = _probe_http_ok,
) -> None:
    if not _local_probe_enabled():
        return

    for name, (env_name, default_url) in LOCAL_PROBE_TARGETS.items():
        current = sidecars.get(name)
        if current is None or current.get("health") != "down" or current.get("_error") != "missing":
            continue
        url = os.environ.get(env_name, default_url)
        if not probe_http_ok(url):
            continue
        sidecars[name] = {
            "name": name,
            "health": "ok",
            "ts": now,
            "source": "local_probe",
            "probe_url": url,
        }


def _decode_reporter_entry(name: str, blob: object, now: float) -> dict[str, Any]:
    if blob is None:
        return _down_entry(name, "missing")
    try:
        decoded = json.loads(blob)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _down_entry(name, "bad_json")
    if not isinstance(decoded, dict):
        return _down_entry(name, "bad_payload")

    entry = dict(decoded)
    entry["name"] = name
    entry["health"] = _classify(now, entry)
    if entry["health"] == "down" and _ts_from_payload(entry) is None:
        entry.setdefault("_error", "bad_ts")
    return entry


def _reporter_keys() -> list[str]:
    return [f"{KEY_PREFIX}{name}" for name in KNOWN_SIDECARS]


def _all_down_snapshot(now: float, reason: str, error: str | None = None) -> dict[str, Any]:
    return {
        "ts": now,
        "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
        "sidecars": {name: _down_entry(name, reason, error) for name in ALL_SIDECARS},
        "events": {"row1": 0, "row2": 0, "row3": 0, "row4": 0},
        "degraded": True,
        "degraded_reason": reason,
    }


def _mget_reporters(client: redis.Redis) -> Sequence[object]:
    raw: list[object] = client.mget(_reporter_keys())  # type: ignore[assignment]
    return raw if raw is not None else []


def _redis_self_snapshot(
    client: redis.Redis,
    now: float,
    sampler: RedisCpuSampler = _REDIS_CPU_SAMPLER,
) -> dict[str, Any]:
    try:
        info_mem: dict[str, Any] = client.info("memory")  # type: ignore[assignment]
        info_cpu: dict[str, Any] = client.info("cpu")  # type: ignore[assignment]
        info_server: dict[str, Any] = client.info("server")  # type: ignore[assignment]
    except redis.RedisError as exc:
        LOGGER.warning("redis self-info failed: %s", exc)
        return {
            "name": "redis",
            "health": "degraded",
            "ts": now,
            "cpu_pct": 0.0,
            "mem_bytes": 0,
            "mem_max_bytes": None,
            "mem_pct": None,
            "_error": "redis_info_failed",
            "_detail": str(exc)[:120],
        }

    cpu_total = float(info_cpu.get("used_cpu_sys", 0)) + float(info_cpu.get("used_cpu_user", 0))

    return {
        "name": "redis",
        "ts": now,
        "health": "ok",
        "cpu_pct": sampler.percent(now, cpu_total),
        "mem_bytes": int(info_mem.get("used_memory", 0)),
        "mem_max_bytes": int(info_mem.get("maxmemory", 0)) or None,
        "mem_pct": None,
        "redis_version": info_server.get("redis_version"),
    }


def collect_snapshot(
    redis_url: str | None = None,
    client: redis.Redis | None = None,
    *,
    drain_events: bool = True,
) -> dict[str, Any]:
    """Return the unified payload consumed by the SPA card.

    Missing, malformed, or stale reporter entries surface as per-sidecar
    ``health = "down"``. A Redis connection failure returns an all-down
    degraded snapshot so the dashboard remains honest and renderable.

    ``drain_events`` controls whether the UI animation event counters are
    atomically read+reset by this call. The SSE stream is the canonical
    drain (one consumer, every 5 s); HTTP poll callers (initial mount and
    polling fallback) must pass ``drain_events=False`` so they don't race
    with SSE for the same hash and steal events from the live stream.
    """

    now = time.time()
    if client is not None:
        redis_client = client
    else:
        from api.services.redis_clients import get_redis_client

        redis_client = get_redis_client(redis_url or _redis_url(), socket_timeout=1.5)

    try:
        raw_entries = list(_mget_reporters(redis_client))
    except redis.RedisError as exc:
        _log_redis_unavailable(exc)
        return _all_down_snapshot(now, "redis_unavailable", str(exc))

    sidecars: dict[str, dict[str, Any]] = {}
    for index, name in enumerate(KNOWN_SIDECARS):
        blob = raw_entries[index] if index < len(raw_entries) else None
        sidecars[name] = _decode_reporter_entry(name, blob, now)

    _apply_local_probe_fallbacks(sidecars, now)

    sidecars["redis"] = _redis_self_snapshot(redis_client, now)

    # Drain UI animation events — see api.services.event_emitter. Failure
    # returns a zero-filled dict so the SPA can render a stable shape.
    # Non-draining callers (HTTP poll) get all-zero events without ever
    # touching Redis so they cannot steal a tick from the SSE consumer.
    if drain_events:
        from api.services.event_emitter import drain as _drain_events

        events = _drain_events(redis_client)
    else:
        from api.services.event_emitter import ROW_FIELDS as _ROW_FIELDS

        events = {field: 0 for field in _ROW_FIELDS}

    return {
        "ts": now,
        "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
        "sidecars": sidecars,
        "events": events,
    }

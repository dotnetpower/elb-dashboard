"""Measured AKS lifecycle timings used to turn the SPA start estimate into a stat.

Responsibility: Append observed durations for cluster lifecycle phases
    (`aks_start`, `aks_stop`, `openapi_deploy`) and compute a robust per-phase
    statistic (median of the most recent samples) so the dashboard can render a
    real "Last observed …" estimate instead of a hardcoded constant.
Edit boundaries: Persistence + aggregation only. No ARM/Kubernetes calls here —
    callers (lifecycle / openapi deploy tasks) pass an already-measured duration.
    The recording path must stay best-effort: a metrics write must never fail the
    lifecycle task that produced it.
Key entry points: `record_timing`, `get_timing_stats`, `PhaseStat`,
    `DEFAULT_SECONDS`, `KNOWN_PHASES`.
Risky contracts: Backend selection mirrors `auto_warmup._use_table_backend()`
    (Azure Tables only when `AZURE_TABLE_ENDPOINT` *and* `CONTAINER_APP_NAME` are
    set; local file fallback otherwise). The `/api/monitor/aks/start-stats` route
    and `StartEstimatePanel` depend on the `PhaseStat` field names and on
    `get_timing_stats` never raising.
Validation: `uv run pytest -q api/tests/test_cluster_timings.py`.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"
_LOCAL_STATE_ENV = "ELB_LOCAL_STATE_DIR"
_TABLE_NAME = "clustertimings"
_TYPE = "cluster_timing"

# Phases the SPA cares about. The constants double as the fallback estimate
# when no samples have been recorded yet (mirrors the historical hardcoded
# values that lived in web/src/components/ClusterItem/StartEstimatePanel.tsx).
DEFAULT_SECONDS: dict[str, float] = {
    "aks_start": 235.0,
    "aks_stop": 180.0,
    "openapi_deploy": 31.0,
}
KNOWN_PHASES: tuple[str, ...] = tuple(DEFAULT_SECONDS.keys())

# How many recent samples a statistic considers, and the sanity bounds for a
# single observation (anything outside is treated as a measurement glitch and
# dropped rather than poisoning the median).
_SAMPLE_LIMIT = 20
_MIN_SECONDS = 1.0
_MAX_SECONDS = 2 * 60 * 60.0  # 2 hours

# Inverse-timestamp base so the newest RowKey sorts first in Azure Tables
# (PartitionKey, RowKey) ascending order. 10**13 ms ≈ year 2286, well past any
# realistic lifetime, and keeps the key fixed-width for lexical ordering.
_INV_BASE_MS = 10**13

_ENSURED_TABLES: set[str] = set()
_ENSURED_TABLES_LOCK = threading.Lock()
_TABLE_POOLED: Any | None = None
_TABLE_POOL_LOCK = threading.Lock()

_FILE_BACKEND_LOCKS: dict[str, threading.Lock] = {}
_FILE_BACKEND_LOCKS_GUARD = threading.Lock()


@dataclass
class PhaseStat:
    """Aggregated estimate for one lifecycle phase."""

    phase: str
    seconds: float
    samples: int
    last_observed_at: str | None
    source: str  # "measured" when samples > 0, otherwise "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "seconds": round(self.seconds, 1),
            "samples": self.samples,
            "last_observed_at": self.last_observed_at,
            "source": self.source,
        }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _use_table_backend() -> bool:
    """Return True when the Azure Tables backend should be used.

    Same guard as ``api.services.auto_warmup._use_table_backend``: requires
    both ``AZURE_TABLE_ENDPOINT`` *and* ``CONTAINER_APP_NAME`` so a local
    ``az login`` identity that lacks Storage Table RBAC never crashes the
    lifecycle task — local dev silently uses the file backend.
    """
    return bool(
        os.environ.get(_TABLE_ENDPOINT_ENV) and os.environ.get("CONTAINER_APP_NAME")
    )


def record_timing(
    phase: str,
    duration_seconds: float,
    *,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> bool:
    """Append one observed duration for ``phase``. Best-effort, never raises.

    Returns ``True`` when a sample was persisted, ``False`` when it was
    dropped (unknown phase, out-of-range duration) or the write failed.
    Side effect: one row appended to the ``clustertimings`` Table (deployed)
    or to the local state JSON file (dev).
    """
    if phase not in DEFAULT_SECONDS:
        LOGGER.warning("cluster_timings: refusing to record unknown phase %r", phase)
        return False
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        return False
    if not (_MIN_SECONDS <= duration <= _MAX_SECONDS):
        LOGGER.info(
            "cluster_timings: dropping out-of-range %s sample (%.1fs)", phase, duration
        )
        return False
    sample = {
        "phase": phase,
        "duration_seconds": duration,
        "subscription_id": subscription_id,
        "resource_group": resource_group,
        "cluster_name": cluster_name,
        "recorded_at": _now_iso(),
    }
    try:
        if _use_table_backend():
            _append_table(sample)
        else:
            _append_file(sample)
        return True
    except Exception as exc:  # metrics writes are never fatal
        LOGGER.warning("cluster_timings: failed to record %s sample: %s", phase, exc)
        return False


def get_timing_stats(
    phases: tuple[str, ...] = KNOWN_PHASES,
    *,
    sample_limit: int = _SAMPLE_LIMIT,
) -> dict[str, PhaseStat]:
    """Return a :class:`PhaseStat` per requested phase. Never raises.

    Uses the median of up to ``sample_limit`` most-recent samples (robust to
    the occasional cold-start outlier). When a phase has no samples the
    statistic falls back to :data:`DEFAULT_SECONDS` with ``source="default"``.
    """
    out: dict[str, PhaseStat] = {}
    for phase in phases:
        default = DEFAULT_SECONDS.get(phase, 0.0)
        try:
            samples = _recent_samples(phase, sample_limit)
        except Exception as exc:  # read path must degrade, not 500
            LOGGER.warning("cluster_timings: read failed for %s: %s", phase, exc)
            samples = []
        if samples:
            durations = [s["duration_seconds"] for s in samples]
            last_at = max(
                (s.get("recorded_at") for s in samples if s.get("recorded_at")),
                default=None,
            )
            out[phase] = PhaseStat(
                phase=phase,
                seconds=float(statistics.median(durations)),
                samples=len(durations),
                last_observed_at=last_at,
                source="measured",
            )
        else:
            out[phase] = PhaseStat(
                phase=phase,
                seconds=float(default),
                samples=0,
                last_observed_at=None,
                source="default",
            )
    return out


# --------------------------------------------------------------------------- #
# Azure Tables backend
# --------------------------------------------------------------------------- #


def _row_key_for(now_ms: int) -> str:
    inverse = max(0, _INV_BASE_MS - now_ms)
    # Append a short random suffix so two samples in the same millisecond do not
    # collide on the same RowKey.
    return f"{inverse:013d}-{uuid.uuid4().hex[:8]}"


def _table_client() -> Any:
    """Return a process-shared pooled ``TableClient`` for the timings table."""
    global _TABLE_POOLED
    pool = _TABLE_POOLED
    if pool is not None:
        return pool
    from azure.data.tables import TableClient

    from api.services import get_credential
    from api.services.state_repo import _PooledTableClient

    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _TABLE_POOL_LOCK:
        if _TABLE_POOLED is None:
            _TABLE_POOLED = _PooledTableClient(
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _TABLE_POOLED


def _reset_table_pool() -> None:
    """Test hook + safety valve for credential reset."""
    global _TABLE_POOLED
    with _TABLE_POOL_LOCK:
        pool = _TABLE_POOLED
        _TABLE_POOLED = None
    if pool is not None:
        close = getattr(pool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: S110 - close races are not fatal
                pass


def _ensure_table() -> None:
    from azure.core.exceptions import ResourceExistsError
    from azure.data.tables import TableServiceClient

    from api.services import get_credential

    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    if endpoint in _ENSURED_TABLES:
        return
    with _ENSURED_TABLES_LOCK:
        if endpoint in _ENSURED_TABLES:
            return
        with TableServiceClient(endpoint=endpoint, credential=get_credential()) as service:
            try:
                service.create_table_if_not_exists(_TABLE_NAME)
            except AttributeError:
                try:
                    service.create_table(_TABLE_NAME)
                except ResourceExistsError:
                    pass
        _ENSURED_TABLES.add(endpoint)


def _append_table(sample: dict[str, Any]) -> None:
    _ensure_table()
    entity = {
        "PartitionKey": sample["phase"],
        "RowKey": _row_key_for(int(time.time() * 1000)),
        "type": _TYPE,
        "duration_seconds": float(sample["duration_seconds"]),
        "subscription_id": sample.get("subscription_id", ""),
        "resource_group": sample.get("resource_group", ""),
        "cluster_name": sample.get("cluster_name", ""),
        "recorded_at": sample.get("recorded_at", _now_iso()),
    }
    with _table_client() as table:
        table.create_entity(entity)


def _recent_samples_table(phase: str, limit: int) -> list[dict[str, Any]]:
    _ensure_table()
    rows: list[dict[str, Any]] = []
    with _table_client() as table:
        # Inverse RowKey ⇒ ascending order already yields newest-first.
        entities = table.query_entities(
            query_filter="PartitionKey eq @phase",
            parameters={"phase": phase},
            results_per_page=limit,
        )
        for entity in entities:
            rows.append(
                {
                    "duration_seconds": float(entity.get("duration_seconds", 0.0) or 0.0),
                    "recorded_at": str(entity.get("recorded_at") or ""),
                }
            )
            if len(rows) >= limit:
                break
    return rows


# --------------------------------------------------------------------------- #
# Local file backend (dev only)
# --------------------------------------------------------------------------- #


def _state_file() -> Path:
    default_root = Path(__file__).resolve().parents[2] / ".logs" / "local" / "state"
    root = Path(os.environ.get(_LOCAL_STATE_ENV, str(default_root)))
    return root / "cluster_timings.json"


def _file_backend_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _FILE_BACKEND_LOCKS_GUARD:
        lock = _FILE_BACKEND_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_BACKEND_LOCKS[key] = lock
    return lock


def _read_file_state() -> dict[str, list[dict[str, Any]]]:
    path = _state_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return cast(dict[str, list[dict[str, Any]]], data)


def _write_file_state(data: dict[str, list[dict[str, Any]]]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _append_file(sample: dict[str, Any]) -> None:
    path = _state_file()
    lock = _file_backend_lock(path)
    with lock:
        data = _read_file_state()
        bucket = data.get(sample["phase"])
        if not isinstance(bucket, list):
            bucket = []
        # Newest-first, capped so the dev file never grows unbounded.
        bucket.insert(
            0,
            {
                "duration_seconds": float(sample["duration_seconds"]),
                "recorded_at": sample.get("recorded_at", _now_iso()),
                "cluster_name": sample.get("cluster_name", ""),
            },
        )
        data[sample["phase"]] = bucket[: max(_SAMPLE_LIMIT, 50)]
        _write_file_state(data)


def _recent_samples_file(phase: str, limit: int) -> list[dict[str, Any]]:
    bucket = _read_file_state().get(phase)
    if not isinstance(bucket, list):
        return []
    out: list[dict[str, Any]] = []
    for item in bucket[:limit]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "duration_seconds": float(item.get("duration_seconds", 0.0) or 0.0),
                "recorded_at": str(item.get("recorded_at") or ""),
            }
        )
    return out


def _recent_samples(phase: str, limit: int) -> list[dict[str, Any]]:
    if _use_table_backend():
        return _recent_samples_table(phase, limit)
    return _recent_samples_file(phase, limit)


__all__ = [
    "DEFAULT_SECONDS",
    "KNOWN_PHASES",
    "PhaseStat",
    "get_timing_stats",
    "record_timing",
]

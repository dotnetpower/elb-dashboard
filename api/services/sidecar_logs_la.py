"""Container Apps log fallback — KQL against `ContainerAppConsoleLogs_CL`.

Responsibility: When the api runs as a Container Apps sidecar the historical
  project-local log files do not exist, so this module fetches a recent window
  of stdout/stderr for all six sidecars from the Log Analytics workspace and
  serves the existing Live Wall contract (`read_recent_lines` / `end_offset`
  / `read_lines_since`) without changing the route surface.
Edit boundaries: This module is the only place that talks to
  `azure-monitor-query`. The route and the file-tail fallback stay
  oblivious — `sidecar_logs._is_la_mode()` decides which path runs.
Key entry points: `read_recent_lines_la`, `read_lines_since_la`,
  `end_offset_la`, `la_workspace_id`.
Risky contracts:
  * `offset` in this module is **milliseconds since epoch (UTC)**, NOT a byte
    count. The route only treats it as opaque so this is safe, but never
    cross-compare an LA offset against a file byte offset.
  * One process-wide snapshot is shared across all SSE connections and all
    six sidecars to keep LA query cost bounded; the snapshot TTL must stay
    above the route's poll interval or the cache becomes useless.
  * The LA workspace receives stdout/stderr with a typical ingestion delay of
    ~30-90 seconds; lines newer than the snapshot's data horizon will simply
    show up on the next refresh.
Validation: `uv run pytest -q api/tests/test_sidecar_logs_la.py`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from api.services.sidecar_logs import (
    SIDECAR_CONTAINERS,
    LogLine,
    SidecarContainer,
    _render_log_line,
)

if TYPE_CHECKING:
    from azure.monitor.query import LogsQueryClient

LOGGER = logging.getLogger(__name__)

# How long a fetched snapshot is reused. The route polls every ~5 s; this
# upper bound ensures at most one LA query per 5 s regardless of how many
# SSE connections or browser tabs are open. Operators can override via
# `LIVE_WALL_LA_CACHE_TTL_SECONDS` if they need fresher data and accept
# the higher query cost.
_DEFAULT_CACHE_TTL_SEC = 5.0
# How far back the snapshot reaches. Live Wall renders the most recent
# ~60 lines per tile; 10 minutes of history is enough for any reasonable
# burst while keeping the per-query data scan small.
_DEFAULT_LOOKBACK_MINUTES = 10
# Hard cap on how many rows the snapshot keeps per sidecar; protects
# against a misbehaving sidecar flooding LA and OOM-ing the api worker.
_MAX_LINES_PER_CONTAINER = 500

# Single underscore — re-exported via tests; keeping the API surface tiny.
_CACHE_TTL_SEC = float(
    os.environ.get("LIVE_WALL_LA_CACHE_TTL_SECONDS", _DEFAULT_CACHE_TTL_SEC)
)
_LOOKBACK = timedelta(
    minutes=int(os.environ.get("LIVE_WALL_LA_LOOKBACK_MINUTES", _DEFAULT_LOOKBACK_MINUTES))
)


# Guards the shared snapshot refresh. NON-reentrant — never acquire it
# transitively (e.g. via `_get_client`) while it is already held, or the
# refreshing thread self-deadlocks.
_lock = threading.Lock()
# Separate, dedicated lock for lazy client construction. Kept distinct from
# `_lock` precisely because `_fetch_snapshot` calls `_get_client` while it
# already holds `_lock`; a shared non-reentrant lock would deadlock the very
# first Live Wall log fetch (HTTP 200 stream stays open but never emits a
# line, with no exception logged).
_client_lock = threading.Lock()
_client: LogsQueryClient | None = None
_snapshot: dict[SidecarContainer, list[tuple[int, LogLine]]] = {}
_snapshot_fetched_at_monotonic: float = 0.0
_snapshot_error_count = 0


def la_workspace_id() -> str | None:
    """Return the configured workspace customerId GUID or `None`."""
    value = os.environ.get("LOG_ANALYTICS_WORKSPACE_ID", "").strip()
    return value or None


def is_enabled() -> bool:
    """True iff the LA fallback should be used for log tailing."""
    return la_workspace_id() is not None


def read_recent_lines_la(container: SidecarContainer, *, tail: int) -> list[LogLine]:
    """Return the last `tail` log lines for one sidecar from the LA snapshot."""
    snap = _ensure_snapshot()
    rows = snap.get(container, [])
    if tail <= 0:
        return []
    return [line for _ts, line in rows[-tail:]]


def read_lines_since_la(
    container: SidecarContainer, offset_ms: int
) -> tuple[list[LogLine], int]:
    """Return new lines for one sidecar since `offset_ms` (UTC epoch ms).

    The returned offset is the timestamp of the newest line returned, or
    the caller's offset when no new lines are available, so the SSE loop
    can keep advancing without re-fetching the same rows.
    """
    snap = _ensure_snapshot()
    rows = snap.get(container, [])
    if not rows:
        return [], offset_ms
    fresh = [(ts, line) for ts, line in rows if ts > offset_ms]
    if not fresh:
        return [], offset_ms
    next_offset = max(ts for ts, _ in fresh)
    return [line for _, line in fresh], next_offset


def end_offset_la(container: SidecarContainer) -> int:
    """Return the newest known timestamp for `container` as epoch ms.

    Falls back to `now()` when the snapshot has no lines for that sidecar
    so a tile that just connected starts streaming forward instead of
    re-emitting whatever historical lines happened to be cached.
    """
    snap = _ensure_snapshot()
    rows = snap.get(container, [])
    if rows:
        return rows[-1][0]
    return _now_ms()


def _ensure_snapshot() -> dict[SidecarContainer, list[tuple[int, LogLine]]]:
    """Refresh the shared snapshot if it is older than the cache TTL."""
    global _snapshot, _snapshot_fetched_at_monotonic, _snapshot_error_count
    now = time.monotonic()
    if _snapshot and now - _snapshot_fetched_at_monotonic < _CACHE_TTL_SEC:
        return _snapshot
    with _lock:
        # Re-check under the lock — another thread may have refreshed while
        # we were waiting.
        now = time.monotonic()
        if _snapshot and now - _snapshot_fetched_at_monotonic < _CACHE_TTL_SEC:
            return _snapshot
        try:
            fetched = _fetch_snapshot()
            _snapshot = fetched
            _snapshot_fetched_at_monotonic = now
            _snapshot_error_count = 0
            return _snapshot
        except Exception as exc:
            # On failure return the previous snapshot rather than wiping the
            # cache — the tile will keep showing the last data and the next
            # tick will retry. Log at WARNING the first few times so the
            # operator notices; demote to DEBUG after that to avoid log
            # spam if LA is intermittently unavailable.
            level = (
                logging.WARNING if _snapshot_error_count < 3 else logging.DEBUG
            )
            LOGGER.log(level, "live-wall LA snapshot refresh failed: %s", exc)
            _snapshot_error_count += 1
            return _snapshot


def _fetch_snapshot() -> dict[SidecarContainer, list[tuple[int, LogLine]]]:
    """Run one KQL query covering all six sidecars and group by container."""
    client = _get_client()
    workspace = la_workspace_id()
    if workspace is None:
        return {}
    container_list = ", ".join(f"'{c}'" for c in SIDECAR_CONTAINERS)
    # NOTE: ContainerAppConsoleLogs_CL is the user-app stdout/stderr
    # table. ContainerName_s carries the sidecar name set in the Container
    # App template; Log_s is the raw line. We project a stable subset so
    # schema drift in additional columns cannot break the parser. The
    # per-container cap is applied in Python after grouping because KQL's
    # `top-nested` syntax for the same effect is fragile and harder to
    # reason about than a single windowed scan.
    query = (
        "ContainerAppConsoleLogs_CL"
        f" | where ContainerName_s in ({container_list})"
        " | project TimeGenerated, ContainerName_s, Log_s"
        " | order by TimeGenerated asc"
    )
    response = client.query_workspace(
        workspace_id=workspace,
        query=query,
        timespan=_LOOKBACK,
    )
    return _parse_query_response(response)


def _parse_query_response(response: object) -> dict[SidecarContainer, list[tuple[int, LogLine]]]:
    """Convert a `LogsQueryResult` into the per-container snapshot dict."""
    out: dict[SidecarContainer, list[tuple[int, LogLine]]] = {
        c: [] for c in SIDECAR_CONTAINERS
    }
    # azure.monitor.query returns either LogsQueryResult.tables or
    # .partial_data; we treat partial the same as success so the Live Wall
    # never goes blank during a workspace hiccup.
    tables = getattr(response, "tables", None) or getattr(response, "partial_data", []) or []
    for table in tables:
        columns: list[str] = list(getattr(table, "columns", []))
        try:
            ts_idx = columns.index("TimeGenerated")
            name_idx = columns.index("ContainerName_s")
            log_idx = columns.index("Log_s")
        except ValueError:
            continue
        for row in getattr(table, "rows", []) or []:
            ts_val = row[ts_idx]
            name_val = row[name_idx]
            log_val = row[log_idx]
            if not isinstance(name_val, str):
                continue
            if name_val not in SIDECAR_CONTAINERS:
                continue
            container = _coerce_container(name_val)
            ts_ms, ts_iso = _normalise_timestamp(ts_val)
            if ts_ms is None:
                continue
            line = _render_log_line(str(log_val or ""), ts_iso)
            out[container].append((ts_ms, line))
    # Cap each container at _MAX_LINES_PER_CONTAINER (keeping the newest).
    for container in out:
        rows = out[container]
        if len(rows) > _MAX_LINES_PER_CONTAINER:
            out[container] = rows[-_MAX_LINES_PER_CONTAINER:]
    return out


def _normalise_timestamp(value: object) -> tuple[int | None, str]:
    """Return (epoch_ms, iso8601_z_string) for a datetime-ish value."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None, ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    else:
        return None, ""
    epoch_ms = int(dt.astimezone(UTC).timestamp() * 1000)
    iso = dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return epoch_ms, iso


def _coerce_container(value: str) -> SidecarContainer:
    # `SidecarContainer` is a Literal alias — narrow without a cast helper.
    return value  # type: ignore[return-value]


_ContainerKey = Literal["frontend", "api", "worker", "beat", "redis", "terminal"]
_ = _ContainerKey  # silence "unused" — kept so the typing surface matches sidecar_logs


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_client() -> LogsQueryClient:
    """Return a process-wide `LogsQueryClient` constructed lazily.

    Lazy so importing this module does not require the credential chain
    to resolve (tests + local-dev would otherwise fail at import time).
    """
    global _client
    if _client is not None:
        return _client
    # Dedicated `_client_lock` (not `_lock`): `_fetch_snapshot` already holds
    # `_lock` when it calls this, and `_lock` is non-reentrant.
    with _client_lock:
        if _client is not None:
            return _client
        from azure.monitor.query import LogsQueryClient

        from api.services import get_credential

        _client = LogsQueryClient(get_credential())
        return _client


def reset_for_tests() -> None:
    """Reset cache + client. Test-only — never call from production code."""
    global _client, _snapshot, _snapshot_fetched_at_monotonic, _snapshot_error_count
    _client = None
    _snapshot = {}
    _snapshot_fetched_at_monotonic = 0.0
    _snapshot_error_count = 0

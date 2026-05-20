"""Redis Stream event bus for live BLAST job logs.

Responsibility: Redis Stream event bus for live BLAST job logs
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_now_iso`, `_safe_stream_key`, `_redis_client`, `publish_job_log_event`,
`read_job_log_events`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

_STREAM_MAXLEN = 5_000
_LINE_MAX_CHARS = 4_000
_SAFE_STREAM_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _safe_stream_key(job_id: str) -> str:
    safe = _SAFE_STREAM_RE.sub("_", str(job_id or "").strip())[:160]
    if not safe:
        safe = "unknown"
    return f"joblogs:{safe}"


def _redis_client() -> Any:
    import redis

    broker_url = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
    return redis.Redis.from_url(broker_url)


def _event_payload(
    *,
    job_id: str,
    source: str,
    phase: str,
    line: str,
    stream: str = "stdout",
    pod: str = "",
    container: str = "",
    event_id: str = "",
) -> dict[str, Any]:
    safe_line = sanitise(str(line or ""))[:_LINE_MAX_CHARS]
    return {
        "id": event_id,
        "schema_version": 1,
        "job_id": str(job_id),
        "source": str(source or "unknown")[:40],
        "phase": str(phase or "running")[:80],
        "stream": str(stream or "stdout")[:20],
        "pod": str(pod or "")[:253],
        "container": str(container or "")[:253],
        "line": safe_line,
        "ts": _now_iso(),
    }


def publish_job_log_event(
    job_id: str,
    *,
    source: str,
    phase: str,
    line: str,
    stream: str = "stdout",
    pod: str = "",
    container: str = "",
) -> None:
    """Best-effort publish of a sanitised live log event."""

    if not job_id or not line:
        return
    payload = _event_payload(
        job_id=job_id,
        source=source,
        phase=phase,
        line=line,
        stream=stream,
        pod=pod,
        container=container,
    )
    try:
        _redis_client().xadd(
            _safe_stream_key(job_id),
            {"event": json.dumps(payload, separators=(",", ":"))},
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:
        LOGGER.debug("job log publish skipped job_id=%s: %s", job_id, type(exc).__name__)


def read_job_log_events(
    job_id: str,
    *,
    last_id: str = "0-0",
    block_ms: int = 5_000,
    count: int = 100,
) -> list[dict[str, Any]]:
    """Read live events from the Redis Stream after ``last_id``."""

    try:
        rows = _redis_client().xread(
            {_safe_stream_key(job_id): last_id},
            count=max(1, min(count, 500)),
            block=max(0, min(block_ms, 30_000)),
        )
    except Exception as exc:
        LOGGER.debug("job log read skipped job_id=%s: %s", job_id, type(exc).__name__)
        return []

    events: list[dict[str, Any]] = []
    for _stream_name, entries in rows or []:
        for raw_id, fields in entries:
            event_id = (
                raw_id.decode("utf-8", errors="replace")
                if isinstance(raw_id, bytes)
                else str(raw_id)
            )
            raw_event = fields.get(b"event") if isinstance(fields, dict) else None
            if raw_event is None and isinstance(fields, dict):
                raw_event = fields.get("event")
            if isinstance(raw_event, bytes):
                raw_event = raw_event.decode("utf-8", errors="replace")
            try:
                payload = json.loads(str(raw_event or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            payload["id"] = event_id
            events.append(payload)
    return events


__all__ = ["publish_job_log_event", "read_job_log_events"]

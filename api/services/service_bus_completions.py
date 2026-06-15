"""Cross-sidecar store of completion-topic events observed by the demo consumer.

A small, capped, best-effort ring of the most recent ``blast.transition`` events
the optional external-completion consumer (running on the worker sidecar) pulled
off the completion topic. It lives in the shared OPS Redis so the api sidecar's
Playground route can render what the worker observed (the in-process memory of
one sidecar is invisible to the other — Redis is the only shared, cheap channel
already wired into every sidecar).

Responsibility: Persist/read a bounded list of observed completion events only.
    No Service Bus SDK, no receive loop, no HTTP shaping — the consumer writes,
    the route reads.
Edit boundaries: stdlib + the shared Redis client helper only. Every operation
    is best-effort: a Redis outage degrades to "no observations" and never
    raises into the consumer loop or the route.
Key entry points: ``record_completion``, ``list_recent``, ``clear``.
Risky contracts: The list is capped (``_MAX_ENTRIES``) via ``LTRIM`` on every
    write and carries a TTL so a quiet deployment does not keep stale rows
    forever. Events are stored newest-first (``LPUSH``); ``list_recent`` returns
    newest-first. A malformed stored entry is skipped, never raised.
Validation: ``uv run pytest -q api/tests/test_service_bus_completions.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

# Redis key (OPS db) for the capped observation ring.
_KEY = "elb:sb:observed-completions"
# Keep only the most recent N observations — bounded memory regardless of
# throughput (the demo consumer is an observability aid, not an audit log; the
# durable per-job timeline lives in the jobhistory table via message_trace).
_MAX_ENTRIES = 200
# Expire the whole ring after this idle window so a deployment that stops
# observing does not surface stale rows indefinitely.
_TTL_SECONDS = 24 * 3600


def _client() -> Any | None:
    """Return the shared OPS Redis client, or ``None`` when unavailable."""
    try:
        from api.services.redis_clients import get_ops_redis_client

        return get_ops_redis_client()
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("completions observer redis unavailable: %s", type(exc).__name__)
        return None


def record_completion(event: dict[str, Any]) -> None:
    """Record one observed completion event (best-effort, newest-first).

    ``event`` is the parsed ``blast.transition`` JSON body. Only a compact,
    non-sensitive projection is stored (the event already carries pointers, not
    result bytes — charter §9). Never raises.
    """
    client = _client()
    if client is None:
        return
    entry = {
        "event_id": str(event.get("event_id") or ""),
        "external_correlation_id": str(event.get("external_correlation_id") or ""),
        "request_id": str(event.get("request_id") or ""),
        "openapi_job_id": str(event.get("openapi_job_id") or ""),
        "status": str(event.get("status") or ""),
        "ts": str(event.get("ts") or ""),
        "observed_at": _now_iso(),
    }
    try:
        pipe = client.pipeline()
        pipe.lpush(_KEY, json.dumps(entry, default=str))
        pipe.ltrim(_KEY, 0, _MAX_ENTRIES - 1)
        pipe.expire(_KEY, _TTL_SECONDS)
        pipe.execute()
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.debug("completions observer record failed: %s", type(exc).__name__)


def list_recent(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent observed completions, newest-first.

    Best-effort: returns an empty list when Redis is unavailable. A malformed
    stored entry is skipped rather than aborting the whole read.
    """
    client = _client()
    if client is None:
        return []
    bounded = max(1, min(int(limit), _MAX_ENTRIES))
    try:
        raw = client.lrange(_KEY, 0, bounded - 1)
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.debug("completions observer read failed: %s", type(exc).__name__)
        return []
    out: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    for item in raw or []:
        try:
            text = item.decode() if isinstance(item, bytes | bytearray) else str(item)
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                continue
        except (ValueError, TypeError):
            continue
        # At-least-once delivery means the same completion event can be observed
        # (and stored) more than once. De-dup by event_id so the UI renders a
        # stable, unique list (and never collides on a React key). Entries with
        # no event_id are kept as-is (best-effort — they carry no dedup key).
        event_id = str(parsed.get("event_id") or "")
        if event_id:
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
        out.append(parsed)
    return out


def clear() -> None:
    """Drop all observations (test/operator hook). Best-effort."""
    client = _client()
    if client is None:
        return
    try:
        client.delete(_KEY)
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.debug("completions observer clear failed: %s", type(exc).__name__)


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")

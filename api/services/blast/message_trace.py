"""BLAST message lifecycle trace — stage timeline over the jobhistory table.

A focused recorder/deriver for the Service-Bus → consumer → execution →
result lifecycle so the dashboard can answer "where is this message right now,
and how long did each hop take" from a single jobstate row. Stages are stored as
``mf.<stage>`` events in the existing ``jobhistory`` table (no new schema), one
event per stage, carrying the real stage timestamp in its payload.

Responsibility: Record a message-flow lifecycle stage onto a job's history and
    derive an ordered, deduplicated stage timeline (+ dwell/latency metrics)
    back out of raw history rows.
Edit boundaries: Pure trace recording/derivation only. No Service Bus calls, no
    OpenAPI calls, no jobstate-row writes — the caller owns those. Persistence is
    delegated to ``JobStateRepository.append_history`` (best-effort, never
    raises). Keep the ``mf.`` event-name prefix stable; consumers filter on it.
Key entry points: ``record_stage``, ``derive_trace``, ``MESSAGE_TRACE_STAGES``.
Risky contracts: ``record_stage`` MUST be best-effort (a trace write must never
    fail the parent drain/publish task). ``derive_trace`` keeps the FIRST
    occurrence of each stage (stages are monotonic; a re-delivered duplicate
    must not move a timestamp). Metric math tolerates missing/out-of-order
    stages and never raises.
Validation: ``uv run pytest -q api/tests/test_message_trace.py``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)

# Ordered lifecycle stages. The order is the canonical render/sort order and the
# basis for the derived dwell/latency metrics below.
MESSAGE_TRACE_STAGES: tuple[str, ...] = (
    "enqueued",  # message landed on the request queue (SB enqueuedTimeUtc)
    "received",  # consumer pulled the message off the queue
    "row_created",  # durable jobstate row written by the consumer
    "routed",  # execution backend chosen (openapi | local)
    "submitted",  # backend accepted the job (openapi_job_id known)
    "running",  # execution started
    "succeeded",  # terminal OK
    "failed",  # terminal not-OK
    "completion_published",  # result/transition delivered to the completion topic
    "dead_letter",  # message could never succeed; dead-lettered
)
_STAGE_INDEX = {stage: i for i, stage in enumerate(MESSAGE_TRACE_STAGES)}

_EVENT_PREFIX = "mf."


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def record_stage(
    repo: Any,
    job_id: str,
    stage: str,
    *,
    stage_ts: str | datetime | None = None,
    **detail: Any,
) -> None:
    """Append one ``mf.<stage>`` history event. Best-effort — never raises.

    ``stage_ts`` is the *real* time the stage happened (e.g. the SB
    ``enqueued_time_utc`` for ``enqueued``), which can predate this write; it is
    stored in the payload so ``derive_trace`` reports the true hop time, not the
    write time. When omitted the current time is used.
    """
    if not job_id or stage not in _STAGE_INDEX:
        LOGGER.debug("message_trace: ignoring unknown stage=%r job_id=%r", stage, job_id)
        return
    if isinstance(stage_ts, datetime):
        ts_value = stage_ts.astimezone(UTC).isoformat(timespec="seconds")
    elif isinstance(stage_ts, str) and stage_ts.strip():
        ts_value = stage_ts.strip()
    else:
        ts_value = _now_iso()
    payload: dict[str, Any] = {"stage": stage, "stage_ts": ts_value}
    for key, value in detail.items():
        if value is not None:
            payload[key] = value
    try:
        repo.append_history(job_id, f"{_EVENT_PREFIX}{stage}", payload)
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.debug("message_trace record failed job_id=%s stage=%s: %s", job_id, stage, stage)
        del exc


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _stage_from_row(row: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(stage, stage_ts_iso)`` for an ``mf.*`` history row, else None."""
    event = str(row.get("event") or "")
    if not event.startswith(_EVENT_PREFIX):
        return None
    stage = event[len(_EVENT_PREFIX) :]
    if stage not in _STAGE_INDEX:
        return None
    stage_ts: str | None = None
    raw_payload = row.get("payload_json")
    if isinstance(raw_payload, str) and raw_payload:
        try:
            parsed = json.loads(raw_payload)
            if isinstance(parsed, dict):
                cand = parsed.get("stage_ts")
                if isinstance(cand, str) and cand.strip():
                    stage_ts = cand.strip()
        except (ValueError, TypeError):
            stage_ts = None
    return stage, stage_ts or str(row.get("ts") or "")


def derive_trace(history_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive an ordered stage timeline + dwell/latency metrics from history.

    Keeps the FIRST occurrence of each stage (stages are monotonic; an
    at-least-once re-delivery must not rewrite an earlier timestamp). Returns a
    stable shape even when stages are missing so the SPA can render partial
    progress without special-casing. Never raises.
    """
    first_ts: dict[str, str] = {}
    for row in history_rows or []:
        parsed = _stage_from_row(row if isinstance(row, dict) else {})
        if parsed is None:
            continue
        stage, ts = parsed
        if stage not in first_ts and ts:
            first_ts[stage] = ts

    stages = [
        {"stage": stage, "ts": first_ts[stage]}
        for stage in MESSAGE_TRACE_STAGES
        if stage in first_ts
    ]

    def _delta_ms(a: str, b: str) -> int | None:
        ta, tb = _parse_iso(first_ts.get(a)), _parse_iso(first_ts.get(b))
        if ta is None or tb is None:
            return None
        delta = (tb - ta).total_seconds() * 1000.0
        return int(delta) if delta >= 0 else None

    metrics = {
        "queue_dwell_ms": _delta_ms("enqueued", "received"),
        "submit_latency_ms": _delta_ms("received", "submitted"),
        "e2e_ms": _delta_ms("enqueued", "completion_published"),
    }

    terminal = None
    for stage in ("succeeded", "failed", "dead_letter"):
        if stage in first_ts:
            terminal = stage
            break

    return {
        "stages": stages,
        "metrics": metrics,
        "terminal_stage": terminal,
        "last_stage": stages[-1]["stage"] if stages else None,
    }

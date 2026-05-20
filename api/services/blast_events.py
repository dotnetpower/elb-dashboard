"""Canonical BLAST job event mapping."""

from __future__ import annotations

import json
from typing import Any


def canonical_job_event(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    if payload is None and row.get("payload_json"):
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            payload = {"raw": str(row.get("payload_json") or "")[:500]}
    if not isinstance(payload, dict):
        payload = {}
    event = str(row.get("event") or "event")
    phase = str(payload.get("phase") or payload.get("status") or event)
    return {
        "id": str(row.get("RowKey") or row.get("id") or ""),
        "job_id": str(row.get("PartitionKey") or row.get("job_id") or ""),
        "event": event,
        "phase": phase,
        "status": str(payload.get("status") or ""),
        "timestamp": str(row.get("ts") or row.get("timestamp") or ""),
        "payload": payload,
    }


def canonical_job_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = [canonical_job_event(row) for row in rows]
    events.sort(key=lambda event: (event["timestamp"], event["id"]))
    return events

"""Pure helpers for parsing Kubernetes timestamps and timespans.

Used by [api/services/k8s/monitoring.py](./monitoring.py) when summarising
pod / container start and completion times into job-level progress payloads.

Responsibility: Parse Kubernetes RFC3339 / ISO-8601 timestamps and compute
ordered min/max/span payloads from lists of such timestamps.
Edit boundaries: Pure helpers only. No Kubernetes / Azure SDK imports, no
network calls, no caching.
Key entry points: `parse_k8s_timestamp`, `parseable_k8s_timestamps`,
`min_k8s_timestamp`, `max_k8s_timestamp`, `k8s_timestamp_span_payload`.
Risky contracts: `parseable_k8s_timestamps` swallows `ValueError` for
unparseable inputs and logs them at DEBUG; callers rely on that
permissive behaviour to render UI even when a pod reports a malformed
timestamp.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)


def parse_k8s_timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parseable_k8s_timestamps(values: list[str]) -> list[tuple[str, datetime]]:
    parsed: list[tuple[str, datetime]] = []
    for value in values:
        try:
            parsed.append((value, parse_k8s_timestamp(value)))
        except ValueError:
            LOGGER.debug("ignoring unparseable Kubernetes timestamp: %r", value)
    return parsed


def min_k8s_timestamp(values: list[str]) -> str | None:
    parsed = parseable_k8s_timestamps(values)
    if not parsed:
        return None
    return min(parsed, key=lambda item: item[1])[0]


def max_k8s_timestamp(values: list[str]) -> str | None:
    parsed = parseable_k8s_timestamps(values)
    if not parsed:
        return None
    return max(parsed, key=lambda item: item[1])[0]


def k8s_timestamp_span_payload(
    prefix: str,
    started_values: list[str],
    completed_values: list[str],
) -> dict[str, Any]:
    started = parseable_k8s_timestamps(started_values)
    completed = parseable_k8s_timestamps(completed_values)
    if not started or not completed:
        return {}
    start_value, start_time = min(started, key=lambda item: item[1])
    completed_value, completed_time = max(completed, key=lambda item: item[1])
    payload: dict[str, Any] = {
        f"{prefix}_started_at": start_value,
        f"{prefix}_completed_at": completed_value,
    }
    if completed_time >= start_time:
        payload[f"{prefix}_duration_ms"] = int((completed_time - start_time).total_seconds() * 1000)
    return payload


__all__ = (
    "k8s_timestamp_span_payload",
    "max_k8s_timestamp",
    "min_k8s_timestamp",
    "parse_k8s_timestamp",
    "parseable_k8s_timestamps",
)

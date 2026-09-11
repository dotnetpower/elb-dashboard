"""Build stable, user-facing BLAST timing breakdowns.

Responsibility: Derive immutable queue, processing, result-ready, delivery, and execution-phase
  timing from one projected job plus its optional message trace.
Edit boundaries: Pure transformation only; no Storage, Kubernetes, Redis, route, or task calls.
Key entry points: `build_job_timing`
Risky contracts: Never derive terminal duration from mutable `updated_at`; missing evidence stays
  null, and negative or out-of-order values are rejected instead of clamped.
Validation: `uv run pytest -q api/tests/test_blast_timing.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

_TERMINAL_STATUSES = frozenset(
    {"completed", "succeeded", "success", "failed", "cancelled", "canceled"}
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _seconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 2_147_483_647:
        return None
    return int(value)


def _milliseconds_as_seconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value / 1000)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z") if value else None


def _first_seconds(*values: object) -> int | None:
    for value in values:
        parsed = _seconds(value)
        if parsed is not None:
            return parsed
    return None


def _first_timestamp(*values: object) -> datetime | None:
    for value in values:
        parsed = _timestamp(value)
        if parsed is not None:
            return parsed
    return None


def _trace_stage_timestamp(trace: Mapping[str, Any], stage_name: str) -> datetime | None:
    stages = trace.get("stages")
    if not isinstance(stages, list):
        return None
    for stage in stages:
        item = _mapping(stage)
        if item.get("stage") == stage_name:
            return _timestamp(item.get("ts"))
    return None


def _duration_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None or end < start:
        return None
    return int((end - start).total_seconds())


def _phase_seconds(
    execution_timing: Mapping[str, Any],
    phase: str,
    *,
    fallback_ms: object = None,
) -> int | None:
    direct = _first_seconds(execution_timing.get(f"{phase}_seconds"))
    if direct is not None:
        return direct
    started = _timestamp(execution_timing.get(f"{phase}_started_at"))
    completed = _timestamp(execution_timing.get(f"{phase}_completed_at"))
    from_timestamps = _duration_between(started, completed)
    return from_timestamps if from_timestamps is not None else _milliseconds_as_seconds(fallback_ms)


def build_job_timing(
    job: Mapping[str, Any],
    message_trace: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable additive timing contract for one projected BLAST job."""

    payload = _mapping(job.get("payload"))
    external = _mapping(payload.get("external"))
    trace = _mapping(message_trace)
    trace_metrics = _mapping(trace.get("metrics"))
    custom_status = _mapping(job.get("custom_status"))
    steps = _mapping(custom_status.get("steps"))
    running = _mapping(steps.get("running"))
    k8s = _mapping(running.get("k8s"))
    execution_timing = _mapping(job.get("execution_timing") or external.get("execution_timing"))
    status_value = str(job.get("status") or job.get("phase") or "").strip().casefold()
    has_explicit_terminal_clock = any(
        _timestamp(value) is not None
        for value in (
            job.get("result_ready_at"),
            job.get("completed_at"),
            job.get("failed_at"),
            external.get("result_ready_at"),
            external.get("completed_at"),
            external.get("failed_at"),
        )
    )
    is_terminal = status_value in _TERMINAL_STATUSES or has_explicit_terminal_clock

    execution_queue_seconds = _first_seconds(
        job.get("queue_wait_seconds"), external.get("queue_wait_seconds")
    )
    processing_seconds = _first_seconds(job.get("run_seconds"), external.get("run_seconds"))
    execution_elapsed_seconds = _first_seconds(
        job.get("elapsed_seconds"), external.get("elapsed_seconds")
    )
    service_bus_queue_seconds = _milliseconds_as_seconds(trace_metrics.get("queue_dwell_ms"))
    submit_seconds = _milliseconds_as_seconds(trace_metrics.get("submit_latency_ms"))

    if service_bus_queue_seconds is None:
        service_bus_queue_seconds = _duration_between(
            _first_timestamp(external.get("message_enqueued_at")),
            _first_timestamp(external.get("message_received_at"), job.get("created_at")),
        )
    if submit_seconds is None:
        submit_seconds = _duration_between(
            _first_timestamp(external.get("message_received_at"), job.get("created_at")),
            _first_timestamp(external.get("submitted_at")),
        )

    total_queue_seconds = None
    queue_parts = [service_bus_queue_seconds, execution_queue_seconds]
    if any(part is not None for part in queue_parts):
        total_queue_seconds = sum(part or 0 for part in queue_parts)

    unattributed_seconds = None
    execution_breakdown_consistent = True
    if (
        execution_elapsed_seconds is not None
        and execution_queue_seconds is not None
        and processing_seconds is not None
    ):
        difference = execution_elapsed_seconds - execution_queue_seconds - processing_seconds
        if difference >= 0:
            unattributed_seconds = difference
        elif difference >= -2:
            unattributed_seconds = 0
        else:
            execution_breakdown_consistent = False

    is_service_bus = str(job.get("submission_source") or "") == "servicebus"
    time_to_result_seconds = (
        execution_elapsed_seconds if is_terminal and not is_service_bus else None
    )
    if (
        is_terminal
        and service_bus_queue_seconds is not None
        and submit_seconds is not None
        and execution_elapsed_seconds is not None
    ):
        time_to_result_seconds = (
            service_bus_queue_seconds + submit_seconds + execution_elapsed_seconds
        )
    queue_complete = service_bus_queue_seconds is not None if is_service_bus else True

    started_at = _first_timestamp(job.get("started_at"), external.get("started_at"))
    result_ready_at = _first_timestamp(
        job.get("result_ready_at"),
        job.get("completed_at"),
        external.get("result_ready_at"),
        external.get("completed_at"),
    )
    if (
        is_terminal
        and result_ready_at is None
        and started_at is not None
        and processing_seconds is not None
    ):
        result_ready_at = started_at + timedelta(seconds=processing_seconds)
    completion_published_at = _first_timestamp(
        job.get("completion_published_at"),
        external.get("completion_published_at"),
    ) or _trace_stage_timestamp(trace, "completion_published")
    status_delivery_seconds = _duration_between(result_ready_at, completion_published_at)

    return {
        "schema_version": 1,
        "result_ready_at": _iso(result_ready_at),
        "completion_published_at": _iso(completion_published_at),
        "time_to_result_seconds": time_to_result_seconds,
        "total_queue_seconds": total_queue_seconds,
        "service_bus_queue_seconds": service_bus_queue_seconds,
        "submit_seconds": submit_seconds,
        "execution_queue_seconds": execution_queue_seconds,
        "processing_seconds": processing_seconds,
        "execution_elapsed_seconds": execution_elapsed_seconds,
        "status_delivery_seconds": status_delivery_seconds,
        "unattributed_seconds": unattributed_seconds,
        "queue_complete": queue_complete,
        "breakdown_complete": bool(
            time_to_result_seconds is not None
            and processing_seconds is not None
            and total_queue_seconds is not None
            and queue_complete
            and (not is_service_bus or submit_seconds is not None)
            and execution_breakdown_consistent
            and unattributed_seconds is not None
        ),
        "phases": {
            "orchestration_seconds": _phase_seconds(execution_timing, "orchestration"),
            "k8s_setup_seconds": _phase_seconds(execution_timing, "k8s_setup"),
            "blast_seconds": _phase_seconds(
                execution_timing,
                "blast",
                fallback_ms=k8s.get("blast_container_duration_ms"),
            ),
            "export_seconds": _phase_seconds(
                execution_timing,
                "export",
                fallback_ms=k8s.get("results_export_container_duration_ms"),
            ),
            "finalizer_seconds": _phase_seconds(execution_timing, "finalizer"),
        },
    }

"""Structured, payload-free telemetry for Service Bus request lifecycles.

Responsibility: Emit bounded `servicebus_request` feature events for enqueue and
    drain decisions without accepting or recording request bodies.
Edit boundaries: Telemetry shaping only; no Service Bus SDK calls, persistence,
    queue settlement, or control-flow decisions.
Key entry points: `record_service_bus_request_event`.
Risky contracts: The explicit scalar-only signature is the data-loss boundary:
    query FASTA, options, credentials, and raw message bodies must never be
    accepted. Emission is best-effort and must never affect queue processing.
Validation: `uv run pytest -q api/tests/test_service_bus_observability.py`.
"""

from __future__ import annotations

from api.services.feature_events import record_feature_event


def _bounded(value: str, limit: int) -> str:
    return str(value or "").strip()[:limit]


def record_service_bus_request_event(
    stage: str,
    *,
    correlation_id: str = "",
    request_id: str = "",
    message_id: str = "",
    queue: str = "",
    openapi_job_id: str = "",
    program: str = "",
    database: str = "",
    taxid: int | None = None,
    is_inclusive: bool | None = None,
    action: str = "",
    error_code: str = "",
    delivery_count: int | None = None,
    sequence_number: int | None = None,
    ack_published: bool | None = None,
) -> None:
    """Emit one searchable Service Bus request event without payload content."""
    try:
        record_feature_event(
            "servicebus_request",
            status=_bounded(stage, 64),
            stage=_bounded(stage, 64),
            correlation_id=_bounded(correlation_id, 256) or None,
            request_id=_bounded(request_id, 256) or None,
            message_id=_bounded(message_id, 256) or None,
            queue=_bounded(queue, 260) or None,
            openapi_job_id=_bounded(openapi_job_id, 256) or None,
            program=_bounded(program, 32) or None,
            database=_bounded(database, 256) or None,
            taxid=taxid,
            is_inclusive=is_inclusive,
            action=_bounded(action, 32) or None,
            error_code=_bounded(error_code, 128) or None,
            delivery_count=delivery_count,
            sequence_number=sequence_number,
            ack_published=ack_published,
        )
    except Exception:
        return

"""Structured, payload-free telemetry for Service Bus request lifecycles.

Responsibility: Emit bounded `servicebus_request` lifecycle events and periodic
    `servicebus_health` queue/outbox events without accepting request bodies.
Edit boundaries: Telemetry shaping only; no Service Bus SDK calls, persistence,
    queue settlement, or control-flow decisions.
Key entry points: `record_service_bus_request_event`,
    `record_service_bus_health_event`.
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


def record_service_bus_health_event(
    *,
    status: str,
    warning_codes: str,
    queue_counts_available: bool,
    queue_counts_error: str,
    queue_active: int | None,
    queue_scheduled: int | None,
    queue_dead_letter: int | None,
    queue_total: int | None,
    completion_configured: bool,
    completion_kind: str,
    completion_accessible: bool | None,
    completion_error: str,
    completion_subscription_count: int | None,
    completion_active: int | None,
    completion_dead_letter: int | None,
    outbox_available: bool,
    outbox_error: str,
    outbox_pending: int | None,
    outbox_pending_truncated: bool,
    outbox_oldest_age_seconds: int | None,
    outbox_last_attempt_at: str,
    outbox_last_success_at: str,
    outbox_last_error_at: str,
    outbox_last_scanned: int,
    outbox_last_delivered: int,
    outbox_last_errors: int,
    admission_available: bool,
    admission_allowed: bool | None,
    admission_reason: str,
    resident_consumer_enabled: bool,
    drain_concurrency: int,
    request_policy_available: bool = False,
    request_policy_error: str = "",
    request_ttl_seconds: int | None = None,
    request_dead_letter_on_expiration: bool | None = None,
    request_max_delivery_count: int | None = None,
    completion_policy_available: bool = False,
    completion_min_ttl_seconds: int | None = None,
    completion_dead_letter_on_expiration: bool | None = None,
    completion_max_delivery_count: int | None = None,
    admission_target_node_count: int = 0,
    admission_ready_node_count: int = 0,
    admission_warmup_job_count: int = 0,
    admission_failed_warmup_job_count: int = 0,
    outbox_deferred: int | None = None,
    outbox_poison: int | None = None,
    producer_request_ttl_seconds: int = 0,
) -> None:
    """Emit one bounded operational snapshot with no message-level fields."""
    try:
        record_feature_event(
            "servicebus_health",
            status=_bounded(status, 16),
            warning_codes=_bounded(warning_codes, 512) or None,
            queue_counts_available=queue_counts_available,
            queue_counts_error=_bounded(queue_counts_error, 128) or None,
            queue_active=queue_active,
            queue_scheduled=queue_scheduled,
            queue_dead_letter=queue_dead_letter,
            queue_total=queue_total,
            completion_configured=completion_configured,
            completion_kind=_bounded(completion_kind, 16),
            completion_accessible=completion_accessible,
            completion_error=_bounded(completion_error, 128) or None,
            completion_subscription_count=completion_subscription_count,
            completion_active=completion_active,
            completion_dead_letter=completion_dead_letter,
            outbox_available=outbox_available,
            outbox_error=_bounded(outbox_error, 128) or None,
            outbox_pending=outbox_pending,
            outbox_pending_truncated=outbox_pending_truncated,
            outbox_oldest_age_seconds=outbox_oldest_age_seconds,
            outbox_last_attempt_at=_bounded(outbox_last_attempt_at, 64) or None,
            outbox_last_success_at=_bounded(outbox_last_success_at, 64) or None,
            outbox_last_error_at=_bounded(outbox_last_error_at, 64) or None,
            outbox_last_scanned=outbox_last_scanned,
            outbox_last_delivered=outbox_last_delivered,
            outbox_last_errors=outbox_last_errors,
            outbox_deferred=outbox_deferred,
            outbox_poison=outbox_poison,
            admission_available=admission_available,
            admission_allowed=admission_allowed,
            admission_reason=_bounded(admission_reason, 128) or None,
            request_policy_available=request_policy_available,
            request_policy_error=_bounded(request_policy_error, 128) or None,
            request_ttl_seconds=request_ttl_seconds,
            producer_request_ttl_seconds=producer_request_ttl_seconds,
            request_dead_letter_on_expiration=request_dead_letter_on_expiration,
            request_max_delivery_count=request_max_delivery_count,
            completion_policy_available=completion_policy_available,
            completion_min_ttl_seconds=completion_min_ttl_seconds,
            completion_dead_letter_on_expiration=completion_dead_letter_on_expiration,
            completion_max_delivery_count=completion_max_delivery_count,
            admission_target_node_count=admission_target_node_count,
            admission_ready_node_count=admission_ready_node_count,
            admission_warmup_job_count=admission_warmup_job_count,
            admission_failed_warmup_job_count=admission_failed_warmup_job_count,
            resident_consumer_enabled=resident_consumer_enabled,
            drain_concurrency=drain_concurrency,
        )
    except Exception:
        return

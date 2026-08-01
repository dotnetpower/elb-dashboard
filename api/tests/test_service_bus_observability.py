"""Tests for payload-free Service Bus request lifecycle telemetry.

Responsibility: Verify request/health event names, safe scalar dimensions, and
    the helpers' refusal to accept request-body fields.
Edit boundaries: Test-only; no Azure Monitor or Service Bus calls.
Key entry points: `test_*` functions.
Risky contracts: Query FASTA and raw payload parameters must remain outside the
    helper signature so request content cannot reach Application Insights.
Validation: `uv run pytest -q api/tests/test_service_bus_observability.py`.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from api.services import service_bus_observability as observability


def test_records_searchable_scalar_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        observability,
        "record_feature_event",
        lambda event, *, status, **attributes: captured.append(
            (event, status, attributes)
        ),
    )

    observability.record_service_bus_request_event(
        "accepted",
        correlation_id="corr-1",
        request_id="req-1",
        message_id="msg-1",
        queue="requests",
        openapi_job_id="job-1",
        program="blastn",
        database="core_nt",
        taxid=9606,
        is_inclusive=False,
        action="complete",
        delivery_count=2,
        sequence_number=42,
        ack_published=True,
    )

    event, status, attributes = captured[0]
    assert event == "servicebus_request"
    assert status == "accepted"
    assert attributes["stage"] == "accepted"
    assert attributes["correlation_id"] == "corr-1"
    assert attributes["openapi_job_id"] == "job-1"
    assert attributes["is_inclusive"] is False
    assert attributes["ack_published"] is True
    assert "query_fasta" not in attributes
    assert "payload" not in attributes


def test_request_event_writes_searchable_parent_process_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=observability.__name__)
    observability.record_service_bus_request_event(
        "accepted",
        correlation_id="wf3:943:exclusive:hypothetical protein:1024979",
        request_id="req-1",
        openapi_job_id="job-1",
        action="complete",
        ack_published=True,
    )

    assert "servicebus_request stage=accepted" in caplog.text
    assert "corr=wf3:943:exclusive:hypothetical protein:1024979" in caplog.text
    assert "ack_published=True" in caplog.text


def test_refuses_request_body_fields() -> None:
    with pytest.raises(TypeError):
        observability.record_service_bus_request_event(  # type: ignore[call-arg]
            "accepted", query_fasta=">secret\nACGT"
        )


def test_bounds_caller_controlled_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        observability,
        "record_feature_event",
        lambda _event, *, status, **attributes: captured.append(
            {"status": status, **attributes}
        ),
    )

    observability.record_service_bus_request_event(
        "x" * 100,
        correlation_id="c" * 1000,
        request_id="r" * 1000,
        database="d" * 1000,
        error_code="e" * 1000,
    )

    event = captured[0]
    assert len(event["status"]) == 64
    assert len(event["correlation_id"]) == 256
    assert len(event["request_id"]) == 256
    assert len(event["database"]) == 256
    assert len(event["error_code"]) == 128


def test_never_raises_when_underlying_emitter_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        observability,
        "record_feature_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("logging failed")),
    )

    observability.record_service_bus_request_event("accepted", correlation_id="corr-1")


def test_records_payload_free_health_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        observability,
        "record_feature_event",
        lambda event, *, status, **attributes: captured.append((event, status, attributes)),
    )

    observability.record_service_bus_health_event(
        status="warning",
        warning_codes="request_dlq_nonempty",
        queue_counts_available=True,
        queue_counts_error="",
        queue_active=3,
        queue_scheduled=1,
        queue_dead_letter=2,
        queue_total=6,
        completion_configured=True,
        completion_kind="topic",
        completion_accessible=True,
        completion_error="",
        completion_subscription_count=1,
        completion_active=0,
        completion_dead_letter=0,
        outbox_available=True,
        outbox_error="",
        outbox_pending=4,
        outbox_pending_truncated=False,
        outbox_oldest_age_seconds=60,
        outbox_last_attempt_at="2026-07-30T12:00:00+00:00",
        outbox_last_success_at="2026-07-30T12:00:00+00:00",
        outbox_last_error_at="",
        outbox_last_scanned=4,
        outbox_last_delivered=4,
        outbox_last_errors=0,
        admission_available=True,
        admission_allowed=True,
        admission_reason="",
        resident_consumer_enabled=True,
        drain_concurrency=4,
    )

    event, status, attributes = captured[0]
    assert event == "servicebus_health"
    assert status == "warning"
    assert attributes["queue_active"] == 3
    assert attributes["outbox_pending"] == 4
    assert "query_fasta" not in attributes
    assert "payload" not in attributes


def test_health_event_refuses_request_body_fields() -> None:
    with pytest.raises(TypeError):
        observability.record_service_bus_health_event(  # type: ignore[call-arg]
            status="ok",
            query_fasta=">secret\nACGT",
        )

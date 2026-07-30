"""Tests for payload-free Service Bus request lifecycle telemetry.

Responsibility: Verify the structured event name, safe scalar dimensions, and
    the helper's refusal to accept request-body fields.
Edit boundaries: Test-only; no Azure Monitor or Service Bus calls.
Key entry points: `test_*` functions.
Risky contracts: Query FASTA and raw payload parameters must remain outside the
    helper signature so request content cannot reach Application Insights.
Validation: `uv run pytest -q api/tests/test_service_bus_observability.py`.
"""

from __future__ import annotations

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

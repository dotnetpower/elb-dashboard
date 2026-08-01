"""Tests for durable Service Bus producer response outbox persistence.

Responsibility: Verify local outbox idempotency, ordering, and publish-confirmed deletion.
Edit boundaries: Persistence contract only; Service Bus publishing is tested with task code.
Key entry points: ``test_*`` functions.
Risky contracts: Duplicate event IDs must not create duplicate rows and pending responses must
    remain present until explicit delivery confirmation.
Validation: ``uv run pytest -q api/tests/test_service_bus_outbox.py``.
"""

from __future__ import annotations

import pytest
from api.services import service_bus_outbox as outbox


@pytest.fixture(autouse=True)
def _local_backend(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    outbox._reset_outbox_for_tests()


def _event(event_id: str, status: str = "failed") -> dict[str, str]:
    return {
        "event": "blast.transition",
        "event_id": event_id,
        "external_correlation_id": "corr-1",
        "status": status,
    }


def test_duplicate_event_id_is_idempotent() -> None:
    assert outbox.enqueue_response(_event("evt-1")) is True
    assert outbox.enqueue_response(_event("evt-1")) is False
    assert [item.event_id for item in outbox.list_pending_responses()] == ["evt-1"]


def test_response_remains_until_delivery_confirmation() -> None:
    outbox.enqueue_response(_event("evt-1"))
    assert outbox.list_pending_responses(limit=1)[0].event["status"] == "failed"
    outbox.mark_response_delivered("evt-1")
    assert outbox.list_pending_responses() == []


def test_pending_response_is_queryable_by_correlation() -> None:
    outbox.enqueue_response(_event("evt-1"))

    assert outbox.has_pending_response("corr-1") is True
    assert outbox.has_pending_response("corr-other") is False

    outbox.mark_response_delivered("evt-1")
    assert outbox.has_pending_response("corr-1") is False


def test_pending_correlation_snapshot_is_bounded() -> None:
    outbox.enqueue_response(_event("evt-1"))

    correlations, complete = outbox.pending_response_correlations(limit=2)
    assert correlations == {"corr-1"}
    assert complete is True

    outbox.enqueue_response({**_event("evt-2"), "external_correlation_id": "corr-2"})
    correlations, complete = outbox.pending_response_correlations(limit=2)
    assert correlations == {"corr-1", "corr-2"}
    assert complete is False


def test_pending_responses_are_oldest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = iter(("2026-07-30T00:00:02+00:00", "2026-07-30T00:00:01+00:00"))
    monkeypatch.setattr(outbox, "_now_iso", lambda: next(timestamps))
    outbox.enqueue_response(_event("evt-later"))
    outbox.enqueue_response(_event("evt-earlier", status="succeeded"))
    assert [item.event_id for item in outbox.list_pending_responses()] == [
        "evt-earlier",
        "evt-later",
    ]


def test_deployed_without_table_endpoint_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-dashboard")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)

    with pytest.raises(outbox.ResponseOutboxPersistenceError):
        outbox.enqueue_response(_event("evt-no-table"))


def test_defer_response_persists_bounded_retry_metadata() -> None:
    outbox.enqueue_response(_event("evt-deferred"))

    outbox.defer_response(
        "evt-deferred",
        error_code="completion_event_compacted",
        retry_after_seconds=3600,
        replacement_event={**_event("evt-deferred"), "compacted": True},
    )

    pending = outbox.list_pending_responses()[0]
    assert pending.failure_count == 1
    assert pending.next_attempt_at
    assert pending.last_error_code == "completion_event_compacted"
    assert pending.event["compacted"] is True


def test_defer_missing_response_is_idempotent_noop() -> None:
    outbox.defer_response(
        "missing",
        error_code="completion_publish_failed",
        retry_after_seconds=30,
    )
    assert outbox.list_pending_responses() == []

"""Tests for bounded Service Bus queue and response-outbox health telemetry.

Responsibility: Verify payload-free health aggregation, partial-failure
    degradation, flush-state tracking, and periodic task emission.
Edit boundaries: Test-only fakes; no Azure, Service Bus, Table, or OpenAPI calls.
Key entry points: ``test_*``.
Risky contracts: Health reads must stay bounded and observational; request or
    response bodies must never enter the emitted custom-event dimensions.
    Celery soft deadlines must propagate instead of reporting false success.
Validation: ``uv run pytest -q api/tests/test_service_bus_health.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api.services import service_bus_health as health
from api.services.service_bus_outbox import PendingResponse
from api.services.service_bus_pref import ServiceBusConfig
from api.tasks.servicebus import tasks as sb_tasks
from billiard.exceptions import SoftTimeLimitExceeded


def _cfg(*, completion_topic: str = "elastic-blast-completions") -> ServiceBusConfig:
    return ServiceBusConfig(
        enabled=True,
        namespace_fqdn="example.servicebus.windows.net",
        request_queue="elastic-blast-requests",
        completion_topic=completion_topic,
        completion_kind="topic",
    )


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    health._reset_health_state_for_tests()
    sb_tasks._LAST_SERVICE_BUS_HEALTH_WARNING = ""


def test_collects_queue_outbox_admission_and_flush_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(health, "_now", lambda: now)
    monkeypatch.setattr(
        health.service_bus,
        "entity_counts",
        lambda _cfg: {
            "queue": {
                "active_message_count": 9,
                "scheduled_message_count": 2,
                "dead_letter_message_count": 1,
                "total_message_count": 12,
            },
            "completion_accessible": True,
            "completion_error": "",
            "subscriptions": [
                {
                    "name": "customer",
                    "active_message_count": 3,
                    "dead_letter_message_count": 2,
                }
            ],
        },
    )
    monkeypatch.setattr(
        health,
        "list_pending_responses",
        lambda *, limit: [
            PendingResponse(
                event_id="evt-1",
                event={"ignored": "payload must not enter the snapshot"},
                created_at=(now - timedelta(minutes=7)).isoformat(),
            )
        ],
    )
    health.note_outbox_flush({"scanned": 4, "delivered": 3, "errors": 1})

    snapshot = health.collect_service_bus_health(
        _cfg(),
        admission={"allowed": False, "reason": "cluster_starting"},
    )

    assert snapshot["status"] == "warning"
    assert snapshot["queue"]["active"] == 9
    assert snapshot["queue"]["completion_dead_letter"] == 2
    assert snapshot["queue"]["completion_accessible"] is True
    assert snapshot["outbox"]["pending"] == 1
    assert snapshot["outbox"]["oldest_age_seconds"] == 420
    assert snapshot["outbox"]["last_errors"] == 1
    assert snapshot["admission_reason"] == "cluster_starting"
    assert "request_dlq_nonempty" in snapshot["warnings"]
    assert "completion_dlq_nonempty" in snapshot["warnings"]
    assert "drain_admission_blocked" in snapshot["warnings"]
    assert "payload" not in str(snapshot)
    assert "ignored" not in str(snapshot)


def test_component_failures_degrade_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health.service_bus,
        "entity_counts",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("admin unavailable")),
    )
    monkeypatch.setattr(
        health,
        "list_pending_responses",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("table unavailable")),
    )

    snapshot = health.collect_service_bus_health(_cfg(completion_topic=""), admission=None)

    assert snapshot["queue"]["counts_available"] is False
    assert snapshot["queue"]["counts_error"] == "RuntimeError"
    assert snapshot["outbox"]["available"] is False
    assert snapshot["outbox"]["error"] == "RuntimeError"
    assert snapshot["admission_available"] is False
    assert snapshot["completion_configured"] is False
    assert set(snapshot["warnings"]) >= {
        "completion_not_configured",
        "queue_counts_unavailable",
        "outbox_unavailable",
    }


def test_outbox_scan_is_bounded_and_reports_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_limits: list[int] = []
    monkeypatch.setattr(
        health.service_bus,
        "entity_counts",
        lambda _cfg: {"queue": {"active_message_count": 0}, "subscriptions": []},
    )

    def pending(*, limit: int) -> list[PendingResponse]:
        observed_limits.append(limit)
        return [
            PendingResponse(str(i), {"status": "queued"}, "2026-07-30T00:00:00+00:00")
            for i in range(limit)
        ]

    monkeypatch.setattr(health, "list_pending_responses", pending)

    snapshot = health.collect_service_bus_health(_cfg(), admission={"allowed": True})

    assert observed_limits == [201]
    assert snapshot["outbox"]["pending"] == 200
    assert snapshot["outbox"]["pending_truncated"] is True
    assert "outbox_backlog_truncated" in snapshot["warnings"]


def test_outbox_corrupt_payload_is_reported_without_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health.service_bus,
        "entity_counts",
        lambda _cfg: {"queue": {"active_message_count": 0}, "subscriptions": []},
    )
    monkeypatch.setattr(
        health,
        "list_pending_responses",
        lambda **_kwargs: [
            PendingResponse(
                event_id="secret-corrupt-event",
                event={},
                created_at="2026-08-25T00:00:00+00:00",
                last_error_code="outbox_payload_corrupt",
            )
        ],
    )

    snapshot = health.collect_service_bus_health(
        _cfg(),
        admission={"allowed": True},
    )

    assert snapshot["outbox"]["corrupt"] == 1
    assert "outbox_corrupt_response_pending" in snapshot["warnings"]
    assert "secret-corrupt-event" not in str(snapshot)


def test_outbox_corrupt_timestamp_is_reported_without_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health.service_bus,
        "entity_counts",
        lambda _cfg: {"queue": {"active_message_count": 0}, "subscriptions": []},
    )
    monkeypatch.setattr(
        health,
        "list_pending_responses",
        lambda **_kwargs: [
            PendingResponse(
                event_id="secret-timestamp-event",
                event={"status": "queued"},
                created_at="2026-08-25T00:00:00+00:00",
                next_attempt_at="2026-08-25T00:05:00+00:00",
                last_error_code="deferred_timestamp_corrupt",
            )
        ],
    )

    snapshot = health.collect_service_bus_health(_cfg(), admission={"allowed": True})

    assert snapshot["outbox"]["timestamp_corrupt"] == 1
    assert "outbox_timestamp_corrupt_pending" in snapshot["warnings"]
    assert "secret-timestamp-event" not in str(snapshot)


def test_warns_on_unsafe_entity_policy_and_counts_warmup_without_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health.service_bus,
        "entity_counts",
        lambda _cfg: {
            "queue": {
                "active_message_count": 2,
                "scheduled_message_count": 0,
                "dead_letter_message_count": 0,
                "total_message_count": 2,
                "telemetry": {
                    "policy": {
                        "available": True,
                        "default_ttl_seconds": 900,
                        "dead_letter_on_expiration": False,
                        "max_delivery_count": 10,
                    }
                },
            },
            "completion_accessible": True,
            "completion_error": "",
            "subscriptions": [
                {
                    "name": "customer",
                    "active_message_count": 0,
                    "dead_letter_message_count": 0,
                    "policy": {
                        "available": True,
                        "default_ttl_seconds": 1800,
                        "dead_letter_on_expiration": False,
                        "max_delivery_count": 10,
                    },
                }
            ],
        },
    )
    monkeypatch.setattr(health, "list_pending_responses", lambda **_kwargs: [])

    snapshot = health.collect_service_bus_health(
        _cfg(),
        admission={
            "allowed": False,
            "reason": "database_warmup_in_progress",
            "target_node_count": 4,
            "ready_node_count": 2,
            "warmup_jobs": {"secret-db-a": "secret-job-a", "secret-db-b": "secret-job-b"},
            "failed_warmup_jobs": {"secret-db-b": "secret-job-b"},
        },
    )

    assert set(snapshot["warnings"]) >= {
        "request_ttl_too_short",
        "request_entity_ttl_shorter_than_producer",
        "request_expiration_not_dead_lettered",
        "completion_ttl_too_short",
        "completion_expiration_not_dead_lettered",
        "drain_admission_blocked",
    }
    assert snapshot["admission_target_node_count"] == 4
    assert snapshot["producer_request_ttl_seconds"] == 86400
    assert snapshot["admission_ready_node_count"] == 2
    assert snapshot["admission_warmup_job_count"] == 2
    assert snapshot["admission_failed_warmup_job_count"] == 1
    assert "secret-db" not in str(snapshot)
    assert "secret-job" not in str(snapshot)


def test_periodic_task_emits_scalar_health_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []
    snapshot = {
        "status": "warning",
        "warnings": ("completion_not_configured",),
        "queue": {
            "counts_available": True,
            "counts_error": "",
            "active": 5,
            "scheduled": 1,
            "dead_letter": 0,
            "total": 6,
            "completion_subscription_count": 0,
            "completion_accessible": False,
            "completion_error": "ServiceBusError",
            "completion_active": 0,
            "completion_dead_letter": 0,
        },
        "outbox": {
            "available": True,
            "error": "",
            "pending": 2,
            "pending_truncated": False,
            "oldest_age_seconds": 30,
            "last_attempt_at": "",
            "last_success_at": "",
            "last_error_at": "",
            "last_scanned": 0,
            "last_delivered": 0,
            "last_errors": 0,
        },
        "admission_available": True,
        "admission_allowed": True,
        "admission_reason": "",
        "completion_configured": False,
        "completion_kind": "topic",
        "resident_consumer_enabled": True,
        "drain_concurrency": 4,
    }
    monkeypatch.setattr(sb_tasks, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(sb_tasks, "get_service_bus_config", lambda: _cfg(completion_topic=""))
    monkeypatch.setattr(
        sb_tasks,
        "_execution_admission_for_drain",
        lambda _cfg: {"allowed": True},
    )
    monkeypatch.setattr(
        sb_tasks,
        "collect_service_bus_health",
        lambda _cfg, *, admission: snapshot,
    )
    monkeypatch.setattr(
        sb_tasks,
        "record_service_bus_health_event",
        lambda **values: emitted.append(values),
    )

    result = sb_tasks.emit_service_bus_health.run()

    assert result == snapshot
    assert emitted[0]["warning_codes"] == "completion_not_configured"
    assert emitted[0]["queue_active"] == 5
    assert emitted[0]["completion_accessible"] is False
    assert emitted[0]["outbox_pending"] == 2
    assert "event" not in emitted[0]
    assert "payload" not in emitted[0]


def test_periodic_task_noops_when_integration_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sb_tasks, "service_bus_enabled", lambda: False)

    assert sb_tasks.emit_service_bus_health.run() == {"skipped": "disabled"}


def test_periodic_task_propagates_admission_soft_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sb_tasks, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(sb_tasks, "get_service_bus_config", _cfg)
    monkeypatch.setattr(
        sb_tasks,
        "_execution_admission_for_drain",
        lambda _cfg: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    with pytest.raises(SoftTimeLimitExceeded):
        sb_tasks.emit_service_bus_health.run()


@pytest.mark.parametrize(
    ("publish_raises", "expected"),
    [
        (False, {"scanned": 1, "delivered": 1, "errors": 0}),
        (True, {"scanned": 1, "delivered": 0, "errors": 1}),
    ],
)
def test_outbox_flush_always_records_health_state(
    monkeypatch: pytest.MonkeyPatch,
    publish_raises: bool,
    expected: dict[str, int],
) -> None:
    recorded: list[tuple[dict[str, int], bool]] = []
    pending = PendingResponse(
        event_id="evt-1",
        event={"event": "blast.transition", "event_id": "evt-1"},
        created_at="2026-07-30T12:00:00+00:00",
    )
    monkeypatch.setattr(sb_tasks, "list_due_responses", lambda *, limit: [pending])
    monkeypatch.setattr(sb_tasks, "mark_response_delivered", lambda _event_id: None)
    monkeypatch.setattr(
        sb_tasks,
        "note_outbox_flush",
        lambda stats, *, attempted=True: recorded.append((dict(stats), attempted)),
    )

    def publish(*_args: object, **_kwargs: object) -> None:
        if publish_raises:
            raise RuntimeError("completion unavailable")

    monkeypatch.setattr(sb_tasks.service_bus, "publish_event", publish)

    result = sb_tasks._flush_response_outbox(_cfg())

    assert result == expected
    assert recorded == [(expected, True)]


@pytest.mark.parametrize("failure_stage", ["list", "publish"])
def test_outbox_flush_propagates_soft_time_limit(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    pending = PendingResponse(
        event_id="evt-soft-limit",
        event={"event": "blast.transition", "event_id": "evt-soft-limit"},
        created_at="2026-08-25T00:00:00+00:00",
    )
    if failure_stage == "list":
        monkeypatch.setattr(
            sb_tasks,
            "list_due_responses",
            lambda **_kwargs: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
        )
    else:
        monkeypatch.setattr(sb_tasks, "list_due_responses", lambda **_kwargs: [pending])
        monkeypatch.setattr(
            sb_tasks.service_bus,
            "publish_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
        )

    with pytest.raises(SoftTimeLimitExceeded):
        sb_tasks._flush_response_outbox(_cfg())


def test_response_staging_propagates_publish_soft_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sb_tasks, "enqueue_response", lambda _event: True)
    monkeypatch.setattr(
        sb_tasks.service_bus,
        "publish_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    with pytest.raises(SoftTimeLimitExceeded):
        sb_tasks._stage_response_event(
            _cfg(),
            {"event": "blast.transition", "event_id": "evt-stage-soft-limit"},
        )


def test_outbox_flush_stops_before_starting_work_past_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [
        PendingResponse(
            event_id=f"evt-{index}",
            event={"event": "blast.transition", "event_id": f"evt-{index}"},
            created_at=f"2026-08-25T00:00:0{index}+00:00",
        )
        for index in range(2)
    ]
    monkeypatch.setattr(sb_tasks, "list_due_responses", lambda **_kwargs: pending)
    published: list[str] = []
    monkeypatch.setattr(
        sb_tasks.service_bus,
        "publish_event",
        lambda _cfg, event: published.append(str(event["event_id"])),
    )
    monkeypatch.setattr(sb_tasks, "mark_response_delivered", lambda _event_id: None)
    clock = iter((0.0, 10.0))

    result = sb_tasks._flush_response_outbox(
        _cfg(),
        deadline=5.0,
        clock=lambda: next(clock),
    )

    assert published == ["evt-0"]
    assert result == {"scanned": 1, "delivered": 1, "errors": 0}


def test_outbox_poison_isolates_only_its_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [
        PendingResponse(
            "poison-queued",
            {"event_id": "poison-queued", "external_correlation_id": "corr-poison"},
            "2026-07-30T12:00:00+00:00",
        ),
        PendingResponse(
            "poison-terminal",
            {"event_id": "poison-terminal", "external_correlation_id": "corr-poison"},
            "2026-07-30T12:00:01+00:00",
        ),
        PendingResponse(
            "healthy",
            {"event_id": "healthy", "external_correlation_id": "corr-healthy"},
            "2026-07-30T12:00:02+00:00",
        ),
    ]
    sent: list[str] = []
    deferred: list[str] = []
    monkeypatch.setattr(sb_tasks, "list_due_responses", lambda *, limit: pending)
    monkeypatch.setattr(sb_tasks, "mark_response_delivered", lambda event_id: None)
    monkeypatch.setattr(
        sb_tasks,
        "defer_response",
        lambda event_id, **_kwargs: deferred.append(event_id),
    )

    def publish(_cfg: object, event: dict[str, Any]) -> None:
        if event["event_id"] == "poison-queued":
            raise sb_tasks.service_bus.ServiceBusEventValidationError("too large")
        sent.append(event["event_id"])

    monkeypatch.setattr(sb_tasks.service_bus, "publish_event", publish)

    result = sb_tasks._flush_response_outbox(_cfg())

    assert result == {"scanned": 3, "delivered": 1, "errors": 1}
    assert deferred == ["poison-queued"]
    assert sent == ["healthy"]

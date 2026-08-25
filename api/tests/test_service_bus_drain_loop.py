"""Tests for the bounded Service Bus drain loop (``drain_requests``).

Responsibility: Verify the real ``drain_requests`` settlement loop — that an
    abandoned message is NOT re-received and re-abandoned within the same tick
    (the bug that burned the whole delivery count → premature dead-letter), and
    that complete/dead-letter still settle exactly once.
Edit boundaries: Exercises the loop with a fake SDK client/receiver injected via
    ``service_bus._client``; no live Service Bus.
Key entry points: the ``test_*`` functions.
Risky contracts: one settle per message per tick; an abandoned message is
deferred to the next tick instead of hot-looping; claimed messages register
automatic lock renewal before any handler runs.
Validation: ``uv run pytest -q api/tests/test_service_bus_drain_loop.py``.
"""

from __future__ import annotations

import ast
import inspect
import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api.services import service_bus
from api.services.service_bus import MessageAction, ServiceBusConfig
from billiard.exceptions import SoftTimeLimitExceeded


@pytest.fixture(autouse=True)
def _stub_request_send_coordination(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.acquire_request_send",
        lambda _queue: (True, None),
    )
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.release_request_send",
        lambda _queue, *, token, retain_seconds=0: None,
    )


class _FakeMessage:
    def __init__(self, message_id: str, body: dict[str, Any]) -> None:
        self.message_id = message_id
        self.sequence_number = hash(message_id) & 0xFFFF
        self.correlation_id = message_id
        self.subject = "blast.request"
        self.content_type = "application/json"
        self.enqueued_time_utc = None
        self.application_properties: dict[str, Any] = {}
        self.dead_letter_reason = None
        import json

        self._raw = json.dumps(body).encode("utf-8")

    @property
    def body(self):
        return [self._raw]


class _FakeReceiver:
    """Simulates a peek-lock receiver where abandoned messages reappear."""

    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._available = list(messages)
        self.completed: list[str] = []
        self.abandoned: list[str] = []
        self.dead_lettered: list[str] = []
        self.dead_letter_reasons: list[tuple[str, str, str | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def receive_messages(self, max_message_count: int, max_wait_time: int):
        batch = self._available[:max_message_count]
        self._available = self._available[max_message_count:]
        return batch

    def complete_message(self, message: _FakeMessage) -> None:
        self.completed.append(message.message_id)

    def abandon_message(self, message: _FakeMessage) -> None:
        self.abandoned.append(message.message_id)
        # Abandon makes the message receivable again (the real-broker behaviour
        # that caused the hot-loop bug). Re-queue it so the loop *could* see it.
        self._available.append(message)

    def dead_letter_message(
        self,
        message: _FakeMessage,
        reason: str = "",
        error_description: str | None = None,
    ) -> None:
        self.dead_lettered.append(message.message_id)
        self.dead_letter_reasons.append((message.message_id, reason, error_description))


class _FakeClient:
    def __init__(self, receiver: _FakeReceiver) -> None:
        self._receiver = receiver

    def get_queue_receiver(self, *_a: Any, **_k: Any) -> _FakeReceiver:
        return self._receiver


def _cfg() -> ServiceBusConfig:
    return ServiceBusConfig(
        enabled=True, auth_mode="entra", namespace_fqdn="x.servicebus.windows.net"
    )


def _patch_client(monkeypatch: pytest.MonkeyPatch, receiver: _FakeReceiver) -> None:
    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _FakeClient(receiver)

    monkeypatch.setattr(service_bus, "_client", fake_client)


def test_abandoned_message_not_reabandoned_same_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler that abandons must burn only ONE delivery attempt per tick."""
    receiver = _FakeReceiver([_FakeMessage("m1", {"db": "core_nt"})])
    _patch_client(monkeypatch, receiver)

    stats = service_bus.drain_requests(_cfg(), lambda _m: MessageAction.ABANDON, max_messages=50)
    # The pass yields immediately after the transient batch, so the message is
    # abandoned exactly once and cannot burn delivery count twice in one pass.
    assert stats.abandoned == 1
    assert receiver.abandoned.count("m1") == 1
    assert stats.received == 1  # only counted as handled once


def test_complete_settles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeReceiver([_FakeMessage("m1", {"db": "core_nt"})])
    _patch_client(monkeypatch, receiver)

    stats = service_bus.drain_requests(_cfg(), lambda _m: MessageAction.COMPLETE, max_messages=50)
    assert stats.completed == 1
    assert receiver.completed == ["m1"]
    assert receiver.abandoned == []


def test_claimed_message_registers_lock_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = _FakeReceiver([_FakeMessage("m1", {"db": "core_nt"})])
    _patch_client(monkeypatch, receiver)
    registrations: list[tuple[Any, Any, int]] = []

    class _Renewer:
        def __init__(self, *, max_lock_renewal_duration: int) -> None:
            self.duration = max_lock_renewal_duration

        def __enter__(self):
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def register(
            self,
            registered_receiver: Any,
            message: Any,
            *,
            max_lock_renewal_duration: int,
        ) -> None:
            registrations.append((registered_receiver, message, max_lock_renewal_duration))

    monkeypatch.setattr(service_bus, "AutoLockRenewer", _Renewer)

    stats = service_bus.drain_requests(_cfg(), lambda _m: MessageAction.COMPLETE, max_messages=1)

    assert stats.completed == 1
    assert len(registrations) == 1
    assert registrations[0][0] is receiver
    assert registrations[0][1].message_id == "m1"
    assert registrations[0][2] == service_bus._MAX_LOCK_RENEWAL_SECONDS


def test_lock_renewal_registration_failure_is_visible_and_still_settles(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    receiver = _FakeReceiver([_FakeMessage("m1", {"db": "core_nt"})])
    _patch_client(monkeypatch, receiver)

    class _BrokenRenewer:
        def __init__(self, *, max_lock_renewal_duration: int) -> None:
            del max_lock_renewal_duration

        def __enter__(self):
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def register(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("protocol mismatch")

    monkeypatch.setattr(service_bus, "AutoLockRenewer", _BrokenRenewer)

    stats = service_bus.drain_requests(_cfg(), lambda _m: MessageAction.COMPLETE, max_messages=1)

    assert stats.completed == 1
    assert receiver.completed == ["m1"]
    assert "lock renewal registration failed" in caplog.text
    assert "RuntimeError" in caplog.text


def test_dead_letter_settles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeReceiver([_FakeMessage("m1", {"db": "core_nt"})])
    _patch_client(monkeypatch, receiver)

    stats = service_bus.drain_requests(
        _cfg(), lambda _m: MessageAction.DEAD_LETTER, max_messages=50
    )
    assert stats.dead_lettered == 1
    assert receiver.dead_lettered == ["m1"]
    assert receiver.dead_letter_reasons == [("m1", "handler_rejected", None)]


def test_dead_letter_preserves_handler_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeReceiver([_FakeMessage("m1", {"db": "core_nt"})])
    _patch_client(monkeypatch, receiver)

    def handler(message: service_bus.ParsedMessage) -> MessageAction:
        message.settlement_reason = "servicebus_malformed_request"
        message.settlement_description = "request contract rejected"
        return MessageAction.DEAD_LETTER

    stats = service_bus.drain_requests(_cfg(), handler, max_messages=1)

    assert stats.dead_lettered == 1
    assert receiver.dead_letter_reasons == [
        ("m1", "servicebus_malformed_request", "request contract rejected")
    ]


def test_multiple_distinct_messages_all_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    msgs = [_FakeMessage(f"m{i}", {"db": "core_nt"}) for i in range(5)]
    receiver = _FakeReceiver(msgs)
    _patch_client(monkeypatch, receiver)

    stats = service_bus.drain_requests(_cfg(), lambda _m: MessageAction.COMPLETE, max_messages=50)
    assert stats.completed == 5
    assert sorted(receiver.completed) == ["m0", "m1", "m2", "m3", "m4"]


class _FakeTopicSender:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def send_messages(self, message: Any) -> None:
        self.sent.append(message)


class _FailingQueueSender(_FakeTopicSender):
    def send_messages(self, message: Any) -> None:
        del message
        raise RuntimeError("broker unavailable")


class _FakeTopicClient:
    def __init__(self, sender: _FakeTopicSender) -> None:
        self._sender = sender

    def get_topic_sender(self, *_a: Any, **_k: Any) -> _FakeTopicSender:
        return self._sender


def test_retry_schedules_clone_before_completing_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = _FakeReceiver([_FakeMessage("retry-source", {"db": "core_nt"})])
    sender = _FakeTopicSender()

    class _RetryClient(_FakeClient):
        def get_queue_sender(self, *_a: Any, **_k: Any) -> _FakeTopicSender:
            return sender

    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _RetryClient(receiver)

    monkeypatch.setattr(service_bus, "_client", fake_client)

    stats = service_bus.drain_requests(
        _cfg(),
        lambda _m: MessageAction.RETRY,
        max_messages=50,
        max_concurrency=4,
    )

    assert stats.retried == 1
    assert receiver.completed == ["retry-source"]
    assert receiver.abandoned == []
    assert len(sender.sent) == 1
    retry = sender.sent[0]
    assert str(retry.message_id).startswith("retry-")
    assert retry.application_properties["elb_retry_attempt"] == 1
    assert retry.scheduled_enqueue_time_utc is not None


def test_retry_preserves_original_absolute_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    source = _FakeMessage("retry-expiry", {"db": "core_nt"})
    source.expires_at_utc = now + timedelta(hours=2)
    receiver = _FakeReceiver([source])
    sender = _FakeTopicSender()

    class _RetryClient(_FakeClient):
        def get_queue_sender(self, *_a: Any, **_k: Any) -> _FakeTopicSender:
            return sender

    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _RetryClient(receiver)

    monkeypatch.setattr(service_bus, "_client", fake_client)
    monkeypatch.setattr(service_bus, "_now", lambda: now)
    service_bus.drain_requests(_cfg(), lambda _m: MessageAction.RETRY, max_messages=1)

    retry = sender.sent[0]
    assert retry.scheduled_enqueue_time_utc == now + timedelta(seconds=30)
    assert retry.time_to_live == timedelta(hours=2) - timedelta(seconds=30)


def test_retry_message_id_preserves_legacy_correlation_attempt_contract() -> None:
    left = service_bus.ParsedMessage(
        body={"db": "core_nt"},
        raw_body='{"db":"core_nt"}',
        message_id="left",
        correlation_id="corr-shared",
        subject="blast.request",
        content_type="application/json",
        enqueued_time_utc=None,
        sequence_number=1,
    )
    right = service_bus.ParsedMessage(
        body={"db": "nt"},
        raw_body='{"db":"nt"}',
        message_id="right",
        correlation_id="corr-shared",
        subject="blast.request",
        content_type="application/json",
        enqueued_time_utc=None,
        sequence_number=2,
    )

    assert (
        service_bus._retry_message(left).message_id == service_bus._retry_message(right).message_id
    )


def test_retry_expiry_guard_reserves_clock_skew_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(service_bus, "_now", lambda: now)
    parsed = service_bus.ParsedMessage(
        body={},
        raw_body="{}",
        message_id="m",
        correlation_id="c",
        subject="blast.request",
        content_type="application/json",
        enqueued_time_utc=now,
        sequence_number=1,
        expires_at_utc=now + timedelta(seconds=89),
    )

    # 30s retry delay + 60s skew/scheduling margin reaches the 89s expiry.
    assert service_bus.retry_would_outlive_request(parsed) is True


def _patch_topic_client(monkeypatch: pytest.MonkeyPatch, sender: _FakeTopicSender) -> None:
    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _FakeTopicClient(sender)

    monkeypatch.setattr(service_bus, "_client", fake_client)


def _topic_cfg() -> ServiceBusConfig:
    return ServiceBusConfig(
        enabled=True,
        auth_mode="entra",
        namespace_fqdn="x.servicebus.windows.net",
        completion_topic="elastic-blast-completions",
    )


def test_publish_event_stamps_request_id_on_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``request_id`` on the event body is echoed onto the message envelope
    (``application_properties``) so a topic subscriber correlates without
    parsing the payload."""
    sender = _FakeTopicSender()
    _patch_topic_client(monkeypatch, sender)

    service_bus.publish_event(
        _topic_cfg(),
        {
            "event": "blast.transition",
            "external_correlation_id": "corr-1",
            "status": "running",
            "request_id": "req-abc-123",
        },
    )
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    assert dict(msg.application_properties or {}).get("request_id") == "req-abc-123"
    # Body still carries it too (round-trips for body-only subscribers).
    import json

    body = json.loads(b"".join(msg.body).decode("utf-8"))
    assert body["request_id"] == "req-abc-123"


def test_publish_event_no_request_id_leaves_envelope_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``request_id`` on the event → no ``application_properties`` stamped
    (the common case stays byte-identical to before)."""
    sender = _FakeTopicSender()
    _patch_topic_client(monkeypatch, sender)

    service_bus.publish_event(
        _topic_cfg(),
        {
            "event": "blast.transition",
            "external_correlation_id": "corr-2",
            "status": "queued",
        },
    )
    assert len(sender.sent) == 1
    assert not (sender.sent[0].application_properties or {})


def test_publish_event_rejects_oversized_payload_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _FakeTopicSender()
    _patch_topic_client(monkeypatch, sender)

    with pytest.raises(service_bus.ServiceBusEventValidationError):
        service_bus.publish_event(
            _topic_cfg(),
            {"event": "blast.transition", "oversized": "x" * (193 * 1024)},
        )

    assert sender.sent == []


class _FakeQueueClient:
    def __init__(self, sender: _FakeTopicSender) -> None:
        self._sender = sender

    def get_queue_sender(self, *_a: Any, **_k: Any) -> _FakeTopicSender:
        return self._sender

    def get_topic_sender(self, *_a: Any, **_k: Any) -> _FakeTopicSender:
        raise AssertionError("queue completion entity must use get_queue_sender")


def test_send_request_emits_enqueued_observability_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _FakeTopicSender()

    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _FakeQueueClient(sender)

    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(service_bus, "_client", fake_client)
    monkeypatch.setattr(
        service_bus,
        "record_service_bus_request_event",
        lambda stage, **attributes: events.append((stage, attributes)),
    )
    monkeypatch.setattr(
        "api.services.aks.queue_autostart.request_autostart_evaluation",
        lambda **_kwargs: None,
    )

    message_id = service_bus.send_request(
        _cfg(),
        {
            "program": "blastn",
            "db": "core_nt",
            "query_fasta": ">secret\nACGT",
            "request_id": "req-1",
            "taxid": 9606,
            "is_inclusive": False,
        },
        message_id="msg-1",
        correlation_id="corr-1",
    )

    assert message_id == "msg-1"
    assert sender.sent[0].time_to_live == timedelta(hours=24)
    assert events == [
        (
            "enqueued",
            {
                "correlation_id": "corr-1",
                "request_id": "req-1",
                "message_id": "msg-1",
                "queue": "elastic-blast-requests",
                "program": "blastn",
                "database": "core_nt",
                "taxid": 9606,
                "is_inclusive": False,
                "action": "sent",
            },
        )
    ]
    assert "secret" not in str(events)


def test_send_request_preserves_legacy_wire_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _FakeTopicSender()
    captured: dict[str, Any] = {}
    real_message = service_bus.ServiceBusMessage

    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _FakeQueueClient(sender)

    def capture_message(payload: str, **kwargs: Any) -> Any:
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return real_message(payload, **kwargs)

    monkeypatch.setattr(service_bus, "_client", fake_client)
    monkeypatch.setattr(service_bus, "ServiceBusMessage", capture_message)
    monkeypatch.setattr(service_bus, "record_service_bus_request_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "api.services.aks.queue_autostart.request_autostart_evaluation",
        lambda **_kwargs: None,
    )

    body = {"program": "blastn", "db": "core_nt", "query_fasta": ">q\nACGT"}
    service_bus.send_request(
        _cfg(),
        body,
        correlation_id="corr-legacy-wire",
    )

    assert captured["payload"] == json.dumps(body, default=str)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["message_id"] is None
    assert kwargs["correlation_id"] == "corr-legacy-wire"
    assert kwargs["content_type"] == "application/json"
    assert kwargs["subject"] == "blast.request"
    assert kwargs["time_to_live"] == timedelta(hours=24)


def test_send_request_rejects_oversized_body_before_opening_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service_bus,
        "_client",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("oversized request must fail before Service Bus I/O")
        ),
    )

    with pytest.raises(service_bus.ServiceBusRequestValidationError):
        service_bus.send_request(
            _cfg(),
            {
                "program": "blastn",
                "db": "core_nt",
                "query_fasta": ">q\n" + ("A" * service_bus._MAX_REQUEST_MESSAGE_BYTES),
            },
            correlation_id="corr-oversized",
        )


def test_send_request_stops_while_reconfiguration_fence_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.acquire_request_send",
        lambda _queue: (False, None),
    )
    monkeypatch.setattr(
        service_bus,
        "_client",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("fenced send must fail before Service Bus I/O")
        ),
    )

    with pytest.raises(service_bus.ServiceBusUnavailable, match="reconfiguration"):
        service_bus.send_request(
            _cfg(),
            {"program": "blastn", "db": "core_nt", "query_fasta": ">q\nACGT"},
            correlation_id="corr-reconfigure-fence",
        )


def test_send_request_reports_coordination_outage_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.tasks.servicebus.drain_coordination import (
        RequestSendCoordinationUnavailable,
    )

    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.acquire_request_send",
        lambda _queue: (_ for _ in ()).throw(
            RequestSendCoordinationUnavailable("coordination unavailable")
        ),
    )

    with pytest.raises(service_bus.ServiceBusUnavailable, match="coordination unavailable"):
        service_bus.send_request(
            _cfg(),
            {"program": "blastn", "db": "core_nt", "query_fasta": ">q\nACGT"},
            correlation_id="corr-coordination-outage",
        )


def test_send_request_rejects_stale_routing_config_after_acquiring_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _cfg()
    stale.revision = "revision-before-change"
    current = ServiceBusConfig(
        enabled=True,
        auth_mode="entra",
        namespace_fqdn=stale.namespace_fqdn,
        request_queue="new-request-queue",
    )
    monkeypatch.setattr(service_bus, "get_service_bus_config", lambda: current)
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.acquire_request_send",
        lambda _queue: (True, "send-token"),
    )
    released: list[tuple[str, str | None, int]] = []
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.release_request_send",
        lambda queue, *, token, retain_seconds=0: released.append((queue, token, retain_seconds)),
    )
    monkeypatch.setattr(
        service_bus,
        "_client",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("stale config must fail before Service Bus I/O")
        ),
    )

    with pytest.raises(service_bus.ServiceBusUnavailable, match="configuration changed"):
        service_bus.send_request(
            stale,
            {"program": "blastn", "db": "core_nt", "query_fasta": ">q\nACGT"},
            correlation_id="corr-stale-routing",
        )

    assert released == [(stale.request_queue, "send-token", 0)]


def test_config_io_guard_rejects_snapshot_changed_after_token_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _cfg()
    current = ServiceBusConfig(
        **{
            **stale.to_dict(),
            "completion_topic": "new-completions",
        }
    )
    monkeypatch.setattr(service_bus, "get_service_bus_config", lambda: current)
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.acquire_request_send",
        lambda _queue: (True, "io-token"),
    )
    released: list[tuple[str, str | None, int]] = []
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.release_request_send",
        lambda queue, *, token, retain_seconds=0: released.append((queue, token, retain_seconds)),
    )

    with pytest.raises(service_bus.ServiceBusUnavailable, match="configuration changed"):
        service_bus.acquire_config_io(stale)

    assert released == [(stale.request_queue, "io-token", 0)]


def test_send_request_releases_inflight_lease_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _FakeTopicSender()

    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _FakeQueueClient(sender)

    monkeypatch.setattr(service_bus, "_client", fake_client)
    monkeypatch.setattr(service_bus, "record_service_bus_request_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "api.services.aks.queue_autostart.request_autostart_evaluation",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.acquire_request_send",
        lambda _queue: (True, "send-token"),
    )
    released: list[tuple[str, str | None, int]] = []
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.release_request_send",
        lambda queue, *, token, retain_seconds=0: released.append((queue, token, retain_seconds)),
    )

    service_bus.send_request(
        _cfg(),
        {"program": "blastn", "db": "core_nt", "query_fasta": ">q\nACGT"},
        correlation_id="corr-send-release",
    )

    assert released == [
        (
            "elastic-blast-requests",
            "send-token",
            service_bus._REQUEST_SEND_VISIBILITY_GRACE_SECONDS,
        )
    ]


def test_send_request_emits_failure_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _FakeQueueClient(_FailingQueueSender())

    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(service_bus, "_client", fake_client)
    monkeypatch.setattr(
        service_bus,
        "record_service_bus_request_event",
        lambda stage, **attributes: events.append((stage, attributes)),
    )

    with pytest.raises(RuntimeError, match="broker unavailable"):
        service_bus.send_request(
            _cfg(),
            {"program": "blastn", "db": "core_nt", "query_fasta": ">secret\nACGT"},
            message_id="msg-fail",
            correlation_id="corr-fail",
        )

    assert events[0][0] == "enqueue_failed"
    assert events[0][1]["action"] == "outcome_unknown"
    assert events[0][1]["error_code"] == "RuntimeError"
    assert "secret" not in str(events)


def test_publish_event_queue_kind_uses_queue_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``completion_kind=queue`` the event is sent to a queue (point-to-point)
    via ``get_queue_sender``, never a topic sender."""
    sender = _FakeTopicSender()

    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _FakeQueueClient(sender)

    monkeypatch.setattr(service_bus, "_client", fake_client)

    cfg = ServiceBusConfig(
        enabled=True,
        auth_mode="entra",
        namespace_fqdn="x.servicebus.windows.net",
        completion_topic="elastic-blast-completions",
        completion_kind="queue",
    )
    service_bus.publish_event(
        cfg,
        {
            "event": "blast.transition",
            "external_correlation_id": "corr-q",
            "status": "succeeded",
        },
    )
    assert len(sender.sent) == 1
    assert sender.sent[0].correlation_id == "corr-q"


# --------------------------------------------------------------------------- #
# Parallel drain (max_concurrency > 1): handler bodies run concurrently but
# settlement still happens once per message on the main thread in receiver order.
# --------------------------------------------------------------------------- #


def test_parallel_drain_maps_each_action_and_settles_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With max_concurrency>1 every message settles exactly once with the action
    its handler returned — parallelism must not reorder or drop settlements."""
    msgs = [_FakeMessage(f"p{i}", {"db": "core_nt"}) for i in range(6)]
    receiver = _FakeReceiver(msgs)
    _patch_client(monkeypatch, receiver)

    def handler(m: Any) -> MessageAction:
        n = int(m.correlation_id[1:])
        return MessageAction.COMPLETE if n % 2 == 0 else MessageAction.DEAD_LETTER

    stats = service_bus.drain_requests(_cfg(), handler, max_messages=50, max_concurrency=8)
    assert set(receiver.completed) == {"p0", "p2", "p4"}
    assert set(receiver.dead_lettered) == {"p1", "p3", "p5"}
    assert stats.completed == 3
    assert stats.dead_lettered == 3
    assert stats.received == 6


def test_parallel_drain_isolates_one_handler_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One handler raising in a parallel batch abandons only that message; the
    others still complete (partial-failure isolation across threads)."""
    msgs = [_FakeMessage(f"e{i}", {"db": "core_nt"}) for i in range(4)]
    receiver = _FakeReceiver(msgs)
    _patch_client(monkeypatch, receiver)

    def handler(m: Any) -> MessageAction:
        if m.correlation_id == "e2":
            raise RuntimeError("boom")
        return MessageAction.COMPLETE

    stats = service_bus.drain_requests(_cfg(), handler, max_messages=4, max_concurrency=4)
    assert set(receiver.completed) == {"e0", "e1", "e3"}
    assert "e2" in receiver.abandoned
    assert stats.completed == 3


def test_parallel_drain_does_not_wait_for_executor_after_soft_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = _FakeReceiver(
        [
            _FakeMessage("soft-pool-1", {"db": "core_nt"}),
            _FakeMessage("soft-pool-2", {"db": "core_nt"}),
        ]
    )
    _patch_client(monkeypatch, receiver)
    shutdown_calls: list[tuple[bool, bool]] = []

    class _Future:
        def result(self) -> MessageAction:
            raise SoftTimeLimitExceeded()

    class _Pool:
        def submit(self, *_args: object, **_kwargs: object) -> _Future:
            return _Future()

        def shutdown(self, wait: bool, *, cancel_futures: bool = False) -> None:
            shutdown_calls.append((wait, cancel_futures))

    monkeypatch.setattr(service_bus, "ThreadPoolExecutor", lambda **_kwargs: _Pool())

    with pytest.raises(SoftTimeLimitExceeded):
        service_bus.drain_requests(
            _cfg(),
            lambda _message: MessageAction.COMPLETE,
            max_messages=2,
            max_concurrency=2,
        )

    assert shutdown_calls == [(False, True)]


def test_safe_drain_handler_propagates_soft_time_limit() -> None:
    parsed = service_bus.ParsedMessage(
        body={},
        raw_body="{}",
        message_id="soft-limit",
        correlation_id="soft-limit",
        subject="blast.request",
        content_type="application/json",
        enqueued_time_utc=None,
        sequence_number=1,
    )

    with pytest.raises(SoftTimeLimitExceeded):
        service_bus._safe_drain_handler(
            lambda _message: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
            parsed,
        )


def test_settlement_propagates_soft_time_limit() -> None:
    parsed = service_bus.ParsedMessage(
        body={},
        raw_body="{}",
        message_id="settle-soft-limit",
        correlation_id="settle-soft-limit",
        subject="blast.request",
        content_type="application/json",
        enqueued_time_utc=None,
        sequence_number=1,
    )
    receiver = type(
        "_SoftLimitReceiver",
        (),
        {
            "complete_message": lambda _self, _message: (_ for _ in ()).throw(
                SoftTimeLimitExceeded()
            )
        },
    )()
    stats = service_bus.DrainStats()

    with pytest.raises(SoftTimeLimitExceeded):
        service_bus._settle(
            receiver,
            None,
            object(),
            parsed,
            MessageAction.COMPLETE,
            stats,
        )

    assert stats.completed == 0
    assert stats.abandoned == 0


def test_service_bus_data_plane_broad_catches_propagate_soft_deadline() -> None:
    tree = ast.parse(inspect.getsource(service_bus))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        caught_names: set[str] = set()
        for handler in node.handlers:
            if isinstance(handler.type, ast.Name):
                caught_names.add(handler.type.id)
            elif isinstance(handler.type, ast.Tuple):
                caught_names.update(
                    item.id for item in handler.type.elts if isinstance(item, ast.Name)
                )
        if "Exception" in caught_names and "SoftTimeLimitExceeded" not in caught_names:
            missing.append(node.lineno)

    assert missing == [], f"broad catches swallow SoftTimeLimitExceeded at lines {missing}"


def test_parallel_drain_actually_runs_handlers_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proof of concurrency: a barrier that only releases when all N handlers are
    in-flight at once. Serial execution would time out (BrokenBarrierError →
    ABANDON) and fail the completed-count assertion."""
    msgs = [_FakeMessage(f"c{i}", {"db": "core_nt"}) for i in range(4)]
    receiver = _FakeReceiver(msgs)
    _patch_client(monkeypatch, receiver)
    barrier = threading.Barrier(4, timeout=5)

    def handler(_m: Any) -> MessageAction:
        barrier.wait()  # all 4 must be in-flight together or this raises
        return MessageAction.COMPLETE

    stats = service_bus.drain_requests(_cfg(), handler, max_messages=4, max_concurrency=4)
    assert stats.completed == 4
    assert set(receiver.completed) == {"c0", "c1", "c2", "c3"}


def test_serial_default_creates_no_thread_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_concurrency=1 (the default) must NOT spawn a ThreadPoolExecutor, so
    the legacy serial path is byte-for-byte unchanged (charter §12a Rule 4)."""
    created: list[int] = []
    real_pool = service_bus.ThreadPoolExecutor

    def spy(*a: Any, **k: Any) -> Any:
        created.append(1)
        return real_pool(*a, **k)

    monkeypatch.setattr(service_bus, "ThreadPoolExecutor", spy)
    receiver = _FakeReceiver([_FakeMessage("s1", {"db": "core_nt"})])
    _patch_client(monkeypatch, receiver)

    stats = service_bus.drain_requests(_cfg(), lambda _m: MessageAction.COMPLETE, max_messages=50)
    assert created == []  # no pool created for the serial default
    assert stats.completed == 1


def test_parallel_drain_handles_multiple_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """70 messages span 3 receive batches (32+32+6); all settle under fan-out."""
    msgs = [_FakeMessage(f"b{i}", {"db": "core_nt"}) for i in range(70)]
    receiver = _FakeReceiver(msgs)
    _patch_client(monkeypatch, receiver)

    stats = service_bus.drain_requests(
        _cfg(), lambda _m: MessageAction.COMPLETE, max_messages=100, max_concurrency=8
    )
    assert stats.completed == 70
    assert stats.received == 70
    assert len(receiver.completed) == 70


def test_receive_batch_is_capped_to_handler_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    msgs = [_FakeMessage(f"cap-{i}", {"db": "core_nt"}) for i in range(12)]
    receiver = _FakeReceiver(msgs)
    requested: list[int] = []
    original_receive = receiver.receive_messages

    def receive(max_message_count: int, max_wait_time: int):
        requested.append(max_message_count)
        return original_receive(max_message_count, max_wait_time)

    receiver.receive_messages = receive  # type: ignore[method-assign]
    _patch_client(monkeypatch, receiver)

    stats = service_bus.drain_requests(
        _cfg(),
        lambda _m: MessageAction.COMPLETE,
        max_messages=12,
        max_concurrency=4,
    )

    assert stats.completed == 12
    assert requested
    assert max(requested) <= 4


def test_pass_budget_yields_without_locking_remaining_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = _FakeReceiver([_FakeMessage(f"budget-{i}", {"db": "core_nt"}) for i in range(3)])
    _patch_client(monkeypatch, receiver)
    clock = iter((0.0, 241.0))

    stats = service_bus.drain_requests(
        _cfg(),
        lambda _m: MessageAction.COMPLETE,
        max_messages=3,
        max_concurrency=1,
        max_pass_seconds=240,
        clock=lambda: next(clock),
    )

    assert stats.completed == 1
    assert stats.budget_exhausted is True
    assert len(receiver._available) == 2

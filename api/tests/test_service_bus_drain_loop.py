"""Tests for the bounded Service Bus drain loop (``drain_requests``).

Responsibility: Verify the real ``drain_requests`` settlement loop — that an
    abandoned message is NOT re-received and re-abandoned within the same tick
    (the bug that burned the whole delivery count → premature dead-letter), and
    that complete/dead-letter still settle exactly once.
Edit boundaries: Exercises the loop with a fake SDK client/receiver injected via
    ``service_bus._client``; no live Service Bus.
Key entry points: the ``test_*`` functions.
Risky contracts: one settle per message per tick; an abandoned message is
    deferred to the next tick instead of hot-looping.
Validation: ``uv run pytest -q api/tests/test_service_bus_drain_loop.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from api.services import service_bus
from api.services.service_bus import MessageAction, ServiceBusConfig


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

    def dead_letter_message(self, message: _FakeMessage, reason: str = "") -> None:
        self.dead_lettered.append(message.message_id)


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

    stats = service_bus.drain_requests(
        _cfg(), lambda _m: MessageAction.ABANDON, max_messages=50
    )
    # Despite the message reappearing after abandon, it is abandoned at most
    # twice (once handled, once as the deferred re-delivery guard) — NOT 50x.
    assert stats.abandoned <= 2
    assert receiver.abandoned.count("m1") <= 2
    assert stats.received == 1  # only counted as handled once


def test_complete_settles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeReceiver([_FakeMessage("m1", {"db": "core_nt"})])
    _patch_client(monkeypatch, receiver)

    stats = service_bus.drain_requests(
        _cfg(), lambda _m: MessageAction.COMPLETE, max_messages=50
    )
    assert stats.completed == 1
    assert receiver.completed == ["m1"]
    assert receiver.abandoned == []


def test_dead_letter_settles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeReceiver([_FakeMessage("m1", {"db": "core_nt"})])
    _patch_client(monkeypatch, receiver)

    stats = service_bus.drain_requests(
        _cfg(), lambda _m: MessageAction.DEAD_LETTER, max_messages=50
    )
    assert stats.dead_lettered == 1
    assert receiver.dead_lettered == ["m1"]


def test_multiple_distinct_messages_all_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    msgs = [_FakeMessage(f"m{i}", {"db": "core_nt"}) for i in range(5)]
    receiver = _FakeReceiver(msgs)
    _patch_client(monkeypatch, receiver)

    stats = service_bus.drain_requests(
        _cfg(), lambda _m: MessageAction.COMPLETE, max_messages=50
    )
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


class _FakeTopicClient:
    def __init__(self, sender: _FakeTopicSender) -> None:
        self._sender = sender

    def get_queue_sender(self, *_a: Any, **_k: Any) -> _FakeTopicSender:
        return self._sender

    def get_topic_sender(self, *_a: Any, **_k: Any) -> _FakeTopicSender:
        return self._sender


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



class _RecordingClient:
    """Records which sender kinds were requested and the messages sent through
    each, so a test can assert the queue-first / optional-topic contract."""

    def __init__(self) -> None:
        self.queue_senders: dict[str, _FakeTopicSender] = {}
        self.topic_senders: dict[str, _FakeTopicSender] = {}

    def get_queue_sender(self, name: str) -> _FakeTopicSender:
        sender = self.queue_senders.setdefault(name, _FakeTopicSender())
        return sender

    def get_topic_sender(self, name: str) -> _FakeTopicSender:
        sender = self.topic_senders.setdefault(name, _FakeTopicSender())
        return sender


def _patch_recording_client(
    monkeypatch: pytest.MonkeyPatch, client: _RecordingClient
) -> None:
    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield client

    monkeypatch.setattr(service_bus, "_client", fake_client)


def test_publish_event_uses_result_queue_only_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Messaging is unified on queues: the event lands on ``completion_queue``
    and the optional fan-out topic is NOT touched unless explicitly enabled."""
    client = _RecordingClient()
    _patch_recording_client(monkeypatch, client)

    cfg = ServiceBusConfig(
        enabled=True,
        auth_mode="entra",
        namespace_fqdn="x.servicebus.windows.net",
        completion_queue="elastic-blast-results",
        completion_topic="elastic-blast-completions",
        completion_topic_enabled=False,
    )
    service_bus.publish_event(cfg, {"event": "blast.transition", "status": "running"})

    assert "elastic-blast-results" in client.queue_senders
    assert len(client.queue_senders["elastic-blast-results"].sent) == 1
    assert client.topic_senders == {}


def test_publish_event_fans_out_to_topic_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the future fan-out explicitly enabled the same event is published to
    BOTH the result queue and the topic (two distinct message instances)."""
    client = _RecordingClient()
    _patch_recording_client(monkeypatch, client)

    cfg = ServiceBusConfig(
        enabled=True,
        auth_mode="entra",
        namespace_fqdn="x.servicebus.windows.net",
        completion_queue="elastic-blast-results",
        completion_topic="elastic-blast-completions",
        completion_topic_enabled=True,
    )
    service_bus.publish_event(cfg, {"event": "blast.transition", "status": "running"})

    q_sent = client.queue_senders["elastic-blast-results"].sent
    t_sent = client.topic_senders["elastic-blast-completions"].sent
    assert len(q_sent) == 1
    assert len(t_sent) == 1
    # The SDK forbids re-sending one message object through two senders, so the
    # queue and topic copies must be distinct instances.
    assert q_sent[0] is not t_sent[0]

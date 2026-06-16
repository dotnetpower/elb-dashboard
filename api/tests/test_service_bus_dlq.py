"""Tests for the Service Bus dead-letter (DLQ) peek / delete / promote helpers.

Responsibility: Verify the operator-facing DLQ management primitives in
    ``api.services.service_bus`` — ``peek_dead_letter_previews`` (non-destructive,
    surfaces dead-letter reason + sequence_number), ``delete_dead_letter_messages``
    (targeted hard-delete by sequence number, non-targets left in place), and
    ``promote_dead_letter_messages`` (re-send to the main queue BEFORE removing
    from the DLQ, so a crash never loses a message). A fake SDK client/receiver/
    sender is injected via ``service_bus._client``; no live Service Bus.
Edit boundaries: Service-layer DLQ helpers only; the HTTP routes are covered by
    ``test_settings_service_bus_dlq.py``.
Key entry points: the ``test_*`` functions.
Risky contracts: targeted by ``sequence_number``; bounded; promote re-sends
    before completing so the message is never lost (the idempotent drain handler
    dedupes any duplicate on ``external_correlation_id``).
Validation: ``uv run pytest -q api/tests/test_service_bus_dlq.py``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from api.services import service_bus
from api.services.service_bus import ServiceBusConfig


class _FakeDlqMessage:
    def __init__(
        self,
        message_id: str,
        sequence_number: int,
        body: dict[str, Any],
        *,
        dead_letter_reason: str | None = "max_delivery_count_exceeded",
        dead_letter_error_description: str | None = "OpenAPI submit failed",
        delivery_count: int | None = 10,
    ) -> None:
        self.message_id = message_id
        self.sequence_number = sequence_number
        self.correlation_id = f"corr-{message_id}"
        self.subject = "blast.request"
        self.content_type = "application/json"
        self.enqueued_time_utc = None
        self.application_properties: dict[str, Any] = {"request_id": f"req-{message_id}"}
        self.dead_letter_reason = dead_letter_reason
        self.dead_letter_error_description = dead_letter_error_description
        self.delivery_count = delivery_count
        self._raw = json.dumps(body).encode("utf-8")

    @property
    def body(self):
        return [self._raw]


class _FakeDlqReceiver:
    """Peek-lock DLQ receiver: abandoned messages reappear, completed are gone."""

    def __init__(self, messages: list[_FakeDlqMessage]) -> None:
        self._available = list(messages)
        self.completed: list[int] = []
        self.abandoned: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def peek_messages(self, max_message_count: int):
        return self._available[:max_message_count]

    def receive_messages(self, max_message_count: int, max_wait_time: int):
        batch = self._available[:max_message_count]
        self._available = self._available[max_message_count:]
        return batch

    def complete_message(self, message: _FakeDlqMessage) -> None:
        self.completed.append(message.sequence_number)

    def abandon_message(self, message: _FakeDlqMessage) -> None:
        self.abandoned.append(message.sequence_number)
        # Abandon makes it receivable again (real-broker behaviour).
        self._available.append(message)


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def send_messages(self, message: Any) -> None:
        self.sent.append(message)


class _FakeDlqClient:
    def __init__(self, receiver: _FakeDlqReceiver, sender: _FakeSender | None = None) -> None:
        self._receiver = receiver
        self._sender = sender or _FakeSender()

    def get_queue_receiver(self, *_a: Any, **_k: Any) -> _FakeDlqReceiver:
        return self._receiver

    def get_queue_sender(self, *_a: Any, **_k: Any) -> _FakeSender:
        return self._sender


def _cfg() -> ServiceBusConfig:
    return ServiceBusConfig(
        enabled=True, auth_mode="entra", namespace_fqdn="x.servicebus.windows.net"
    )


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    receiver: _FakeDlqReceiver,
    sender: _FakeSender | None = None,
) -> _FakeDlqClient:
    fake = _FakeDlqClient(receiver, sender)

    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield fake

    monkeypatch.setattr(service_bus, "_client", fake_client)
    return fake


# --------------------------------------------------------------------------- #
# peek
# --------------------------------------------------------------------------- #


def test_dlq_peek_surfaces_reason_and_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeDlqReceiver(
        [_FakeDlqMessage("m1", 101, {"program": "blastn", "db": "core_nt"})]
    )
    _patch_client(monkeypatch, receiver)

    previews = service_bus.peek_dead_letter_previews(_cfg(), max_count=10)
    assert len(previews) == 1
    p = previews[0]
    assert p["sequence_number"] == 101
    assert p["program"] == "blastn"
    assert p["dead_letter_reason"] == "max_delivery_count_exceeded"
    assert p["dead_letter_error_description"] == "OpenAPI submit failed"
    assert p["delivery_count"] == 10
    # peek must not settle anything.
    assert receiver.completed == []
    assert receiver.abandoned == []


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


def test_dlq_delete_targets_only_requested_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeDlqReceiver(
        [
            _FakeDlqMessage("m1", 101, {"db": "core_nt"}),
            _FakeDlqMessage("m2", 102, {"db": "16S"}),
            _FakeDlqMessage("m3", 103, {"db": "nt"}),
        ]
    )
    _patch_client(monkeypatch, receiver)

    stats = service_bus.delete_dead_letter_messages(_cfg(), sequence_numbers=[102])
    assert stats.deleted == 1
    assert stats.matched == 1
    assert receiver.completed == [102]
    # The two non-targets are abandoned (left in place), never completed.
    assert 101 not in receiver.completed
    assert 103 not in receiver.completed


def test_dlq_delete_empty_list_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeDlqReceiver([_FakeDlqMessage("m1", 101, {"db": "core_nt"})])
    _patch_client(monkeypatch, receiver)

    stats = service_bus.delete_dead_letter_messages(_cfg(), sequence_numbers=[])
    assert stats.deleted == 0
    assert stats.scanned == 0
    assert receiver.completed == []


def test_dlq_delete_stops_once_all_targets_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    # 102 is first; once matched the loop must stop fetching FURTHER batches
    # rather than draining the whole backlog. A receiver that yields one message
    # per batch makes the per-batch early-stop observable.
    class _OnePerBatchReceiver(_FakeDlqReceiver):
        def receive_messages(self, max_message_count: int, max_wait_time: int):
            return super().receive_messages(1, max_wait_time)

    receiver = _OnePerBatchReceiver(
        [
            _FakeDlqMessage("m2", 102, {"db": "16S"}),
            _FakeDlqMessage("m1", 101, {"db": "core_nt"}),
            _FakeDlqMessage("m3", 103, {"db": "nt"}),
        ]
    )
    _patch_client(monkeypatch, receiver)

    stats = service_bus.delete_dead_letter_messages(_cfg(), sequence_numbers=[102])
    assert stats.deleted == 1
    # Only the first batch (one message) was scanned before all targets matched;
    # the loop stops instead of scanning 101 / 103.
    assert stats.scanned == 1


# --------------------------------------------------------------------------- #
# promote
# --------------------------------------------------------------------------- #


def test_dlq_promote_resends_then_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeDlqReceiver(
        [
            _FakeDlqMessage("m1", 101, {"db": "core_nt", "external_correlation_id": "ext-1"}),
            _FakeDlqMessage("m2", 102, {"db": "16S"}),
        ]
    )
    sender = _FakeSender()
    _patch_client(monkeypatch, receiver, sender)

    stats = service_bus.promote_dead_letter_messages(_cfg(), sequence_numbers=[101])
    assert stats.promoted == 1
    assert stats.matched == 1
    # Re-sent to the main queue exactly once, and removed from the DLQ.
    assert len(sender.sent) == 1
    assert receiver.completed == [101]
    # The re-sent message preserves identity so the drain handler dedupes.
    assert sender.sent[0].correlation_id == "corr-m1"


def test_dlq_promote_keeps_message_when_resend_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeDlqReceiver([_FakeDlqMessage("m1", 101, {"db": "core_nt"})])

    class _BoomSender(_FakeSender):
        def send_messages(self, message: Any) -> None:
            raise RuntimeError("namespace unreachable")

    sender = _BoomSender()
    _patch_client(monkeypatch, receiver, sender)

    stats = service_bus.promote_dead_letter_messages(_cfg(), sequence_numbers=[101])
    # Re-send failed → message stays in the DLQ (abandoned), never completed.
    assert stats.promoted == 0
    assert stats.failed == 1
    assert receiver.completed == []
    assert 101 in receiver.abandoned


def test_dlq_promote_empty_list_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeDlqReceiver([_FakeDlqMessage("m1", 101, {"db": "core_nt"})])
    sender = _FakeSender()
    _patch_client(monkeypatch, receiver, sender)

    stats = service_bus.promote_dead_letter_messages(_cfg(), sequence_numbers=[])
    assert stats.promoted == 0
    assert sender.sent == []
    assert receiver.completed == []

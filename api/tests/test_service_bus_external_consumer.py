"""Tests for the optional external completion consumer.

Responsibility: Verify the default-OFF gate, that ``consume_completions``
    delivers parsed events + completes messages, exits on the stop event,
    backs off on error, and the worker launcher single-flights.
Edit boundaries: Uses a fake ServiceBusClient/receiver; no live namespace.
Key entry points: ``service_bus_external_consumer``.
Risky contracts: the loop must settle each message, honour stop promptly, and
    never raise out of the loop body.
Validation: ``uv run pytest -q api/tests/test_service_bus_external_consumer.py``.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import api.services.service_bus_external_consumer as ec
import pytest


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    ec.reset_external_consumer_state_for_test()
    yield
    ec.stop_external_consumer(timeout=2.0)
    ec.reset_external_consumer_state_for_test()


class _FakeMessage:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = json.dumps(body).encode()


class _FakeReceiver:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = messages
        self.completed: list[_FakeMessage] = []
        self.abandoned: list[_FakeMessage] = []

    def __enter__(self) -> _FakeReceiver:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def receive_messages(self, **_k: object) -> list[_FakeMessage]:
        batch = self._messages
        self._messages = []  # only deliver once
        return batch

    def complete_message(self, m: _FakeMessage) -> None:
        self.completed.append(m)

    def abandon_message(self, m: _FakeMessage) -> None:
        self.abandoned.append(m)


class _FakeClient:
    def __init__(self, receiver: _FakeReceiver) -> None:
        self._receiver = receiver

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def get_subscription_receiver(self, **_k: object) -> _FakeReceiver:
        return self._receiver

    def get_queue_receiver(self, **_k: object) -> _FakeReceiver:
        return self._receiver


def _patch_client(monkeypatch: pytest.MonkeyPatch, receiver: _FakeReceiver) -> None:
    """Patch ``azure.servicebus.ServiceBusClient`` with a fake supporting both
    the credential constructor and ``from_connection_string``."""

    class _FakeClientFactory:
        def __init__(self, *_a: object, **_k: object) -> None:
            self._receiver = receiver

        @classmethod
        def from_connection_string(cls, *_a: object, **_k: object) -> _FakeClient:
            return _FakeClient(receiver)

        def __enter__(self) -> _FakeClient:
            return _FakeClient(receiver)

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr("azure.servicebus.ServiceBusClient", _FakeClientFactory)


def test_gate_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ec.EXTERNAL_CONSUMER_ENV, raising=False)
    assert ec.external_consumer_enabled() is False


def test_gate_requires_env_and_sb_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ec.EXTERNAL_CONSUMER_ENV, "true")
    monkeypatch.setattr(
        "api.services.service_bus_pref.service_bus_enabled", lambda: False
    )
    assert ec.external_consumer_enabled() is False
    monkeypatch.setattr(
        "api.services.service_bus_pref.service_bus_enabled", lambda: True
    )
    assert ec.external_consumer_enabled() is True


def test_consume_delivers_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    msgs = [_FakeMessage({"status": "succeeded", "external_correlation_id": "a"})]
    receiver = _FakeReceiver(msgs)
    _patch_client(monkeypatch, receiver)
    received: list[tuple[dict[str, Any], str]] = []
    delivered = ec.consume_completions(
        namespace_fqdn="ns.servicebus.windows.net",
        topic="t",
        subscription="s",
        on_event=lambda e, sub: received.append((e, sub)),
        connection_string="Endpoint=sb://x/;SharedAccessKeyName=k;SharedAccessKey=v",
        max_iterations=1,
    )
    assert delivered == 1
    assert received[0][0]["external_correlation_id"] == "a"
    assert received[0][1] == "s"
    assert receiver.completed == msgs


def test_consume_stops_on_event(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _FakeReceiver([])
    _patch_client(monkeypatch, receiver)
    stop = threading.Event()
    stop.set()  # already stopped → loop body never runs
    delivered = ec.consume_completions(
        namespace_fqdn="ns.servicebus.windows.net",
        topic="t",
        subscription="s",
        on_event=lambda _e, _s: None,
        connection_string="conn",
        stop=stop,
    )
    assert delivered == 0


def test_consume_requires_entities() -> None:
    with pytest.raises(ValueError):
        ec.consume_completions(
            namespace_fqdn="",
            topic="t",
            subscription="s",
            on_event=lambda _e, _s: None,
            connection_string="conn",
        )


def test_run_external_consumer_skips_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.service_bus_pref import ServiceBusConfig

    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config",
        lambda: ServiceBusConfig(),  # no namespace/topic
    )
    assert ec.run_external_consumer(threading.Event()) == 0


def test_consume_queue_kind_uses_queue_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    """``kind=queue`` reads the completion entity as a queue (point-to-point);
    the subscription is ignored and a queue receiver is used."""
    msgs = [_FakeMessage({"status": "running", "external_correlation_id": "q1"})]
    receiver = _FakeReceiver(msgs)
    _patch_client(monkeypatch, receiver)
    received: list[tuple[dict[str, Any], str]] = []
    delivered = ec.consume_completions(
        namespace_fqdn="ns.servicebus.windows.net",
        topic="elastic-blast-completions",
        subscription="",  # ignored in queue mode
        on_event=lambda e, sub: received.append((e, sub)),
        connection_string="conn",
        max_iterations=1,
        kind="queue",
    )
    assert delivered == 1
    assert received[0][0]["external_correlation_id"] == "q1"
    # In queue mode the label is the queue (entity) name, not a subscription.
    assert received[0][1] == "elastic-blast-completions"
    assert receiver.completed == msgs


def test_consume_topic_kind_requires_subscription() -> None:
    """A topic completion entity still requires a subscription name."""
    with pytest.raises(ValueError):
        ec.consume_completions(
            namespace_fqdn="ns.servicebus.windows.net",
            topic="t",
            subscription="",
            on_event=lambda _e, _s: None,
            connection_string="conn",
            kind="topic",
        )


def test_run_external_consumer_disabled_in_queue_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In queue mode the in-deployment demo observer must NOT run (it would
    compete with the real external consumer for messages)."""
    from api.services.service_bus_pref import ServiceBusConfig

    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config",
        lambda: ServiceBusConfig(
            enabled=True,
            namespace_fqdn="ns.servicebus.windows.net",
            completion_topic="elastic-blast-completions",
            completion_kind="queue",
        ),
    )

    def _boom(**_k: object) -> int:
        raise AssertionError("consume_completions must not run in queue mode")

    monkeypatch.setattr(ec, "consume_completions", _boom)
    assert ec.run_external_consumer(threading.Event()) == 0


def _patch_multi_client(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[str | None, Any]
) -> None:
    """Patch ServiceBusClient so each ``subscription_name`` maps to its own fake
    receiver (or an ``Exception`` instance to raise on receiver creation)."""

    class _MultiClient:
        def __enter__(self) -> _MultiClient:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def get_subscription_receiver(
            self, *, subscription_name: str | None = None, **_k: object
        ) -> Any:
            value = mapping[subscription_name]
            if isinstance(value, Exception):
                raise value
            return value

        def get_queue_receiver(self, **_k: object) -> Any:
            value = mapping[None]
            if isinstance(value, Exception):
                raise value
            return value

    class _Factory:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        @classmethod
        def from_connection_string(cls, *_a: object, **_k: object) -> _MultiClient:
            return _MultiClient()

        def __enter__(self) -> _MultiClient:
            return _MultiClient()

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr("azure.servicebus.ServiceBusClient", _Factory)


def test_completion_subscriptions_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default includes the shared "default" subscription so it is drained.
    monkeypatch.delenv(ec.SUBSCRIPTION_ENV, raising=False)
    assert ec.completion_subscriptions() == list(ec.DEFAULT_SUBSCRIPTIONS)
    assert "default" in ec.completion_subscriptions()
    # Comma-separated override, blank-trimmed and order-preserving de-duped.
    monkeypatch.setenv(ec.SUBSCRIPTION_ENV, " a , b ,a, , c ")
    assert ec.completion_subscriptions() == ["a", "b", "c"]
    # completion_subscription() returns the first (primary) one.
    assert ec.completion_subscription() == "a"


def test_consume_multi_subscription_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both subscriptions are drained in one tick and each event is labelled
    with the subscription it came from (fan-out: same event_id on each)."""
    r_default = _FakeReceiver([_FakeMessage({"event_id": "e1", "status": "queued"})])
    r_obs = _FakeReceiver([_FakeMessage({"event_id": "e1", "status": "queued"})])
    _patch_multi_client(monkeypatch, {"default": r_default, "playground-observer": r_obs})
    received: list[tuple[dict[str, Any], str]] = []
    delivered = ec.consume_completions(
        namespace_fqdn="ns.servicebus.windows.net",
        topic="elastic-blast-completions",
        subscriptions=["default", "playground-observer"],
        on_event=lambda e, sub: received.append((e, sub)),
        connection_string="conn",
        max_iterations=1,
    )
    assert delivered == 2
    assert sorted(sub for _e, sub in received) == ["default", "playground-observer"]
    assert r_default.completed and r_obs.completed


def test_consume_skips_missing_subscription_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscription that does not exist is retired after one WARN; the other
    subscription keeps draining."""
    from azure.servicebus.exceptions import MessagingEntityNotFoundError

    good = _FakeReceiver([_FakeMessage({"event_id": "e2", "status": "running"})])
    _patch_multi_client(
        monkeypatch,
        {"missing": MessagingEntityNotFoundError(message="nope"), "default": good},
    )
    received: list[tuple[dict[str, Any], str]] = []
    delivered = ec.consume_completions(
        namespace_fqdn="ns.servicebus.windows.net",
        topic="elastic-blast-completions",
        subscriptions=["missing", "default"],
        on_event=lambda e, sub: received.append((e, sub)),
        connection_string="conn",
        max_iterations=1,
    )
    assert delivered == 1
    assert received[0][1] == "default"


def test_consume_stops_when_all_subscriptions_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every configured subscription is permanently gone the loop exits
    (it does not spin forever)."""
    from azure.servicebus.exceptions import MessagingEntityNotFoundError

    _patch_multi_client(
        monkeypatch,
        {
            "a": MessagingEntityNotFoundError(message="nope"),
            "b": MessagingEntityNotFoundError(message="nope"),
        },
    )
    received: list[tuple[dict[str, Any], str]] = []
    delivered = ec.consume_completions(
        namespace_fqdn="ns.servicebus.windows.net",
        topic="elastic-blast-completions",
        subscriptions=["a", "b"],
        on_event=lambda e, sub: received.append((e, sub)),
        connection_string="conn",
        max_iterations=100,  # would spin if the loop did not self-terminate
    )
    assert delivered == 0
    assert received == []

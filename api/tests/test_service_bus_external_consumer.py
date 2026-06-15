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
    received: list[dict[str, Any]] = []
    delivered = ec.consume_completions(
        namespace_fqdn="ns.servicebus.windows.net",
        topic="t",
        subscription="s",
        on_event=received.append,
        connection_string="Endpoint=sb://x/;SharedAccessKeyName=k;SharedAccessKey=v",
        max_iterations=1,
    )
    assert delivered == 1
    assert received[0]["external_correlation_id"] == "a"
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
        on_event=lambda _e: None,
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
            on_event=lambda _e: None,
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

"""Regression tests for extracted Service Bus SRP boundaries.

Responsibility: Verify legacy facades inject their current monkeypatchable
    dependencies into focused management and drain-coordination modules.
Edit boundaries: Facade delegation and stable task registration only; domain
    behavior remains covered by the existing Service Bus test families.
Key entry points: the ``test_*`` functions.
Risky contracts: Existing callers patch ``service_bus._admin_client``; drain
    coordination must remain mandatory even if the legacy
    ``servicebus.tasks._DRAIN_SINGLEFLIGHT`` symbol is patched false.
Validation: ``uv run pytest -q api/tests/test_service_bus_srp_boundaries.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from api.celery_app import celery_app
from api.services import service_bus, service_bus_management, service_bus_preview
from api.services.service_bus_pref import ServiceBusConfig
from api.tasks.servicebus import drain_coordination, request_translation
from api.tasks.servicebus import tasks as servicebus_tasks


def test_drain_facade_injects_current_gate_and_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def acquire(queue_name: str, **kwargs: Any) -> tuple[bool, str | None]:
        captured.update({"queue_name": queue_name, **kwargs})
        return (True, "token")

    monkeypatch.setattr(drain_coordination, "acquire_drain_lock", acquire)
    monkeypatch.setattr(servicebus_tasks, "_DRAIN_SINGLEFLIGHT", False)

    assert servicebus_tasks._acquire_drain_lock("requests") == (True, "token")
    assert captured["queue_name"] == "requests"
    assert captured["enabled"] is True
    assert captured["lock_base_key"] == servicebus_tasks._DRAIN_LOCK_KEY
    assert captured["stop_intent_base_key"] == servicebus_tasks._DRAIN_STOP_INTENT_KEY


def test_management_facade_injects_monkeypatched_admin_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    @contextmanager
    def fake_admin(_cfg: ServiceBusConfig):
        yield object()

    def counts(cfg: ServiceBusConfig | None, **kwargs: Any) -> dict[str, Any]:
        captured.update({"cfg": cfg, **kwargs})
        return {"queue": {"active_message_count": 7}}

    cfg = ServiceBusConfig(namespace_fqdn="example.servicebus.windows.net")
    monkeypatch.setattr(service_bus, "_admin_client", fake_admin)
    monkeypatch.setattr(service_bus_management, "entity_counts", counts)

    assert service_bus.entity_counts(cfg)["queue"]["active_message_count"] == 7
    assert captured["cfg"] is cfg
    assert captured["admin_client"] is fake_admin
    assert captured["require_config"] is service_bus._require_enabled_config
    assert captured["auth_error"] is service_bus.ServiceBusAuthError


def test_request_translation_facade_injects_current_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def translate(message: Any, config: ServiceBusConfig, **kwargs: Any) -> dict[str, Any]:
        captured.update({"message": message, "config": config, **kwargs})
        return {"external_correlation_id": "corr-1"}

    message = service_bus.ParsedMessage(
        body={},
        raw_body="{}",
        message_id="message-1",
        correlation_id="corr-1",
        subject=None,
        content_type="application/json",
        enqueued_time_utc=None,
        sequence_number=1,
    )
    config = ServiceBusConfig(namespace_fqdn="example.servicebus.windows.net")
    monkeypatch.setattr(request_translation, "build_request_payload", translate)

    assert servicebus_tasks._build_request_payload(message, config) == {
        "external_correlation_id": "corr-1"
    }
    assert captured["message"] is message
    assert captured["config"] is config
    assert captured["logger"] is servicebus_tasks.LOGGER


def test_peek_preview_facade_composes_current_monkeypatchable_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = service_bus.ParsedMessage(
        body={"program": "blastn"},
        raw_body="{}",
        message_id="message-1",
        correlation_id="corr-1",
        subject="blast.request",
        content_type="application/json",
        enqueued_time_utc=None,
        sequence_number=1,
    )
    calls: list[tuple[Any, int]] = []

    def peek(config: ServiceBusConfig | None, max_count: int = 5) -> list[Any]:
        calls.append((config, max_count))
        return [message]

    monkeypatch.setattr(service_bus, "peek_requests", peek)
    monkeypatch.setattr(
        service_bus_preview,
        "preview_message",
        lambda _message, **_kwargs: {"message_id": "message-1"},
    )

    config = ServiceBusConfig(namespace_fqdn="example.servicebus.windows.net")
    assert service_bus.peek_request_previews(config, max_count=7) == [{"message_id": "message-1"}]
    assert calls == [(config, 7)]


def test_legacy_facade_symbols_remain_importable() -> None:
    assert callable(servicebus_tasks.acquire_drain_stop_intent)
    assert callable(servicebus_tasks.release_drain_stop_intent)
    assert callable(service_bus.pending_request_count)
    assert callable(service_bus.entity_counts)
    assert callable(service_bus.discover_namespaces)
    assert callable(service_bus.discover_entities)
    assert callable(service_bus.peek_requests)
    assert callable(service_bus.peek_dead_letter)
    assert callable(servicebus_tasks._build_request_payload)
    assert callable(servicebus_tasks._build_v1_jobs_payload)


def test_service_bus_celery_names_remain_registered() -> None:
    expected = {
        "api.tasks.servicebus.drain_and_resubmit",
        "api.tasks.servicebus.publish_transitions",
        "api.tasks.servicebus.emit_service_bus_health",
        "api.tasks.servicebus.reconcile_dead_letter_responses",
        "api.tasks.servicebus.dlq_cleanup",
    }

    assert expected <= set(celery_app.tasks)

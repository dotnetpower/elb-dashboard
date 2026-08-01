"""Regression tests for extracted Service Bus SRP boundaries.

Responsibility: Verify legacy facades inject their current monkeypatchable
    dependencies into focused management and drain-coordination modules.
Edit boundaries: Facade delegation and stable task registration only; domain
    behavior remains covered by the existing Service Bus test families.
Key entry points: the ``test_*`` functions.
Risky contracts: Existing callers patch ``service_bus._admin_client`` and
    ``servicebus.tasks._DRAIN_SINGLEFLIGHT``; extraction must not bypass those
    seams or rename registered Celery tasks.
Validation: ``uv run pytest -q api/tests/test_service_bus_srp_boundaries.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from api.celery_app import celery_app
from api.services import service_bus, service_bus_management
from api.services.service_bus_pref import ServiceBusConfig
from api.tasks.servicebus import drain_coordination
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
    assert captured["enabled"] is False
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


def test_legacy_facade_symbols_remain_importable() -> None:
    assert callable(servicebus_tasks.acquire_drain_stop_intent)
    assert callable(servicebus_tasks.release_drain_stop_intent)
    assert callable(service_bus.pending_request_count)
    assert callable(service_bus.entity_counts)
    assert callable(service_bus.discover_namespaces)
    assert callable(service_bus.discover_entities)


def test_service_bus_celery_names_remain_registered() -> None:
    expected = {
        "api.tasks.servicebus.drain_and_resubmit",
        "api.tasks.servicebus.publish_transitions",
        "api.tasks.servicebus.emit_service_bus_health",
        "api.tasks.servicebus.reconcile_dead_letter_responses",
        "api.tasks.servicebus.dlq_cleanup",
    }

    assert expected <= set(celery_app.tasks)

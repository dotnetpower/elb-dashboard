"""Tests for the Service Bus entity-name env overrides.

Responsibility: Verify ``SERVICEBUS_REQUEST_QUEUE`` / ``SERVICEBUS_RESPONSE_TOPIC``
    override the saved/default entity names when set and well-formed, are
    ignored when malformed, and leave the config untouched when unset.
Edit boundaries: Pure config behaviour — no SDK, no routes.
Key entry points: ``get_service_bus_config`` via ``service_bus_pref``.
Risky contracts: An unset env preserves existing behaviour (charter §12a Rule 4);
    a malformed env value never silently repoints the integration.
Validation: ``uv run pytest -q api/tests/test_service_bus_env_override.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _local_backend(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SERVICEBUS_REQUEST_QUEUE", raising=False)
    monkeypatch.delenv("SERVICEBUS_RESPONSE_TOPIC", raising=False)
    monkeypatch.delenv("SERVICEBUS_COMPLETION_KIND", raising=False)


def test_unset_env_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.service_bus_pref import (
        DEFAULT_COMPLETION_TOPIC,
        DEFAULT_REQUEST_QUEUE,
        get_service_bus_config,
    )

    cfg = get_service_bus_config()
    assert cfg.request_queue == DEFAULT_REQUEST_QUEUE
    assert cfg.completion_topic == DEFAULT_COMPLETION_TOPIC


def test_env_overrides_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICEBUS_REQUEST_QUEUE", "custom-requests")
    monkeypatch.setenv("SERVICEBUS_RESPONSE_TOPIC", "custom-completions")
    from api.services.service_bus_pref import get_service_bus_config

    cfg = get_service_bus_config()
    assert cfg.request_queue == "custom-requests"
    assert cfg.completion_topic == "custom-completions"


def test_env_override_wins_over_saved_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.service_bus_pref import (
        ServiceBusConfig,
        get_service_bus_config,
        save_service_bus_config,
    )

    save_service_bus_config(
        ServiceBusConfig(
            enabled=True,
            namespace_fqdn="ns.servicebus.windows.net",
            request_queue="saved-queue",
            completion_topic="saved-topic",
        )
    )
    monkeypatch.setenv("SERVICEBUS_REQUEST_QUEUE", "env-queue")
    cfg = get_service_bus_config()
    assert cfg.request_queue == "env-queue"  # env wins
    assert cfg.completion_topic == "saved-topic"  # unset env preserves saved


def test_malformed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICEBUS_REQUEST_QUEUE", "bad name with spaces!!")
    from api.services.service_bus_pref import DEFAULT_REQUEST_QUEUE, get_service_bus_config

    cfg = get_service_bus_config()
    assert cfg.request_queue == DEFAULT_REQUEST_QUEUE  # malformed ignored


def test_completion_kind_defaults_to_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.service_bus_pref import (
        DEFAULT_COMPLETION_KIND,
        completion_is_queue,
        get_service_bus_config,
    )

    cfg = get_service_bus_config()
    assert cfg.completion_kind == DEFAULT_COMPLETION_KIND == "topic"
    assert completion_is_queue(cfg) is False


def test_completion_kind_env_override_to_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICEBUS_COMPLETION_KIND", "QUEUE")  # case-insensitive
    from api.services.service_bus_pref import completion_is_queue, get_service_bus_config

    cfg = get_service_bus_config()
    assert cfg.completion_kind == "queue"
    assert completion_is_queue(cfg) is True


def test_completion_kind_malformed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICEBUS_COMPLETION_KIND", "pubsub")
    from api.services.service_bus_pref import get_service_bus_config

    cfg = get_service_bus_config()
    assert cfg.completion_kind == "topic"  # unrecognised value ignored

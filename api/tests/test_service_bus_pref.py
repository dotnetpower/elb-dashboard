"""Tests for the Service Bus integration config row (service_bus_pref).

Responsibility: Verify the disabled default, validation rules (FQDN/entity/SAS
    secret), request-only blank completion topics, bound clamping for the
    cleanup policy, the three-state env override in ``service_bus_enabled``
    (unset/truthy defer to config, falsy is a kill switch), and the
    file-backend round trip.
Edit boundaries: Persistence + config validation only.
Key entry points: the ``test_*`` functions.
Risky contracts: ``enabled`` must default False; ``service_bus_enabled`` must
    treat the saved+namespaced config as the source of truth, with the env var
    only able to force OFF (kill switch) — never to activate without the config.
Validation: ``uv run pytest -q api/tests/test_service_bus_pref.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _file_backend(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))


def test_default_is_disabled() -> None:
    from api.services.service_bus_pref import get_service_bus_config

    cfg = get_service_bus_config()
    assert cfg.enabled is False
    assert cfg.namespace_fqdn == ""
    assert cfg.request_queue == "elastic-blast-requests"
    assert cfg.completion_topic == "elastic-blast-completions"


def test_round_trip_file_backend() -> None:
    from api.services.service_bus_pref import (
        ServiceBusConfig,
        get_service_bus_config,
        save_service_bus_config,
    )

    save_service_bus_config(
        ServiceBusConfig(
            enabled=True,
            auth_mode="entra",
            namespace_fqdn="sb-elb-dashboard-krc.servicebus.windows.net",
        )
    )
    loaded = get_service_bus_config()
    assert loaded.enabled is True
    assert loaded.namespace_fqdn == "sb-elb-dashboard-krc.servicebus.windows.net"


def test_explicit_blank_completion_topic_is_preserved() -> None:
    from api.services.service_bus_pref import normalise_config

    cfg = normalise_config(
        {
            "enabled": True,
            "auth_mode": "entra",
            "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
            "request_queue": "elastic-blast-requests",
            "completion_topic": "",
        }
    )
    assert cfg.completion_topic == ""


def test_normalise_rejects_bad_fqdn_when_enabled() -> None:
    from api.services.service_bus_pref import normalise_config

    with pytest.raises(ValueError, match="namespace_fqdn"):
        normalise_config({"enabled": True, "namespace_fqdn": "not-a-host"})


def test_normalise_requires_sas_secret_in_sas_mode() -> None:
    from api.services.service_bus_pref import normalise_config

    with pytest.raises(ValueError, match="sas_secret_name"):
        normalise_config(
            {
                "enabled": True,
                "auth_mode": "sas",
                "namespace_fqdn": "ext.servicebus.windows.net",
                "sas_secret_name": "",
            }
        )


def test_cleanup_bounds_are_clamped() -> None:
    from api.services.service_bus_pref import ServiceBusConfig

    cfg = ServiceBusConfig.from_dict(
        {"dlq_max_age_days": 99999, "dlq_max_count": -5, "dlq_cleanup_batch": 100000}
    )
    assert cfg.dlq_max_age_days == 365  # ceil
    assert cfg.dlq_max_count == 1  # floored to low bound
    assert cfg.dlq_cleanup_batch == 2000  # ceil


def test_service_bus_enabled_three_state_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """SERVICEBUS_ENABLED is a three-state deploy-time override of the saved
    config: unset/truthy defer to the config (runtime feature flag), explicit
    falsy is a kill switch. The config (enabled + namespace) is the source of
    truth; the env never bypasses it."""
    from api.services.service_bus_pref import (
        ServiceBusConfig,
        save_service_bus_config,
        service_bus_enabled,
        service_bus_env_override,
        service_bus_kill_switch_on,
    )

    save_service_bus_config(
        ServiceBusConfig(enabled=True, namespace_fqdn="x.servicebus.windows.net")
    )
    # Env unset -> defer to config -> enabled. This is the runtime feature flag:
    # the config lives in the Table, so it survives redeploys (the gate is no
    # longer reset to a revision-baked env default).
    monkeypatch.delenv("SERVICEBUS_ENABLED", raising=False)
    assert service_bus_env_override() is None
    assert service_bus_enabled() is True
    # Explicit truthy -> still defers to config (config required) -> enabled.
    monkeypatch.setenv("SERVICEBUS_ENABLED", "true")
    assert service_bus_env_override() is True
    assert service_bus_enabled() is True
    # Explicit falsy -> deployment kill switch forces OFF regardless of config.
    monkeypatch.setenv("SERVICEBUS_ENABLED", "false")
    assert service_bus_env_override() is False
    assert service_bus_kill_switch_on() is True
    assert service_bus_enabled() is False


def test_service_bus_enabled_requires_config_even_when_env_truthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-OFF preserved (charter 12a Rule 4): a truthy/unset env never
    activates without the saved config opting in (enabled + namespace)."""
    from api.services.service_bus_pref import (
        ServiceBusConfig,
        save_service_bus_config,
        service_bus_enabled,
    )

    # Enabled but no namespace -> not live even with env truthy.
    save_service_bus_config(ServiceBusConfig(enabled=True, namespace_fqdn=""))
    monkeypatch.setenv("SERVICEBUS_ENABLED", "true")
    assert service_bus_enabled() is False
    # Config disabled + env unset -> OFF (fresh-deployment default-OFF).
    save_service_bus_config(ServiceBusConfig(enabled=False, namespace_fqdn=""))
    monkeypatch.delenv("SERVICEBUS_ENABLED", raising=False)
    assert service_bus_enabled() is False


def test_public_dict_has_no_secret_value() -> None:
    from api.services.service_bus_pref import ServiceBusConfig

    cfg = ServiceBusConfig(auth_mode="sas", sas_secret_name="sb-conn")
    pub = cfg.public_dict()
    # Only the secret NAME is surfaced; there is no connection-string field.
    assert pub["sas_secret_name"] == "sb-conn"
    assert "connection_string" not in pub
    assert "sas_connection_string" not in pub

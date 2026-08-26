"""Tests for the Settings → Service Bus HTTP routes.

Responsibility: Verify GET returns a disabled default (never 404), PUT persists
    and validates, the SAS connection string is never returned, test/discover
    degrade gracefully, and purge caps the batch.
Edit boundaries: Route shaping only; persistence + SDK behaviour covered
    elsewhere.
Key entry points: the ``test_*`` functions.
Risky contracts: every route enforces ``require_caller``; no secret material in
    responses; full-row config writes are fenced and reject stale snapshots.
Validation: ``uv run pytest -q api/tests/test_settings_service_bus.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.delenv("SERVICEBUS_ENABLED", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    from api.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _stub_entity_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the status route off the live Service Bus data plane.

    ``GET /api/settings/service-bus`` probes live entity counts whenever the
    saved config is ``enabled`` (``_runtime_counts`` \u2192 ``service_bus.entity_counts``),
    which opens a real management/AMQP connection to the namespace. No test
    here asserts on live counts (the only ``counts`` assertion is the
    ``disabled`` path, which never calls ``entity_counts``), so raise
    ``ServiceBusUnavailable`` \u2014 mirroring the real "namespace unreachable"
    outcome \u2014 instantly instead of paying the ~5 s connect/retry to the fake
    namespace (slow + flaky in CI).
    """
    from api.services import service_bus

    def _unavailable(_cfg: object) -> dict[str, object]:
        raise service_bus.ServiceBusUnavailable("stubbed in tests")

    monkeypatch.setattr(service_bus, "entity_counts", _unavailable)
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.acquire_config_mutation",
        lambda: (True, "settings-mutation-fence"),
    )
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.release_config_mutation",
        lambda _token: None,
    )
    monkeypatch.setattr(service_bus, "acquire_config_io", lambda _cfg: "settings-io-token")
    monkeypatch.setattr(service_bus, "release_config_io", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "api.tasks.servicebus.tasks.acquire_drain_stop_intent",
        lambda _queue: (True, "settings-test-fence"),
    )
    monkeypatch.setattr(
        "api.tasks.servicebus.tasks.release_drain_stop_intent",
        lambda _queue, _token: None,
    )


def _with_current_revision(client: TestClient, payload: dict[str, object]) -> dict[str, object]:
    del client
    from api.services.service_bus_pref import get_stored_service_bus_config

    revision = get_stored_service_bus_config().revision
    return {**payload, "revision": revision}


def test_get_defaults_disabled(client: TestClient) -> None:
    r = client.get("/api/settings/service-bus")
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["enabled"] is False
    assert body["effective_enabled"] is False
    assert body["env_gate_enabled"] is False
    assert body["kill_switch_enabled"] is False
    assert body["counts"]["available"] is False


def test_env_override_three_state_in_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status payload surfaces the three-state env override so the SPA can
    explain activation: an unset env defers to the saved config (runtime feature
    flag), an explicit falsy env is a deployment kill switch, and an explicit
    truthy env pins the capability on."""
    payload = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
    }
    assert client.put("/api/settings/service-bus", json=payload).status_code == 200

    # Env unset -> defer to config -> live (the runtime feature flag).
    monkeypatch.delenv("SERVICEBUS_ENABLED", raising=False)
    body = client.get("/api/settings/service-bus").json()
    assert body["config"]["enabled"] is True
    assert body["env_gate_enabled"] is False  # not explicitly pinned on
    assert body["kill_switch_enabled"] is False
    assert body["effective_enabled"] is True  # config drives it

    # Explicit falsy -> deployment kill switch forces OFF regardless of config.
    monkeypatch.setenv("SERVICEBUS_ENABLED", "false")
    body = client.get("/api/settings/service-bus").json()
    assert body["kill_switch_enabled"] is True
    assert body["effective_enabled"] is False

    # Explicit truthy -> pinned on; config already opts in -> live.
    monkeypatch.setenv("SERVICEBUS_ENABLED", "true")
    body = client.get("/api/settings/service-bus").json()
    assert body["env_gate_enabled"] is True
    assert body["kill_switch_enabled"] is False
    assert body["effective_enabled"] is True


def test_config_rejects_target_change_while_request_backlog_exists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import service_bus

    initial = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
        "subscription_id": "sub-current",
        "resource_group": "rg-current",
        "cluster_name": "aks-current",
        "storage_account": "stcurrent",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    monkeypatch.setattr(
        service_bus,
        "entity_counts",
        lambda _cfg: {
            "queue": {
                "active_message_count": 2,
                "scheduled_message_count": 1,
                "dead_letter_message_count": 0,
            }
        },
    )

    response = client.put(
        "/api/settings/service-bus",
        json=_with_current_revision(client, {**initial, "cluster_name": "aks-other"}),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "servicebus_reconfigure_blocked"
    assert response.json()["pending_requests"] == 3


def test_config_rejects_request_endpoint_change_when_proposed_queue_has_work(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import service_bus

    initial = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "requests-current",
        "completion_topic": "elastic-blast-completions",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    observed_queues: list[str] = []

    def _counts(cfg: object) -> dict[str, object]:
        queue_name = str(getattr(cfg, "request_queue", ""))
        observed_queues.append(queue_name)
        return {
            "queue": {
                "active_message_count": 2 if queue_name == "requests-proposed" else 0,
                "scheduled_message_count": 0,
                "dead_letter_message_count": 0,
            }
        }

    monkeypatch.setattr(service_bus, "entity_counts", _counts)

    response = client.put(
        "/api/settings/service-bus",
        json=_with_current_revision(client, {**initial, "request_queue": "requests-proposed"}),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "servicebus_reconfigure_blocked"
    assert response.json()["pending_requests"] == 2
    assert observed_queues == ["requests-current", "requests-proposed"]


def test_config_rejects_target_change_while_drain_holds_lease(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
        "cluster_name": "aks-current",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    monkeypatch.setattr(
        "api.tasks.servicebus.tasks.acquire_drain_stop_intent",
        lambda _queue: (False, None),
    )

    response = client.put(
        "/api/settings/service-bus",
        json=_with_current_revision(client, {**initial, "cluster_name": "aks-other"}),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "servicebus_reconfigure_busy"


def test_config_rejects_concurrent_settings_writer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.acquire_config_mutation",
        lambda: (False, None),
    )

    response = client.put(
        "/api/settings/service-bus",
        json={"enabled": False},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "servicebus_config_busy"


def test_cleanup_policy_change_waits_for_active_config_io(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
        "dlq_cleanup_enabled": False,
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    monkeypatch.setattr(
        "api.tasks.servicebus.tasks.acquire_drain_stop_intent",
        lambda _queue: (False, None),
    )

    response = client.put(
        "/api/settings/service-bus",
        json=_with_current_revision(client, {**initial, "dlq_cleanup_enabled": True}),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "servicebus_reconfigure_busy"
    assert response.json()["changed_fields"] == ["dlq_cleanup_enabled"]


def test_config_releases_reconfiguration_fence_after_blocked_change(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import service_bus

    initial = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
        "cluster_name": "aks-current",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    monkeypatch.setattr(
        "api.tasks.servicebus.tasks.acquire_drain_stop_intent",
        lambda _queue: (True, "fence-token"),
    )
    released: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "api.tasks.servicebus.tasks.release_drain_stop_intent",
        lambda queue, token: released.append((queue, token)),
    )
    monkeypatch.setattr(
        service_bus,
        "entity_counts",
        lambda _cfg: {
            "queue": {
                "active_message_count": 1,
                "scheduled_message_count": 0,
                "dead_letter_message_count": 0,
            }
        },
    )

    response = client.put(
        "/api/settings/service-bus",
        json=_with_current_revision(client, {**initial, "cluster_name": "aks-other"}),
    )

    assert response.status_code == 409
    assert released == [("elastic-blast-requests", "fence-token")]


def test_config_serialization_reclassifies_stale_nonrouting_put(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import service_bus
    from api.services.service_bus_pref import (
        get_service_bus_config,
        normalise_config,
        save_service_bus_config,
    )

    initial = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
        "cluster_name": "aks-current",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200

    def _acquire_after_concurrent_save() -> tuple[bool, str]:
        concurrent = normalise_config(
            {
                **get_service_bus_config().to_dict(),
                "cluster_name": "aks-concurrent",
            }
        )
        save_service_bus_config(concurrent)
        return (True, "fence-token")

    monkeypatch.setattr(
        "api.tasks.servicebus.drain_coordination.acquire_config_mutation",
        _acquire_after_concurrent_save,
    )
    monkeypatch.setattr(
        service_bus,
        "entity_counts",
        lambda _cfg: {
            "queue": {
                "active_message_count": 1,
                "scheduled_message_count": 0,
                "dead_letter_message_count": 0,
            }
        },
    )

    response = client.put(
        "/api/settings/service-bus",
        json=_with_current_revision(client, {**initial, "dlq_max_count": 6000}),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "servicebus_config_changed"
    assert get_service_bus_config().cluster_name == "aks-concurrent"


def test_disabled_config_cannot_bypass_pending_request_guard(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import service_bus

    initial = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
        "cluster_name": "aks-current",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    assert (
        client.put(
            "/api/settings/service-bus",
            json=_with_current_revision(client, {**initial, "enabled": False}),
        ).status_code
        == 200
    )
    monkeypatch.setattr(
        service_bus,
        "entity_counts",
        lambda _cfg: {
            "queue": {
                "active_message_count": 1,
                "scheduled_message_count": 0,
                "dead_letter_message_count": 0,
            }
        },
    )

    response = client.put(
        "/api/settings/service-bus",
        json=_with_current_revision(
            client,
            {**initial, "enabled": False, "cluster_name": "aks-other"},
        ),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "servicebus_reconfigure_blocked"


def test_config_unchanged_save_does_not_require_queue_counts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import service_bus

    config = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
        "cluster_name": "aks-current",
    }
    assert (
        client.put(
            "/api/settings/service-bus",
            json=_with_current_revision(client, config),
        ).status_code
        == 200
    )
    monkeypatch.setattr(
        service_bus,
        "entity_counts",
        lambda _cfg: (_ for _ in ()).throw(AssertionError("counts must not be read")),
    )

    assert (
        client.put(
            "/api/settings/service-bus",
            json=_with_current_revision(client, config),
        ).status_code
        == 200
    )


def test_auth_change_probes_unchanged_queue_with_proposed_credential(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import service_bus

    initial = {
        "enabled": True,
        "auth_mode": "sas",
        "sas_secret_name": "old-secret",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    monkeypatch.setattr(
        "api.tasks.servicebus.tasks.acquire_drain_stop_intent",
        lambda _queue: (True, "fence-token"),
    )
    monkeypatch.setattr(
        "api.tasks.servicebus.tasks.release_drain_stop_intent",
        lambda _queue, _token: None,
    )
    observed_secrets: list[str] = []

    def _counts(cfg: object) -> dict[str, object]:
        observed_secrets.append(str(getattr(cfg, "sas_secret_name", "")))
        return {
            "queue": {
                "active_message_count": 0,
                "scheduled_message_count": 0,
                "dead_letter_message_count": 0,
            }
        }

    monkeypatch.setattr(service_bus, "entity_counts", _counts)

    response = client.put(
        "/api/settings/service-bus",
        json=_with_current_revision(client, {**initial, "sas_secret_name": "new-secret"}),
    )

    assert response.status_code == 200
    assert observed_secrets == ["new-secret"]


def test_auth_recovery_is_allowed_while_bridge_is_active(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import service_bus
    from api.services.service_bus_tracking import BridgeRecord, upsert_bridge

    initial = {
        "enabled": True,
        "auth_mode": "sas",
        "sas_secret_name": "old-secret",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    monkeypatch.setattr(
        "api.tasks.servicebus.tasks.acquire_drain_stop_intent",
        lambda _queue: (True, "fence-token"),
    )
    monkeypatch.setattr(
        "api.tasks.servicebus.tasks.release_drain_stop_intent",
        lambda _queue, _token: None,
    )
    monkeypatch.setattr(
        service_bus,
        "entity_counts",
        lambda _cfg: {
            "queue": {
                "active_message_count": 0,
                "scheduled_message_count": 0,
                "dead_letter_message_count": 0,
            }
        },
    )
    upsert_bridge(
        BridgeRecord(
            correlation_id="corr-auth-recovery",
            openapi_job_id="job-auth-recovery",
            last_status="running",
        )
    )

    response = client.put(
        "/api/settings/service-bus",
        json=_with_current_revision(client, {**initial, "sas_secret_name": "new-secret"}),
    )

    assert response.status_code == 200


def test_config_rejects_target_change_while_bridge_is_active(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import service_bus
    from api.services.service_bus_tracking import BridgeRecord, upsert_bridge

    initial = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
        "cluster_name": "aks-current",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    monkeypatch.setattr(
        service_bus,
        "entity_counts",
        lambda _cfg: {
            "queue": {
                "active_message_count": 0,
                "scheduled_message_count": 0,
                "dead_letter_message_count": 0,
            }
        },
    )
    upsert_bridge(
        BridgeRecord(
            correlation_id="corr-active-config",
            openapi_job_id="job-active-config",
            last_status="running",
        )
    )

    response = client.put(
        "/api/settings/service-bus",
        json=_with_current_revision(client, {**initial, "cluster_name": "aks-other"}),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "servicebus_reconfigure_blocked"
    assert response.json()["active_bridges"] == 1


def test_put_then_get_round_trip(client: TestClient) -> None:
    payload = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
    }
    r = client.put("/api/settings/service-bus", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "saved"

    g = client.get("/api/settings/service-bus")
    assert g.json()["config"]["namespace_fqdn"] == payload["namespace_fqdn"]
    assert g.json()["config"]["revision"]


def test_put_rejects_stale_config_revision(client: TestClient) -> None:
    initial = client.put(
        "/api/settings/service-bus",
        json={"enabled": False},
    ).json()["config"]
    first = client.put(
        "/api/settings/service-bus",
        json={**initial, "dlq_max_count": 6000},
    )
    assert first.status_code == 200

    stale = client.put(
        "/api/settings/service-bus",
        json={**initial, "dlq_cleanup_enabled": True},
    )

    assert stale.status_code == 409
    assert stale.json()["code"] == "servicebus_config_changed"
    current = client.get("/api/settings/service-bus").json()["config"]
    assert current["dlq_max_count"] == 6000
    assert current["dlq_cleanup_enabled"] is False


def test_put_upgrades_legacy_revisionless_config_once(client: TestClient) -> None:
    from api.services.service_bus_pref import ServiceBusConfig, save_service_bus_config

    save_service_bus_config(
        ServiceBusConfig(
            enabled=False,
            request_queue="requests-legacy",
            revision="",
        )
    )

    upgraded = client.put(
        "/api/settings/service-bus",
        json={"enabled": False, "request_queue": "requests-legacy"},
    )
    assert upgraded.status_code == 200
    assert upgraded.json()["config"]["revision"]

    stale_legacy = client.put(
        "/api/settings/service-bus",
        json={"enabled": False, "request_queue": "requests-legacy"},
    )
    assert stale_legacy.status_code == 409
    assert stale_legacy.json()["code"] == "servicebus_config_changed"


def test_put_does_not_persist_env_pinned_request_queue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services.service_bus_pref import get_stored_service_bus_config

    initial = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "requests-persisted",
        "completion_topic": "elastic-blast-completions",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    monkeypatch.setenv("SERVICEBUS_REQUEST_QUEUE", "requests-pinned")
    effective = client.get("/api/settings/service-bus").json()["config"]
    assert effective["request_queue"] == "requests-pinned"

    response = client.put(
        "/api/settings/service-bus",
        json={**effective, "dlq_max_count": 6000},
    )

    assert response.status_code == 200
    assert response.json()["config"]["request_queue"] == "requests-pinned"
    assert get_stored_service_bus_config().request_queue == "requests-persisted"
    assert get_stored_service_bus_config().dlq_max_count == 6000


def test_put_does_not_persist_env_pinned_completion_entity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services.service_bus_pref import get_stored_service_bus_config

    initial = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "completions-persisted",
        "completion_kind": "topic",
    }
    assert client.put("/api/settings/service-bus", json=initial).status_code == 200
    monkeypatch.setenv("SERVICEBUS_RESPONSE_TOPIC", "completions-pinned")
    monkeypatch.setenv("SERVICEBUS_COMPLETION_KIND", "queue")
    effective = client.get("/api/settings/service-bus").json()["config"]
    assert effective["completion_topic"] == "completions-pinned"
    assert effective["completion_kind"] == "queue"

    response = client.put(
        "/api/settings/service-bus",
        json={**effective, "dlq_max_count": 6000},
    )

    assert response.status_code == 200
    assert response.json()["config"]["completion_topic"] == "completions-pinned"
    assert response.json()["config"]["completion_kind"] == "queue"
    stored = get_stored_service_bus_config()
    assert stored.completion_topic == "completions-persisted"
    assert stored.completion_kind == "topic"


def test_put_allows_request_only_blank_completion_topic(client: TestClient) -> None:
    payload = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "",
    }
    r = client.put("/api/settings/service-bus", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["config"]["completion_topic"] == ""

    g = client.get("/api/settings/service-bus")
    assert g.status_code == 200, g.text
    assert g.json()["config"]["completion_topic"] == ""


def test_put_rejects_invalid_fqdn(client: TestClient) -> None:
    r = client.put(
        "/api/settings/service-bus",
        json={"enabled": True, "namespace_fqdn": "not-a-host"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_config"


def test_put_never_returns_connection_string(client: TestClient) -> None:
    r = client.put(
        "/api/settings/service-bus",
        json={
            "enabled": True,
            "auth_mode": "sas",
            "namespace_fqdn": "ext.servicebus.windows.net",
            "sas_secret_name": "sb-conn",
        },
    )
    assert r.status_code == 200, r.text
    text = r.text.lower()
    assert "sharedaccesskey" not in text
    assert "connection_string" not in text


def test_test_route_requires_namespace(client: TestClient) -> None:
    r = client.post("/api/settings/service-bus/test", json={})
    assert r.status_code == 400
    assert r.json()["code"] == "not_configured"


def test_discover_requires_subscription_or_namespace(client: TestClient) -> None:
    r = client.post("/api/settings/service-bus/discover", json={})
    assert r.status_code == 400
    assert r.json()["code"] == "subscription_required"


# --------------------------------------------------------------------------- #
# Playground send / drain / observed-completions
# --------------------------------------------------------------------------- #

_VALID_SEND_BODY = {
    "query_fasta": ">seq1\nACGTACGTACGTACGTACGT\n",
    "db": "core_nt",
    "program": "blastn",
}


def _enable_service_bus(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICEBUS_ENABLED", "true")
    payload = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
    }
    assert client.put("/api/settings/service-bus", json=payload).status_code == 200


def test_send_rejected_when_disabled(client: TestClient) -> None:
    r = client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY)
    assert r.status_code == 409
    assert r.json()["code"] == "disabled"


def test_send_dry_run_works_when_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation is independent of the data plane — a dry run must succeed even
    when the integration is OFF (compose/verify offline)."""
    from api.services import service_bus

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("send_request must not be called on dry_run")

    monkeypatch.setattr(service_bus, "send_request", _boom)
    # No _enable_service_bus — integration disabled.
    r = client.post("/api/settings/service-bus/send", json={**_VALID_SEND_BODY, "dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "valid"
    assert body["dry_run"] is True


def test_send_rejected_when_queue_full(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A backlog at/over the ceiling returns 429 before enqueueing."""
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    monkeypatch.setattr(
        service_bus,
        "entity_counts",
        lambda _cfg: {"queue": {"active_message_count": 2000, "scheduled_message_count": 0}},
    )

    def _must_not_send(*_a: object, **_k: object) -> str:
        raise AssertionError("send_request must not run when queue is full")

    monkeypatch.setattr(service_bus, "send_request", _must_not_send)
    r = client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY)
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["code"] == "queue_full"
    assert body["limit"] == 2000
    assert body["backlog"] == 2000


def test_send_allowed_just_under_ceiling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backlog under the ceiling enqueues normally."""
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    monkeypatch.setattr(
        service_bus,
        "entity_counts",
        lambda _cfg: {"queue": {"active_message_count": 1999, "scheduled_message_count": 0}},
    )
    monkeypatch.setattr(service_bus, "send_request", lambda *_a, **_k: "msg-ok")
    r = client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"


# --------------------------------------------------------------------------- #
# Fail-closed backpressure (SERVICEBUS_SEND_FAILCLOSED). Default fails OPEN on a
# counts-read failure; gate-on fails CLOSED only after a sustained streak.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_capacity_streak():
    # The fail-closed streak is module-global; reset around every test so one
    # test's induced failures never leak into the next.
    from api.routes.settings.service_bus import _reset_capacity_failure_streak

    _reset_capacity_failure_streak()
    yield
    _reset_capacity_failure_streak()


def test_send_fail_open_by_default_on_counts_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate off (default): a counts failure fails OPEN — the send proceeds even
    on repeated failures, so a transient admin-plane blip never blocks sends."""
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    def _boom(_cfg: object) -> dict:
        raise RuntimeError("no manage claim")

    monkeypatch.setattr(service_bus, "entity_counts", _boom)
    monkeypatch.setattr(service_bus, "send_request", lambda *_a, **_k: "msg-ok")
    for _ in range(5):
        r = client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY)
        assert r.status_code == 200, r.text


def test_send_fail_closed_after_consecutive_failures(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate on: a SUSTAINED counts outage (>= streak threshold) fails CLOSED."""
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    monkeypatch.setenv("SERVICEBUS_SEND_FAILCLOSED", "true")
    monkeypatch.setenv("SERVICEBUS_SEND_FAILCLOSED_STREAK", "3")

    def _boom(_cfg: object) -> dict:
        raise RuntimeError("admin plane down")

    monkeypatch.setattr(service_bus, "entity_counts", _boom)
    monkeypatch.setattr(service_bus, "send_request", lambda *_a, **_k: "msg-ok")
    # First two failures stay under the threshold → still fail open (200).
    assert client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY).status_code == 200
    assert client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY).status_code == 200
    # Third consecutive failure reaches the threshold → fail closed (503).
    r = client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY)
    assert r.status_code == 503, r.text
    assert r.json()["code"] == "capacity_unknown"
    assert r.json()["consecutive_failures"] >= 3
    # A Retry-After header steers the client's backoff (no thundering herd).
    assert r.headers.get("Retry-After") == "30"


def test_send_capacity_success_resets_failclosed_streak(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful counts read clears the streak so fail-closed re-arms fresh —
    a single later blip does not immediately 503."""
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    monkeypatch.setenv("SERVICEBUS_SEND_FAILCLOSED", "true")
    monkeypatch.setenv("SERVICEBUS_SEND_FAILCLOSED_STREAK", "2")
    state = {"mode": "fail"}

    def _counts(_cfg: object) -> dict:
        if state["mode"] == "fail":
            raise RuntimeError("blip")
        return {"queue": {"active_message_count": 0, "scheduled_message_count": 0}}

    monkeypatch.setattr(service_bus, "entity_counts", _counts)
    monkeypatch.setattr(service_bus, "send_request", lambda *_a, **_k: "msg-ok")
    # Fail 1 (streak=1 < 2) → open.
    assert client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY).status_code == 200
    # Success → streak reset.
    state["mode"] = "ok"
    assert client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY).status_code == 200
    # Fail again: streak=1 (reset) < 2 → still open, NOT 503.
    state["mode"] = "fail"
    assert client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY).status_code == 200


def test_send_creates_queued_placeholder(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real send writes a correlation-id ``queued`` placeholder row so the job
    is visible in Recent searches / Message Flow the instant it is enqueued."""
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    monkeypatch.setattr(service_bus, "send_request", lambda *_a, **_k: "msg-ok")
    created: list[dict] = []
    monkeypatch.setattr(
        "api.services.blast.servicebus_placeholder.create_queued_placeholder",
        lambda **kw: created.append(kw) or True,
    )

    r = client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY)
    assert r.status_code == 200, r.text
    corr = r.json()["external_correlation_id"]
    assert created, "send must create a queued placeholder"
    assert created[0]["correlation_id"] == corr
    assert created[0]["program"] == _VALID_SEND_BODY["program"]


def test_send_dry_run_skips_placeholder(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry-run validates without enqueueing, so it must NOT create a placeholder."""
    _enable_service_bus(client, monkeypatch)
    created: list[dict] = []
    monkeypatch.setattr(
        "api.services.blast.servicebus_placeholder.create_queued_placeholder",
        lambda **kw: created.append(kw) or True,
    )

    r = client.post("/api/settings/service-bus/send", json={**_VALID_SEND_BODY, "dry_run": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "valid"
    assert created == []


def test_send_dry_run_validates_without_enqueue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("send_request must not be called on dry_run")

    monkeypatch.setattr(service_bus, "send_request", _boom)
    r = client.post("/api/settings/service-bus/send", json={**_VALID_SEND_BODY, "dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "valid"
    assert body["dry_run"] is True
    assert body["external_correlation_id"]


def test_send_invalid_body_returns_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service_bus(client, monkeypatch)
    r = client.post(
        "/api/settings/service-bus/send",
        json={"db": "core_nt"},  # missing query_fasta
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "invalid_request"
    # The 400 now carries the same per-field detail the native FastAPI submit
    # route emits: a structured ``errors`` list (loc/msg/type) plus a summary
    # ``message`` that names the offending field instead of a truncated blob.
    errors = body["errors"]
    assert isinstance(errors, list) and errors
    assert any("query_fasta" in item["loc"] for item in errors)
    assert all({"loc", "msg", "type"} <= set(item) for item in errors)
    # No submitted input / ctx values leak into the field detail.
    assert all("input" not in item and "ctx" not in item for item in errors)
    assert "query_fasta" in body["message"]


def test_send_invalid_body_reports_every_failing_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple field failures each surface in ``errors`` and the summary, so a
    caller is not left guessing after fixing only the first one."""
    _enable_service_bus(client, monkeypatch)
    r = client.post(
        "/api/settings/service-bus/send",
        # program is not a valid literal AND query_fasta is missing.
        json={"db": "core_nt", "program": "not-a-program"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "invalid_request"
    locs = {item["loc"] for item in body["errors"]}
    assert "query_fasta" in locs
    assert "program" in locs


def test_format_validation_errors_masks_secrets_in_msg() -> None:
    """A validator error message that echoes a GUID/token is masked before it
    reaches the caller (defense-in-depth via the shared sanitiser). Guards the
    hardening pass: a custom validator's ValueError text is the one place caller
    field content can flow back into the 400 body."""
    from api.routes.settings.service_bus import _format_validation_errors
    from pydantic import BaseModel, ValidationError, field_validator

    secret = "11111111-2222-3333-4444-555555555555"

    class _Model(BaseModel):
        x: str

        @field_validator("x")
        @classmethod
        def _reject(cls, value: str) -> str:
            raise ValueError(f"bad value carrying subscription {secret}")

    summary = ""
    errors: list[dict[str, object]] = []
    try:
        _Model(x="anything")
    except ValidationError as exc:
        summary, errors = _format_validation_errors(exc)

    # The full GUID is abbreviated (first 8 chars + ellipsis), never echoed whole.
    assert secret not in summary
    assert errors
    assert all(secret not in str(item["msg"]) for item in errors)
    assert errors[0]["loc"] == "x"


def test_send_enqueues_and_returns_message_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    captured: dict[str, object] = {}

    def _fake_send(cfg: object, body: dict, **kwargs: object) -> str:
        captured["body"] = body
        captured["kwargs"] = kwargs
        return "msg-123"

    monkeypatch.setattr(service_bus, "send_request", _fake_send)
    r = client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["message_id"] == "msg-123"
    assert body["external_correlation_id"]
    # The enqueued payload carries the server-derived correlation id.
    sent = captured["body"]
    assert isinstance(sent, dict)
    assert sent["external_correlation_id"] == body["external_correlation_id"]
    assert sent["db"] == "core_nt"


def test_send_ambiguous_failure_returns_reusable_correlation_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    monkeypatch.setattr(
        service_bus,
        "send_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("sender response lost after transfer")
        ),
    )

    response = client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY)

    assert response.status_code == 502
    assert response.json()["code"] == "send_outcome_unknown"
    assert response.json()["external_correlation_id"]


def test_send_rejects_oversized_request_with_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    monkeypatch.setenv("SERVICEBUS_SEND_FAILCLOSED", "true")
    monkeypatch.setenv("SERVICEBUS_SEND_FAILCLOSED_STREAK", "1")

    def _capacity_must_not_run(_cfg: object) -> dict:
        raise AssertionError("oversized request must fail before capacity lookup")

    monkeypatch.setattr(service_bus, "entity_counts", _capacity_must_not_run)

    response = client.post(
        "/api/settings/service-bus/send",
        json={
            **_VALID_SEND_BODY,
            "query_fasta": ">q\n" + ("A" * service_bus._MAX_REQUEST_MESSAGE_BYTES),
        },
    )

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"


def test_send_does_not_expand_the_legacy_request_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICEBUS_ENABLED", "true")
    config = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
        "subscription_id": "sub-current",
        "resource_group": "rg-current",
        "cluster_name": "aks-current",
        "storage_account": "stcurrent",
    }
    assert client.put("/api/settings/service-bus", json=config).status_code == 200
    from api.services import service_bus

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        service_bus,
        "send_request",
        lambda _cfg, body, **_kwargs: captured.update(body=body) or "msg-target",
    )

    response = client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY)

    assert response.status_code == 200
    sent = captured["body"]
    assert isinstance(sent, dict)
    assert "subscription_id" not in sent
    assert "resource_group" not in sent
    assert "cluster_name" not in sent
    assert "storage_account" not in sent


def test_send_rejects_a_different_execution_target_before_enqueue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICEBUS_ENABLED", "true")
    config = {
        "enabled": True,
        "auth_mode": "entra",
        "namespace_fqdn": "sb-elb-dashboard-krc.servicebus.windows.net",
        "request_queue": "elastic-blast-requests",
        "completion_topic": "elastic-blast-completions",
        "subscription_id": "sub-current",
        "resource_group": "rg-current",
        "cluster_name": "aks-current",
    }
    assert client.put("/api/settings/service-bus", json=config).status_code == 200
    from api.services import service_bus

    sent: list[object] = []
    monkeypatch.setattr(
        service_bus,
        "send_request",
        lambda *_args, **_kwargs: sent.append(object()) or "wrong-target",
    )

    response = client.post(
        "/api/settings/service-bus/send",
        json={**_VALID_SEND_BODY, "subscription_id": "sub-other"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "servicebus_target_mismatch"
    assert sent == []


def test_send_preserves_blast_options_for_v1_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body with `blast_options` (the /v1/jobs shape) must survive into the
    queue message — the consumer routes it to /v1/jobs (multi-token outfmt).
    Validates the M2 critique fix: the XML model would drop blast_options."""
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    captured: dict[str, object] = {}

    def _fake_send(cfg: object, body: dict, **kwargs: object) -> str:
        captured["body"] = body
        return "msg-v1"

    monkeypatch.setattr(service_bus, "send_request", _fake_send)
    r = client.post(
        "/api/settings/service-bus/send",
        json={
            "program": "blastn",
            "db": "core_nt",
            "query_fasta": ">q1\nACGTACGTACGTACGTACGT\n",
            "blast_options": {
                "evalue": 0.05,
                "outfmt": "7 std staxids sstrand qseq sseq",
                "extra": "-word_size 28 -searchsp 32156241807668",
            },
            "resource_profile": "core_nt_safe",
        },
    )
    assert r.status_code == 200, r.text
    sent = captured["body"]
    assert isinstance(sent, dict)
    # Multi-token outfmt + extra survive (the XML model would have dropped them);
    # the dashboard appends the result-UI parity columns (sscinames/stitle/qcovs)
    # so a tabular run's Description / Scientific name / Query Cover populate.
    assert (
        sent["blast_options"]["outfmt"] == "7 std staxids sstrand qseq sseq sscinames stitle qcovs"
    )
    assert "-searchsp" in sent["blast_options"]["extra"]
    assert "options" not in sent  # the XML options object is not synthesised


def test_send_rejects_unmergeable_v1_outfmt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tabular outfmt the shard merge cannot re-rank is rejected at send."""
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    monkeypatch.setattr(service_bus, "send_request", lambda *a, **k: "nope")
    r = client.post(
        "/api/settings/service-bus/send",
        json={
            "program": "blastn",
            "db": "core_nt",
            "query_fasta": ">q1\nACGTACGTACGTACGTACGT\n",
            "blast_options": {"outfmt": "7 qseqid sseqid"},
        },
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_request"


def test_send_propagates_request_id_into_queue_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-supplied request_id is attached to the enqueued message body
    (popped before OpenAPI validation, re-added to the queue payload) and echoed
    back in the response so the consumer can carry it to the completion topic."""
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    captured: dict[str, object] = {}

    def _fake_send(cfg: object, body: dict, **kwargs: object) -> str:
        captured["body"] = body
        return "msg-rid"

    monkeypatch.setattr(service_bus, "send_request", _fake_send)
    r = client.post(
        "/api/settings/service-bus/send",
        json={**_VALID_SEND_BODY, "request_id": "req-from-ui-42"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["request_id"] == "req-from-ui-42"
    sent = captured["body"]
    assert isinstance(sent, dict)
    assert sent["request_id"] == "req-from-ui-42"


def test_send_dry_run_echoes_request_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    monkeypatch.setattr(
        service_bus,
        "send_request",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no send on dry_run")),
    )
    r = client.post(
        "/api/settings/service-bus/send",
        json={**_VALID_SEND_BODY, "request_id": "req-dry", "dry_run": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["request_id"] == "req-dry"


def test_send_maps_unavailable_to_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service_bus(client, monkeypatch)
    from api.services import service_bus

    def _unavailable(*_a: object, **_k: object) -> str:
        raise service_bus.ServiceBusUnavailable("namespace down")

    monkeypatch.setattr(service_bus, "send_request", _unavailable)
    r = client.post("/api/settings/service-bus/send", json=_VALID_SEND_BODY)
    assert r.status_code == 503
    assert r.json()["code"] == "unavailable"


def test_drain_now_rejected_when_disabled(client: TestClient) -> None:
    r = client.post("/api/settings/service-bus/drain")
    assert r.status_code == 409
    assert r.json()["code"] == "disabled"


def test_drain_now_invokes_drain_task(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_service_bus(client, monkeypatch)
    import api.tasks.servicebus.tasks as sb_tasks

    monkeypatch.setattr(sb_tasks, "drain_and_resubmit", lambda: {"received": 1, "completed": 1})
    r = client.post("/api/settings/service-bus/drain")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "drained"
    assert body["received"] == 1


def test_observed_completions_empty_when_no_consumer(client: TestClient) -> None:
    r = client.get("/api/settings/service-bus/observed-completions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["events"] == []
    assert body["consumer_enabled"] is False
    assert body["subscription"] == "playground-observer"
    # The bundled observer owns only its dedicated subscription by default and
    # never competes with an external consumer on shared "default".
    assert body["subscriptions"] == ["playground-observer"]

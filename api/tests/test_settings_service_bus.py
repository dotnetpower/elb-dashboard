"""Tests for the Settings → Service Bus HTTP routes.

Responsibility: Verify GET returns a disabled default (never 404), PUT persists
    and validates, the SAS connection string is never returned, test/discover
    degrade gracefully, and purge caps the batch.
Edit boundaries: Route shaping only; persistence + SDK behaviour covered
    elsewhere.
Key entry points: the ``test_*`` functions.
Risky contracts: every route enforces ``require_caller``; no secret material in
    responses.
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
    assert r.json()["code"] == "invalid_request"


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
    # Multi-token outfmt + extra survive (the XML model would have dropped them).
    assert sent["blast_options"]["outfmt"] == "7 std staxids sstrand qseq sseq"
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

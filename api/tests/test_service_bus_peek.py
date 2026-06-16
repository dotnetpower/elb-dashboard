"""Tests for the non-destructive Service Bus request-queue peek preview.

Responsibility: Verify ``service_bus.peek_request_previews`` shapes a peeked
    message into a sanitised, size-bounded, JSON-safe preview, and that the
    Reader-accessible ``GET /settings/service-bus/peek`` route degrades
    gracefully (not configured / disabled / auth / unavailable) and surfaces
    message content + count when reachable.
Edit boundaries: Route + preview shaping only; the underlying peek SDK loop is
    covered by ``test_service_bus_drain_loop.py``.
Key entry points: the ``test_*`` functions.
Risky contracts: peek uses the data-plane receiver (Data Receiver claim), not
    the Manage claim ``entity_counts`` needs; the route never 500s.
Validation: ``uv run pytest -q api/tests/test_service_bus_peek.py``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from api.services import service_bus
from api.services.service_bus import ServiceBusConfig
from fastapi.testclient import TestClient


class _FakeMessage:
    def __init__(self, message_id: str, body: dict[str, Any]) -> None:
        self.message_id = message_id
        self.sequence_number = 42
        self.correlation_id = "corr-1"
        self.subject = "blast.request"
        self.content_type = "application/json"
        self.enqueued_time_utc = None
        self.application_properties: dict[str, Any] = {"request_id": "req-9"}
        self.dead_letter_reason = None
        self._raw = json.dumps(body).encode("utf-8")

    @property
    def body(self):
        return [self._raw]


class _PeekReceiver:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = messages

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def peek_messages(self, max_message_count: int):
        return self._messages[:max_message_count]


class _PeekClient:
    def __init__(self, receiver: _PeekReceiver) -> None:
        self._receiver = receiver

    def get_queue_receiver(self, *_a: Any, **_k: Any) -> _PeekReceiver:
        return self._receiver


def _cfg() -> ServiceBusConfig:
    return ServiceBusConfig(
        enabled=True, auth_mode="entra", namespace_fqdn="x.servicebus.windows.net"
    )


def _patch_client(monkeypatch: pytest.MonkeyPatch, receiver: _PeekReceiver) -> None:
    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _PeekClient(receiver)

    monkeypatch.setattr(service_bus, "_client", fake_client)


def test_preview_shapes_sanitised_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _PeekReceiver(
        [
            _FakeMessage(
                "m1",
                {
                    "program": "blastn",
                    "db": "core_nt",
                    "external_correlation_id": "ext-1",
                    "query_fasta": ">q\nACGT",
                },
            )
        ]
    )
    _patch_client(monkeypatch, receiver)

    previews = service_bus.peek_request_previews(_cfg(), max_count=5)
    assert len(previews) == 1
    p = previews[0]
    assert p["message_id"] == "m1"
    assert p["program"] == "blastn"
    assert p["db"] == "core_nt"
    assert p["correlation_id"] == "ext-1"
    assert p["request_id"] == "req-9"
    assert p["body_truncated"] is False
    assert "core_nt" in p["body_preview"]


def test_preview_truncates_large_body(monkeypatch: pytest.MonkeyPatch) -> None:
    big = ">q\n" + ("ACGTACGTAC\n" * 1000)
    receiver = _PeekReceiver([_FakeMessage("m2", {"db": "core_nt", "query_fasta": big})])
    _patch_client(monkeypatch, receiver)

    previews = service_bus.peek_request_previews(_cfg(), max_count=5)
    assert previews[0]["body_truncated"] is True
    assert len(previews[0]["body_preview"]) == service_bus._PEEK_BODY_MAX_CHARS


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


def test_peek_route_not_configured(client: TestClient) -> None:
    r = client.get("/api/settings/service-bus/peek")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "not_configured"
    assert body["messages"] == []
    assert body["count"] == 0


def test_peek_route_available(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.routes.settings import service_bus as route

    cfg = _cfg()
    monkeypatch.setattr(route, "get_service_bus_config", lambda: cfg)
    monkeypatch.setattr(route, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(
        route.service_bus,
        "peek_request_previews",
        lambda _cfg, max_count=5: [{"message_id": "m1", "db": "core_nt", "body_preview": "{}"}],
    )

    r = client.get("/api/settings/service-bus/peek?limit=3")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["count"] == 1
    assert body["messages"][0]["message_id"] == "m1"


def test_peek_route_auth_failure_degrades(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.routes.settings import service_bus as route

    cfg = _cfg()
    monkeypatch.setattr(route, "get_service_bus_config", lambda: cfg)
    monkeypatch.setattr(route, "service_bus_enabled", lambda: True)

    def _raise(_cfg: Any, max_count: int = 5) -> Any:
        raise service_bus.ServiceBusAuthError("no receiver claim")

    monkeypatch.setattr(route.service_bus, "peek_request_previews", _raise)

    r = client.get("/api/settings/service-bus/peek")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "auth_failed"

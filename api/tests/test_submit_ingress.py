"""Tests for the optional Service Bus submit-ingress front door (issue #36 Tier 2).

Responsibility: Verify the default-OFF ``ENABLE_SB_SUBMIT_INGRESS`` gate, the
    enqueue helper's message shape + raise-on-failure contract, and that the
    ``/api/v1/elastic-blast/submit`` route enqueues when the gate is ON (and
    falls back to the direct path on a publish failure).
Edit boundaries: Test-only. Service Bus + OpenAPI client are mocked.
Key entry points: ``test_*``.
Risky contracts: gate requires env AND service_bus_enabled; enqueue raises on
    failure so the route can fall back; OFF gate keeps the direct path.
Validation: ``uv run pytest -q api/tests/test_submit_ingress.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_gate_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.blast import submit_ingress

    monkeypatch.delenv("ENABLE_SB_SUBMIT_INGRESS", raising=False)
    assert submit_ingress.should_enqueue_submit() is False


def test_gate_on_requires_service_bus_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.blast import submit_ingress

    monkeypatch.setenv("ENABLE_SB_SUBMIT_INGRESS", "true")
    # Gate env on but SB disabled -> still False (never drop a submit into a void).
    monkeypatch.setattr(
        "api.services.service_bus_pref.service_bus_enabled", lambda: False
    )
    assert submit_ingress.should_enqueue_submit() is False
    # Both on -> True.
    monkeypatch.setattr(
        "api.services.service_bus_pref.service_bus_enabled", lambda: True
    )
    assert submit_ingress.should_enqueue_submit() is True


def test_enqueue_request_shape_and_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import service_bus
    from api.services.blast import submit_ingress

    sent: dict = {}

    def _fake_send(cfg, body, *, correlation_id=None, **_kw):
        sent["body"] = body
        sent["correlation_id"] = correlation_id
        return "msg-123"

    monkeypatch.setattr(service_bus, "send_request", _fake_send)
    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config", lambda: object()
    )
    # Trace record is best-effort; stub the repo so it does not touch Azure.
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _NoopRepo())

    payload = {
        "query_fasta": ">q\nACGT",
        "db": "core_nt",
        "program": "blastn",
        "options": {"outfmt": 5},
        "taxid": 562,
        "is_inclusive": True,
        "ignored_extra": "drop-me",
    }
    msg_id = submit_ingress.enqueue_submit_request(payload, "corr-1")
    assert msg_id == "msg-123"
    body = sent["body"]
    assert body["external_correlation_id"] == "corr-1"
    assert body["query_fasta"] == ">q\nACGT"
    assert body["db"] == "core_nt"
    assert body["options"] == {"outfmt": 5}
    assert body["taxid"] == 562
    # Only the whitelisted keys are forwarded.
    assert "ignored_extra" not in body


def test_enqueue_raises_on_publish_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import service_bus
    from api.services.blast import submit_ingress

    def _boom(*_a, **_k):
        raise RuntimeError("sb down")

    monkeypatch.setattr(service_bus, "send_request", _boom)
    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config", lambda: object()
    )
    with pytest.raises(RuntimeError):
        submit_ingress.enqueue_submit_request({"query_fasta": ">q\nA", "db": "x"}, "corr-2")


class _NoopRepo:
    def append_history(self, *_a, **_k):
        pass


def test_submit_route_enqueues_when_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("ENABLE_SB_SUBMIT_INGRESS", "true")
    from api.main import app
    from api.services import external_blast, service_bus

    monkeypatch.setattr("api.services.service_bus_pref.service_bus_enabled", lambda: True)
    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config", lambda: object()
    )
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _NoopRepo())

    sent: dict = {}

    def _fake_send(cfg, body, *, correlation_id=None, **_kw):
        sent["body"] = body
        return "msg-xyz"

    monkeypatch.setattr(service_bus, "send_request", _fake_send)

    # The direct path MUST NOT be taken when enqueue succeeds.
    def _must_not_submit(*_a, **_k):  # pragma: no cover
        raise AssertionError("direct /v1/jobs submit must not run when enqueued")

    monkeypatch.setattr(external_blast, "submit_job", _must_not_submit)
    monkeypatch.setattr(external_blast, "ready", lambda **_k: {"ready": True})

    client = TestClient(app)
    r = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGCATGC", "db": "core_nt"},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["ingress"] == "service_bus"
    assert body["submission_source"] == "servicebus"
    assert body["status"] == "queued"
    assert sent["body"]["db"] == "core_nt"


def test_submit_route_falls_back_to_direct_on_enqueue_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("ENABLE_SB_SUBMIT_INGRESS", "true")
    from api.main import app
    from api.services import external_blast, service_bus

    monkeypatch.setattr("api.services.service_bus_pref.service_bus_enabled", lambda: True)
    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config", lambda: object()
    )

    def _boom(*_a, **_k):
        raise RuntimeError("sb down")

    monkeypatch.setattr(service_bus, "send_request", _boom)

    submitted: dict = {}

    def _direct_submit(payload, **_k):
        submitted["payload"] = payload
        return {"job_id": "openapi-fallback", "status": "queued"}

    monkeypatch.setattr(external_blast, "submit_job", _direct_submit)
    monkeypatch.setattr(external_blast, "ready", lambda **_k: {"ready": True})

    client = TestClient(app)
    r = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGCATGC", "db": "core_nt"},
    )
    assert r.status_code == 202
    # Fell back to the direct path: real OpenAPI job id, not the corr-id shape.
    assert submitted, "direct submit must run after enqueue failure"
    assert r.json().get("ingress") != "service_bus"

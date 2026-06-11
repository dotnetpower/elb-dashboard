"""Tests for the Service Bus integration Celery tasks.

Responsibility: Verify the three beat tasks no-op when disabled, the drain
    handler bridges a valid message to the OpenAPI plane (and dedups duplicates,
    dead-letters malformed ones), the transition publisher emits one event per
    status change and marks terminal rows done, and DLQ cleanup backs up before
    deleting.
Edit boundaries: Task behaviour only — the Service Bus SDK and OpenAPI client
    are mocked; tracking uses the local file backend.
Key entry points: the ``test_*`` functions.
Risky contracts: idempotency on correlation_id, bounded drain, transition
    de-dup via ``last_status``, backup-then-delete.
Validation: ``uv run pytest -q api/tests/test_servicebus_tasks.py``.
"""

from __future__ import annotations

import pytest
from api.services import external_blast, service_bus
from api.services.service_bus import MessageAction, ParsedMessage
from api.services.service_bus_pref import ServiceBusConfig
from api.tasks.servicebus import tasks as sb_tasks


@pytest.fixture(autouse=True)
def _file_backend(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))


def _enabled_cfg() -> ServiceBusConfig:
    return ServiceBusConfig(
        enabled=True,
        auth_mode="entra",
        namespace_fqdn="sb-elb-dashboard-krc.servicebus.windows.net",
    )


def _enable(monkeypatch: pytest.MonkeyPatch) -> ServiceBusConfig:
    cfg = _enabled_cfg()
    monkeypatch.setattr(sb_tasks, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(sb_tasks, "get_service_bus_config", lambda: cfg)
    return cfg


def _msg(body: dict, **kw) -> ParsedMessage:
    return ParsedMessage(
        body=body,
        raw_body="",
        message_id=kw.get("message_id"),
        correlation_id=kw.get("correlation_id"),
        subject=kw.get("subject"),
        content_type="application/json",
        enqueued_time_utc=kw.get("enqueued_time_utc"),
        sequence_number=kw.get("sequence_number"),
    )


def test_tasks_skip_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb_tasks, "service_bus_enabled", lambda: False)
    assert sb_tasks.drain_and_resubmit()["skipped"] == "disabled"
    assert sb_tasks.publish_transitions()["skipped"] == "disabled"
    assert sb_tasks.dlq_cleanup()["skipped"] == "disabled"


def test_drain_bridges_valid_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    events: list[dict] = []
    submitted: list[dict] = []

    def fake_submit(payload, **_kw):
        submitted.append(payload)
        return {"job_id": "openapi-1"}

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: events.append(e))

    def fake_drain(c, handler, *, max_messages, max_wait_seconds=5):
        action = handler(
            _msg(
                {
                    "program": "blastn",
                    "db": "core_nt",
                    "query_fasta": ">s\nACGT",
                    "external_correlation_id": "corr-1",
                }
            )
        )
        from api.services.service_bus import DrainStats

        s = DrainStats(received=1)
        if action == MessageAction.COMPLETE:
            s.completed = 1
        return s

    monkeypatch.setattr(service_bus, "drain_requests", fake_drain)

    out = sb_tasks.drain_and_resubmit()
    assert out["completed"] == 1
    assert submitted and submitted[0]["submission_source"] == "servicebus"
    assert submitted[0]["external_correlation_id"] == "corr-1"
    # queued transition published + bridge persisted
    assert events and events[0]["status"] == "queued"
    from api.services.service_bus_tracking import get_bridge

    rec = get_bridge("corr-1")
    assert rec is not None
    assert rec.openapi_job_id == "openapi-1"
    assert rec.last_status == "queued"


def test_drain_dedups_duplicate_correlation(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    from api.services.service_bus_tracking import BridgeRecord, upsert_bridge

    upsert_bridge(BridgeRecord(correlation_id="corr-dup", openapi_job_id="op-existing"))

    calls: list[dict] = []
    monkeypatch.setattr(
        external_blast, "submit_job", lambda p, **k: calls.append(p) or {"job_id": "x"}
    )

    action = sb_tasks._drain_handler(
        _msg({"program": "blastn", "db": "core_nt", "query_fasta": ">s\nACGT",
              "external_correlation_id": "corr-dup"}),
        _enabled_cfg(),
    )
    assert action == MessageAction.COMPLETE
    assert calls == []  # no second submit


def test_drain_dead_letters_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    # Missing query_fasta + db → cannot ever succeed.
    action = sb_tasks._drain_handler(
        _msg({"program": "blastn", "external_correlation_id": "corr-bad"}),
        _enabled_cfg(),
    )
    assert action == MessageAction.DEAD_LETTER


def test_message_payload_is_consistent_with_openapi_jobs_model() -> None:
    """A Service Bus message maps to the SAME shape as POST /api/v1/elastic-blast/submit.

    Guards the contract the user cares about: the queue message must round-trip
    through the OpenAPI ExternalBlastSubmitRequest model, forwarding options
    (both an explicit object and flat convenience keys) plus every top-level
    field, and stamping a server-derived submission_source.
    """
    msg = _msg(
        {
            "program": "blastn",
            "db": "core_nt",
            "query_fasta": ">s\nACGT",
            "external_correlation_id": "corr-shape",
            "options": {"max_target_seqs": 250},
            "word_size": 11,
            "evalue": 0.001,
            "taxid": 9606,
            "is_inclusive": True,
            "priority": 70,
            "idempotency_key": "idem-1",
            "resource_profile": "standard",
            # Not part of the OpenAPI contract → silently ignored, exactly as a
            # direct /v1/jobs POST would (local-only precision-sharding option).
            "searchsp": 123456789,
        }
    )
    payload = sb_tasks._build_request_payload(msg, _enabled_cfg())
    assert payload is not None
    # Server-derived metadata.
    assert payload["submission_source"] == "servicebus"
    assert payload["external_correlation_id"] == "corr-shape"
    # Options object + flat keys merged; outfmt is fixed to 5 by the model.
    assert payload["options"]["max_target_seqs"] == 250
    assert payload["options"]["word_size"] == 11
    assert payload["options"]["evalue"] == 0.001
    assert payload["options"]["outfmt"] == 5
    # Top-level fields forwarded.
    assert payload["taxid"] == 9606
    assert payload["is_inclusive"] is True
    assert payload["priority"] == 70
    assert payload["idempotency_key"] == "idem-1"
    # searchsp is NOT part of the OpenAPI model → dropped (not in options).
    assert "searchsp" not in payload
    assert "searchsp" not in payload.get("options", {})


def test_message_flat_options_only() -> None:
    """A purely flat message (no `options` object) still maps options correctly."""
    msg = _msg(
        {
            "program": "blastn",
            "db": "core_nt",
            "query_fasta": ">s\nACGT",
            "external_correlation_id": "corr-flat",
            "word_size": 28,
        }
    )
    payload = sb_tasks._build_request_payload(msg, _enabled_cfg())
    assert payload is not None
    assert payload["options"]["word_size"] == 28


def test_message_correlation_id_falls_back_to_message_id() -> None:
    """When the body omits external_correlation_id, the SB message_id is used."""
    msg = _msg(
        {"program": "blastn", "db": "core_nt", "query_fasta": ">s\nACGT"},
        message_id="sb-msg-42",
    )
    payload = sb_tasks._build_request_payload(msg, _enabled_cfg())
    assert payload is not None
    assert payload["external_correlation_id"] == "sb-msg-42"


def test_publish_transitions_emits_on_change(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    from api.services.service_bus_tracking import BridgeRecord, get_bridge, upsert_bridge

    upsert_bridge(
        BridgeRecord(correlation_id="corr-2", openapi_job_id="op-2", last_status="queued")
    )
    events: list[dict] = []
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: events.append(e))
    monkeypatch.setattr(external_blast, "get_job", lambda jid, **k: {"status": "running"})

    out = sb_tasks.publish_transitions()
    assert out["published"] == 1
    assert events[0]["status"] == "running"
    assert get_bridge("corr-2").last_status == "running"  # marker advanced

    # Second tick with the SAME status publishes nothing (de-dup).
    events.clear()
    out2 = sb_tasks.publish_transitions()
    assert out2["published"] == 0
    assert events == []


def test_publish_transitions_marks_terminal_done(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    from api.services.service_bus_tracking import BridgeRecord, get_bridge, upsert_bridge

    upsert_bridge(
        BridgeRecord(correlation_id="corr-3", openapi_job_id="op-3", last_status="running")
    )
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: None)
    monkeypatch.setattr(
        external_blast, "get_job", lambda jid, **k: {"status": "completed"}
    )

    out = sb_tasks.publish_transitions()
    assert out["finished"] == 1
    rec = get_bridge("corr-3")
    assert rec.done is True
    assert rec.last_status == "succeeded"


def test_publish_transitions_gives_up_on_stuck_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(sb_tasks, "_BRIDGE_MAX_AGE_SECONDS", 0)  # everything is "expired"
    from api.services.service_bus_tracking import BridgeRecord, get_bridge, upsert_bridge

    upsert_bridge(
        BridgeRecord(
            correlation_id="corr-stuck",
            openapi_job_id="op-stuck",
            last_status="running",
            created_at="2000-01-01T00:00:00+00:00",
        )
    )
    events: list[dict] = []
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: events.append(e))
    monkeypatch.setattr(external_blast, "get_job", lambda jid, **k: {"status": "running"})

    out = sb_tasks.publish_transitions()
    assert out["finished"] == 1
    assert events and events[0]["error_code"] == "bridge_timeout"
    assert get_bridge("corr-stuck").done is True


def test_dlq_cleanup_skips_when_policy_off(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled_cfg()
    cfg.dlq_cleanup_enabled = False
    monkeypatch.setattr(sb_tasks, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(sb_tasks, "get_service_bus_config", lambda: cfg)
    assert sb_tasks.dlq_cleanup()["skipped"] == "cleanup_disabled"


def test_dlq_cleanup_backs_up_then_purges(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled_cfg()
    cfg.dlq_cleanup_enabled = True
    cfg.dlq_max_count = 0  # force over-count → every message eligible
    monkeypatch.setattr(sb_tasks, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(sb_tasks, "get_service_bus_config", lambda: cfg)
    monkeypatch.setattr(
        service_bus, "entity_counts", lambda c: {"queue": {"dead_letter_message_count": 3}}
    )
    backed_up: list[dict] = []
    monkeypatch.setattr(
        sb_tasks, "backup_dead_letter_message", lambda rec: backed_up.append(rec) or True
    )

    from api.services.service_bus import PurgeStats

    def fake_purge(c, *, predicate, backup, max_messages):
        # Simulate one DLQ message that matches and is backed up + deleted.
        m = _msg({"corr": "x"}, message_id="m1")
        assert predicate(m) is True  # over-count → eligible
        assert backup(m) is True
        return PurgeStats(scanned=1, purged=1)

    monkeypatch.setattr(service_bus, "purge_dead_letter", fake_purge)

    out = sb_tasks.dlq_cleanup()
    assert out["purged"] == 1
    assert backed_up and backed_up[0]["message_id"] == "m1"

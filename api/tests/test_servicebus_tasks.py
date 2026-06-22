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
        application_properties=kw.get("application_properties") or {},
    )


def test_tasks_skip_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb_tasks, "service_bus_enabled", lambda: False)
    assert sb_tasks.drain_and_resubmit()["skipped"] == "disabled"
    assert sb_tasks.publish_transitions()["skipped"] == "disabled"
    assert sb_tasks.dlq_cleanup()["skipped"] == "disabled"


def test_drain_skips_tick_on_transient_infra_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A transient Table/Service Bus DNS blip must skip the tick, not crash with
    # an exception Celery cannot pickle (UnpickleableExceptionWrapper).
    from azure.core.exceptions import ServiceRequestError

    _enable(monkeypatch)

    def _boom(*_a: object, **_k: object) -> object:
        raise ServiceRequestError(
            "Failed to resolve 'x.table.core.windows.net' "
            "([Errno -3] Temporary failure in name resolution)"
        )

    monkeypatch.setattr(sb_tasks.service_bus, "drain_requests", _boom)
    out = sb_tasks.drain_and_resubmit()
    assert out["skipped"] == "transient"
    assert out["error_class"] == "ServiceRequestError"


def test_publish_skips_tick_on_transient_infra_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from azure.core.exceptions import ServiceResponseError

    _enable(monkeypatch)

    def _boom(*_a: object, **_k: object) -> object:
        raise ServiceResponseError("Connection aborted; remote end closed")

    monkeypatch.setattr(sb_tasks, "list_active_bridges", _boom)
    out = sb_tasks.publish_transitions()
    assert out["skipped"] == "transient"
    assert out["error_class"] == "ServiceResponseError"


def test_dlq_cleanup_skips_tick_on_transient_infra_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from azure.core.exceptions import ServiceRequestError

    cfg = _enable(monkeypatch)
    monkeypatch.setattr(cfg, "dlq_cleanup_enabled", True)

    def _boom(*_a: object, **_k: object) -> object:
        raise ServiceRequestError("Temporary failure in name resolution")

    monkeypatch.setattr(sb_tasks.service_bus, "purge_dead_letter", _boom)
    out = sb_tasks.dlq_cleanup()
    assert out["skipped"] == "transient"


def test_non_transient_error_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # The transient guard must not swallow genuine bugs.
    _enable(monkeypatch)

    def _boom(*_a: object, **_k: object) -> object:
        raise ValueError("genuine bug")

    monkeypatch.setattr(sb_tasks.service_bus, "drain_requests", _boom)
    with pytest.raises(ValueError, match="genuine bug"):
        sb_tasks.drain_and_resubmit()



def test_drain_bridges_valid_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    events: list[dict] = []
    submitted: list[dict] = []

    def fake_submit(payload, **_kw):
        submitted.append(payload)
        return {"job_id": "openapi-1"}

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: events.append(e))

    def fake_drain(c, handler, *, max_messages, max_wait_seconds=5, max_concurrency=1):
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


def test_drain_persists_jobstate_row_and_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The consumer is the writer: a drained message creates the durable
    jobstate row at drain time (reusing the external-jobs sync) and records the
    message-flow trace stages enqueued → received → row_created → routed →
    submitted, keyed by the OpenAPI job id."""
    _enable(monkeypatch)
    import datetime

    synced: list[tuple] = []
    history: list[tuple] = []

    def _fake_sync(rows, **kw):
        synced.append((rows, kw))
        return (len(rows), 0, set())

    class _FakeRepo:
        def append_history(self, job_id, event, payload=None):
            history.append((job_id, event, payload or {}))

    monkeypatch.setattr(
        "api.services.blast.external_jobs._sync_external_jobs_to_table", _fake_sync
    )
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _FakeRepo())
    monkeypatch.setattr(external_blast, "submit_job", lambda p, **k: {"job_id": "openapi-9"})
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: None)

    enq = datetime.datetime(2026, 6, 14, 0, 0, 0, tzinfo=datetime.UTC)

    def fake_drain(c, handler, *, max_messages, max_wait_seconds=5, max_concurrency=1):
        handler(
            _msg(
                {
                    "program": "blastn",
                    "db": "core_nt",
                    "query_fasta": ">s\nACGT",
                    "external_correlation_id": "corr-9",
                },
                enqueued_time_utc=enq,
            )
        )
        from api.services.service_bus import DrainStats

        s = DrainStats(received=1)
        s.completed = 1
        return s

    monkeypatch.setattr(service_bus, "drain_requests", fake_drain)

    sb_tasks.drain_and_resubmit()

    # Row created via the proven sync, keyed by the OpenAPI job id + shared owner.
    assert synced, "drain must create a jobstate row"
    rows, kw = synced[0]
    assert rows[0]["job_id"] == "openapi-9"
    assert rows[0]["submission_source"] == "servicebus"
    assert rows[0]["external_correlation_id"] == "corr-9"
    assert kw.get("caller_oid") == ""

    # Trace stages recorded, keyed by the OpenAPI job id.
    events = [e for (jid, e, _p) in history if jid == "openapi-9"]
    for stage in ("mf.enqueued", "mf.received", "mf.row_created", "mf.routed", "mf.submitted"):
        assert stage in events, f"missing trace stage {stage}"
    # The enqueued stage carries the real SB enqueue time, not the write time.
    enq_payload = next(p for (jid, e, p) in history if e == "mf.enqueued")
    assert enq_payload["stage_ts"].startswith("2026-06-14T00:00:00")


def test_drain_supersedes_send_time_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful drain soft-deletes the correlation-id placeholder created at
    send time, so the real OpenAPI-keyed row is the only one left in the list."""
    _enable(monkeypatch)
    superseded: list[str] = []
    monkeypatch.setattr(
        "api.services.blast.servicebus_placeholder.supersede_placeholder",
        lambda cid: superseded.append(cid),
    )
    # Keep the row-persist path inert without string-patching the facade helper
    # (patch its dependencies instead, mirroring test_drain_persists_*).
    monkeypatch.setattr(
        "api.services.blast.external_jobs._sync_external_jobs_to_table",
        lambda rows, **kw: (len(rows), 0, set()),
    )

    class _FakeRepo:
        def append_history(self, job_id, event, payload=None):
            return None

    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _FakeRepo())
    monkeypatch.setattr(external_blast, "submit_job", lambda p, **k: {"job_id": "openapi-x"})
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: None)

    def fake_drain(c, handler, *, max_messages, max_wait_seconds=5, max_concurrency=1):
        handler(
            _msg(
                {
                    "program": "blastn",
                    "db": "core_nt",
                    "query_fasta": ">s\nACGT",
                    "external_correlation_id": "corr-sup",
                }
            )
        )
        from api.services.service_bus import DrainStats

        return DrainStats(received=1)

    monkeypatch.setattr(service_bus, "drain_requests", fake_drain)

    sb_tasks.drain_and_resubmit()
    assert superseded == ["corr-sup"]


def test_drain_fails_placeholder_on_permanent_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A permanent 4xx submit rejection (dead-letter) terminalises the send-time
    placeholder so it does not linger as ``queued`` after the message is DLQ'd."""
    _enable(monkeypatch)
    from fastapi import HTTPException

    failed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "api.services.blast.servicebus_placeholder.fail_placeholder",
        lambda cid, *, error_code: failed.append((cid, error_code)),
    )

    def _reject(payload, **_kw):
        raise HTTPException(status_code=400, detail="bad option")

    monkeypatch.setattr(external_blast, "submit_job", _reject)

    actions: list = []

    def fake_drain(c, handler, *, max_messages, max_wait_seconds=5, max_concurrency=1):
        actions.append(
            handler(
                _msg(
                    {
                        "program": "blastn",
                        "db": "core_nt",
                        "query_fasta": ">s\nACGT",
                        "external_correlation_id": "corr-rej",
                    }
                )
            )
        )
        from api.services.service_bus import DrainStats

        return DrainStats(received=1)

    monkeypatch.setattr(service_bus, "drain_requests", fake_drain)

    sb_tasks.drain_and_resubmit()
    assert actions == [MessageAction.DEAD_LETTER]
    assert failed and failed[0][0] == "corr-rej"
    assert failed[0][1].startswith("servicebus_submit_rejected_400")


def test_drain_fails_placeholder_on_malformed_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message whose payload cannot be built is dead-lettered AND its
    placeholder is failed (recovered from the raw body's correlation id)."""
    _enable(monkeypatch)
    failed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "api.services.blast.servicebus_placeholder.fail_placeholder",
        lambda cid, *, error_code: failed.append((cid, error_code)),
    )

    def fake_drain(c, handler, *, max_messages, max_wait_seconds=5, max_concurrency=1):
        # No query_fasta / db → _build_request_payload returns None.
        handler(_msg({"external_correlation_id": "corr-bad"}))
        from api.services.service_bus import DrainStats

        return DrainStats(received=1)

    monkeypatch.setattr(service_bus, "drain_requests", fake_drain)

    sb_tasks.drain_and_resubmit()
    assert failed and failed[0][0] == "corr-bad"
    assert failed[0][1] == "servicebus_malformed_request"


def test_publish_transitions_records_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A published transition records the status stage + completion_published on
    the job's message trace, keyed by the OpenAPI job id."""
    _enable(monkeypatch)
    from api.services.service_bus_tracking import BridgeRecord, upsert_bridge

    upsert_bridge(
        BridgeRecord(
            correlation_id="corr-t", openapi_job_id="openapi-t", last_status="queued", done=False
        )
    )
    monkeypatch.setattr(external_blast, "get_job", lambda jid, **k: {"status": "running"})
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: None)

    history: list[tuple] = []

    class _FakeRepo:
        def append_history(self, job_id, event, payload=None):
            history.append((job_id, event))

    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _FakeRepo())

    sb_tasks.publish_transitions()

    events = [e for (jid, e) in history if jid == "openapi-t"]
    assert "mf.running" in events
    assert "mf.completion_published" in events


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


def test_drain_dead_letters_on_permanent_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sibling 4xx (permanent rejection) dead-letters immediately instead of
    abandoning, so a request the sibling will always reject does not burn the
    whole delivery count re-POSTing it."""
    from fastapi import HTTPException

    _enable(monkeypatch)

    def _reject(_p, **_k):
        raise HTTPException(400, detail={"code": "openapi_http_400"})

    monkeypatch.setattr(external_blast, "submit_job", _reject)
    action = sb_tasks._drain_handler(
        _msg(
            {
                "program": "blastn",
                "db": "core_nt",
                "query_fasta": ">s\nACGT",
                "external_correlation_id": "corr-4xx",
            }
        ),
        _enabled_cfg(),
    )
    assert action == MessageAction.DEAD_LETTER


def test_drain_abandons_on_transient_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sibling 5xx / 503 transport error is transient → abandon for redelivery."""
    from fastapi import HTTPException

    _enable(monkeypatch)

    def _unavailable(_p, **_k):
        raise HTTPException(503, detail={"code": "openapi_unreachable"})

    monkeypatch.setattr(external_blast, "submit_job", _unavailable)
    action = sb_tasks._drain_handler(
        _msg(
            {
                "program": "blastn",
                "db": "core_nt",
                "query_fasta": ">s\nACGT",
                "external_correlation_id": "corr-5xx",
            }
        ),
        _enabled_cfg(),
    )
    assert action == MessageAction.ABANDON


def test_drain_abandons_on_retryable_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """408/429 are retryable 4xx → abandon, not dead-letter."""
    from fastapi import HTTPException

    _enable(monkeypatch)

    def _rate_limited(_p, **_k):
        raise HTTPException(429, detail={"code": "rate_limited"})

    monkeypatch.setattr(external_blast, "submit_job", _rate_limited)
    action = sb_tasks._drain_handler(
        _msg(
            {
                "program": "blastn",
                "db": "core_nt",
                "query_fasta": ">s\nACGT",
                "external_correlation_id": "corr-429",
            }
        ),
        _enabled_cfg(),
    )
    assert action == MessageAction.ABANDON


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
            "sharding_mode": "precise",
        }
    )
    payload = sb_tasks._build_request_payload(msg, _enabled_cfg())
    assert payload is not None
    # Server-derived metadata.
    assert payload["submission_source"] == "servicebus"
    assert payload["external_correlation_id"] == "corr-shape"
    # core_nt with a missing/standard profile is promoted to the sharding
    # default so the sibling builds a sharded (memory-fitting) config.
    assert payload["resource_profile"] == "core_nt_safe"
    # Options object + flat keys merged; outfmt is fixed to 5 by the model.
    assert payload["options"]["max_target_seqs"] == 250
    assert payload["options"]["word_size"] == 11
    assert payload["options"]["evalue"] == 0.001
    assert payload["options"]["outfmt"] == 5
    assert payload["options"]["sharding_mode"] == "precise"
    assert payload["options"]["db_effective_search_space"] == 32_156_241_807_668
    # Top-level fields forwarded.
    assert payload["taxid"] == 9606
    assert payload["is_inclusive"] is True
    assert payload["priority"] == 70
    assert payload["idempotency_key"] == "idem-1"
    assert "searchsp" not in payload


def test_message_payload_downgrades_bad_precise_override() -> None:
    msg = _msg(
        {
            "program": "blastn",
            "db": "core_nt",
            "query_fasta": ">s\nACGT",
            "external_correlation_id": "corr-downgrade",
            "options": {
                "outfmt": 5,
                "sharding_mode": "precise",
                "db_effective_search_space": 42,
            },
        }
    )

    payload = sb_tasks._build_request_payload(msg, _enabled_cfg())

    assert payload is not None
    assert payload["options"]["sharding_mode"] == "approximate"
    assert "db_effective_search_space" not in payload["options"]


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


def test_event_id_is_deterministic_per_corr_status() -> None:
    """The completion event_id is stable for the same (corr, status) and differs
    across status — so an external subscriber can dedupe idempotently."""
    a = sb_tasks._event_id("corr-1", "succeeded")
    b = sb_tasks._event_id("corr-1", "succeeded")
    c = sb_tasks._event_id("corr-1", "failed")
    d = sb_tasks._event_id("corr-2", "succeeded")
    assert a == b
    assert a != c
    assert a != d
    assert len(a) == 32


def test_transition_event_carries_idempotency_keys() -> None:
    ev = sb_tasks._transition_event(
        correlation_id="corr-9",
        openapi_job_id="op-9",
        status="succeeded",
        attempt=1,
    )
    assert ev["event"] == "blast.transition"
    assert ev["event_id"] == sb_tasks._event_id("corr-9", "succeeded")
    assert ev["attempt"] == 1
    assert ev["external_correlation_id"] == "corr-9"
    assert ev["status"] == "succeeded"
    # result_ref carries pointers only (never result bytes; charter §9).
    assert "result_ref" in ev and "api" in ev["result_ref"]
    # No request_id supplied → key omitted (envelope stays clean downstream).
    assert "request_id" not in ev


def test_extract_request_id_body_then_props_then_missing() -> None:
    # Body wins.
    assert (
        sb_tasks._extract_request_id(_msg({"request_id": "  rid-body  "})) == "rid-body"
    )
    # Falls back to the message application property.
    assert (
        sb_tasks._extract_request_id(
            _msg({}, application_properties={"request_id": "rid-prop"})
        )
        == "rid-prop"
    )
    # Absent → empty string.
    assert sb_tasks._extract_request_id(_msg({"db": "core_nt"})) == ""
    # Non-string is coerced and length-bounded.
    long_value = "x" * 1000
    out = sb_tasks._extract_request_id(_msg({"request_id": long_value}))
    assert out == "x" * sb_tasks._REQUEST_ID_MAX_LEN


def test_transition_event_includes_request_id_when_present() -> None:
    ev = sb_tasks._transition_event(
        correlation_id="corr-r",
        openapi_job_id="op-r",
        status="running",
        attempt=1,
        request_id="req-xyz",
    )
    assert ev["request_id"] == "req-xyz"
    # request_id must NOT change the dedup digest (constant per correlation id).
    assert ev["event_id"] == sb_tasks._event_id("corr-r", "running")


def _job_with_files() -> dict:
    """A sibling job detail carrying two normalisable result files."""
    return {
        "status": "completed",
        "result": {
            "files": [
                {
                    "file_id": "merged_results.out.gz",
                    "filename": "merged_results.out.gz",
                    "format": "blast_tabular",
                    "size_bytes": 12345,
                },
                {
                    "file_id": "metadata.json",
                    "filename": "metadata.json",
                    "format": "unknown",
                    "size_bytes": 678,
                },
            ]
        },
    }


def test_result_files_for_event_builds_download_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services import control_plane_url

    monkeypatch.setattr(
        control_plane_url,
        "resolve_control_plane_url",
        lambda: ("https://ca-elb-dashboard.example.com", "container_app"),
    )
    files = sb_tasks._result_files_for_event(_job_with_files(), "op-7")
    assert len(files) == 2
    first = files[0]
    assert first["file_id"] == "merged_results.out.gz"
    assert first["name"] == "merged_results.out.gz"
    assert first["format"] == "blast_tabular"
    assert first["size"] == 12345
    # download_url targets the dashboard's authenticated streaming gateway, NOT
    # a Storage SAS URL (charter §9).
    assert first["download_url"] == (
        "https://ca-elb-dashboard.example.com"
        "/api/v1/elastic-blast/jobs/op-7/files/merged_results.out.gz"
    )
    assert "blob.core.windows.net" not in first["download_url"]
    assert "sig=" not in first["download_url"]


def test_result_files_for_event_omits_url_when_base_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services import control_plane_url

    monkeypatch.setattr(
        control_plane_url, "resolve_control_plane_url", lambda: ("", "none")
    )
    files = sb_tasks._result_files_for_event(_job_with_files(), "op-8")
    assert len(files) == 2
    # No public base → metadata still emitted, download_url omitted so the
    # subscriber falls back to result_ref.
    assert "download_url" not in files[0]
    assert files[0]["file_id"] == "merged_results.out.gz"


def test_result_files_for_event_empty_when_no_files() -> None:
    assert sb_tasks._result_files_for_event({"status": "completed"}, "op-9") == []


def test_transition_event_includes_result_files_when_supplied() -> None:
    files = [{"file_id": "f1", "download_url": "https://d/x"}]
    ev = sb_tasks._transition_event(
        correlation_id="corr-rf",
        openapi_job_id="op-rf",
        status="succeeded",
        attempt=1,
        result_files=files,
    )
    assert ev["result_files"] == files
    # result_files must NOT change the dedup digest.
    assert ev["event_id"] == sb_tasks._event_id("corr-rf", "succeeded")


def test_transition_event_omits_result_files_when_none() -> None:
    ev = sb_tasks._transition_event(
        correlation_id="corr-x",
        openapi_job_id="op-x",
        status="running",
        attempt=1,
    )
    assert "result_files" not in ev


def test_publish_transitions_succeeded_attaches_download_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A succeeded transition carries result_files with download URLs."""
    _enable(monkeypatch)
    from api.services import control_plane_url
    from api.services.service_bus_tracking import BridgeRecord, upsert_bridge

    upsert_bridge(
        BridgeRecord(
            correlation_id="corr-dl", openapi_job_id="op-dl", last_status="running"
        )
    )
    events: list[dict] = []
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: events.append(e))
    monkeypatch.setattr(external_blast, "get_job", lambda jid, **k: _job_with_files())
    monkeypatch.setattr(
        control_plane_url,
        "resolve_control_plane_url",
        lambda: ("https://ca-elb-dashboard.example.com", "container_app"),
    )

    out = sb_tasks.publish_transitions()
    assert out["finished"] == 1
    assert events[0]["status"] == "succeeded"
    files = events[0]["result_files"]
    assert files[0]["download_url"].endswith(
        "/api/v1/elastic-blast/jobs/op-dl/files/merged_results.out.gz"
    )


def test_drain_propagates_request_id_to_bridge_and_queued_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied request_id on the queue message is persisted on the
    bridge row and echoed onto the initial queued completion event."""
    _enable(monkeypatch)
    events: list[dict] = []
    monkeypatch.setattr(external_blast, "submit_job", lambda p, **k: {"job_id": "op-rid"})
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: events.append(e))

    action = sb_tasks._drain_handler(
        _msg(
            {
                "program": "blastn",
                "db": "core_nt",
                "query_fasta": ">s\nACGT",
                "external_correlation_id": "corr-rid",
                "request_id": "req-trace-7",
            }
        ),
        _enabled_cfg(),
    )
    assert action == MessageAction.COMPLETE
    from api.services.service_bus_tracking import get_bridge

    rec = get_bridge("corr-rid")
    assert rec is not None and rec.request_id == "req-trace-7"
    assert events and events[0]["status"] == "queued"
    assert events[0]["request_id"] == "req-trace-7"


def test_publish_transitions_echoes_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transition publisher echoes the persisted request_id on every
    subsequent event it emits to the completion topic."""
    _enable(monkeypatch)
    from api.services.service_bus_tracking import BridgeRecord, upsert_bridge

    upsert_bridge(
        BridgeRecord(
            correlation_id="corr-echo",
            openapi_job_id="op-echo",
            last_status="queued",
            request_id="req-echo-9",
        )
    )
    events: list[dict] = []
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: events.append(e))
    monkeypatch.setattr(external_blast, "get_job", lambda jid, **k: {"status": "running"})

    out = sb_tasks.publish_transitions()
    assert out["published"] == 1
    assert events[0]["status"] == "running"
    assert events[0]["request_id"] == "req-echo-9"


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


def test_publish_transitions_idle_skips_openapi_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With zero active bridges the tick must NOT resolve OpenAPI client kwargs.

    Resolving them reads the configured cluster's ``elb-openapi`` Service IP
    from the Kubernetes API; on a stopped / recreated cluster that read raises a
    ConnectionError the OpenTelemetry instrumentation auto-records as an App
    Insights dependency exception, flooding telemetry once per 30 s tick. The
    idle path must touch nothing but the local tracking store.
    """
    _enable(monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(
        sb_tasks, "_openapi_kwargs", lambda cfg: calls.append(1) or {}
    )

    out = sb_tasks.publish_transitions()

    assert calls == [], "idle tick must not resolve OpenAPI kwargs"
    assert out == {"scanned": 0, "published": 0, "finished": 0, "errors": 0}


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


def test_publish_transitions_isolates_one_failing_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracking-store write raising on ONE bridge must not abort the tick.

    Mirrors the per-item isolation of ``drain_requests`` /
    ``reconcile_stale_jobs``: bridge B is still processed (event published, marker
    advanced) even though bridge A's ``mark_published`` raised. Regression guard
    for the partial-failure isolation in ``publish_transitions``.
    """
    _enable(monkeypatch)
    from api.services.service_bus_tracking import BridgeRecord, get_bridge, upsert_bridge

    upsert_bridge(
        BridgeRecord(correlation_id="corr-iso-1", openapi_job_id="op-iso-1", last_status="queued")
    )
    upsert_bridge(
        BridgeRecord(correlation_id="corr-iso-2", openapi_job_id="op-iso-2", last_status="queued")
    )
    events: list[dict] = []
    monkeypatch.setattr(service_bus, "publish_event", lambda c, e: events.append(e))
    monkeypatch.setattr(external_blast, "get_job", lambda jid, **k: {"status": "running"})

    real_mark = sb_tasks.mark_published

    def flaky_mark(corr: str, status: str) -> None:
        if corr == "corr-iso-1":
            raise RuntimeError("simulated tracking-store write failure")
        real_mark(corr, status)

    monkeypatch.setattr(sb_tasks, "mark_published", flaky_mark)

    out = sb_tasks.publish_transitions()
    # Both bridges were scanned; bridge A raised in mark_published (isolated),
    # bridge B completed normally and advanced its marker.
    assert out["scanned"] == 2
    assert out["errors"] == 1
    # Both events reached the topic (publish precedes the mark step); only the
    # fully-completed bridge B is counted under `published`.
    assert len(events) == 2
    assert out["published"] == 1
    assert get_bridge("corr-iso-2").last_status == "running"


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


def test_persist_result_manifest_writes_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """The succeeded transition captures file_id -> blob_path as a durable column.

    Lets the download route stream results from Storage after the cluster
    auto-stops. Only entries carrying a blob_path are stored.
    """
    import json
    from types import SimpleNamespace

    captured: dict = {}

    def fake_update(job_id: str, **kwargs):
        captured["job_id"] = job_id
        captured.update(kwargs)
        return SimpleNamespace(job_id=job_id)

    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: SimpleNamespace(update=fake_update),
    )

    job = {
        "result": {
            "files": [
                {"file_id": "result-001", "filename": "batch_000.out.gz",
                 "blob_path": "job-x/batch_000.out.gz"},
                {"file_id": "result-002", "filename": "batch_001.out.gz"},  # no blob_path → skipped
            ]
        }
    }
    sb_tasks._persist_result_manifest("op-7", job)

    assert captured["job_id"] == "op-7"
    manifest = json.loads(captured["result_manifest"])
    assert manifest == [{"file_id": "result-001", "blob_path": "job-x/batch_000.out.gz"}]


def test_persist_result_manifest_noop_without_blob_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """No blob_path on any file → no write (older sibling payloads stay proxy-only)."""
    from types import SimpleNamespace

    called = {"update": False}

    def fake_update(*_a, **_k):
        called["update"] = True

    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: SimpleNamespace(update=fake_update),
    )
    sb_tasks._persist_result_manifest(
        "op-8", {"result": {"files": [{"file_id": "result-001", "filename": "r.xml"}]}}
    )
    assert called["update"] is False

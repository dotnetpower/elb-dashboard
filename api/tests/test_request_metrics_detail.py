"""Unit tests for the per-request HTTP DETAIL inspector ring buffer.

Responsibility: Unit tests for the per-request HTTP DETAIL inspector ring buffer
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_reset_detail_buffer`, `test_redact_headers_blanks_sensitive_values_only`,
`test_capture_body_decodes_json_and_truncates_at_cap`,
`test_capture_body_refuses_binary_content_type`, `test_capture_body_empty_input_returns_none`,
`test_record_detail_persists_a_single_sample_with_redaction`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_request_metrics_detail.py`.
"""

from __future__ import annotations

import pytest
from api.services import request_metrics as rm
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_detail_buffer():
    rm.reset_details_for_tests()
    yield
    rm.reset_details_for_tests()


def test_redact_headers_blanks_sensitive_values_only():
    out = rm.redact_headers(
        [
            ("Authorization", "Bearer abcd.efgh.ijkl"),
            ("Cookie", "sid=secret"),
            ("X-Api-Key", "deadbeef"),
            ("X-Request-Id", "req_123"),
            ("User-Agent", "Mozilla/5.0"),
        ]
    )
    out_map = {k.lower(): v for k, v in out}
    assert out_map["authorization"] == rm.DETAIL_REDACT_PLACEHOLDER
    assert out_map["cookie"] == rm.DETAIL_REDACT_PLACEHOLDER
    assert out_map["x-api-key"] == rm.DETAIL_REDACT_PLACEHOLDER
    # Non-sensitive headers must survive untouched.
    assert out_map["x-request-id"] == "req_123"
    assert out_map["user-agent"] == "Mozilla/5.0"


def test_capture_body_decodes_json_and_truncates_at_cap():
    raw = b'{"a":' + (b"1," * 5000) + b'"end":true}'
    text, truncated = rm.capture_body(raw, content_type="application/json")
    assert truncated is True
    assert text is not None
    assert len(text.encode("utf-8")) <= rm.DETAIL_BODY_CAP_BYTES


def test_capture_body_refuses_binary_content_type():
    raw = bytes(range(64))
    text, truncated = rm.capture_body(raw, content_type="application/octet-stream")
    assert truncated is False
    assert text is not None and text.startswith("<binary")


def test_capture_body_empty_input_returns_none():
    text, truncated = rm.capture_body(b"", content_type="application/json")
    assert (text, truncated) == (None, False)
    text, truncated = rm.capture_body(None, content_type="application/json")
    assert (text, truncated) == (None, False)


def test_capture_body_masks_secrets_but_keeps_subscription_ids():
    """The inspector renders bodies verbatim, so a captured body must have
    bearer tokens / SAS signatures / keys masked (charter §12), while the
    caller's own subscription/tenant GUIDs stay visible for debug utility."""
    raw = (
        b'{"authorization":"Bearer abcDEF1234567890ghijklmnopqrstuvwxyz",'
        b'"sas":"https://x.blob.core.windows.net/c/b?sig=AAAABBBBCCCCDDDD1234",'
        b'"subscription_id":"b052302c-4c8d-49a4-aa2f-9d60a7301a80"}'
    )
    text, _ = rm.capture_body(raw, content_type="application/json")
    assert text is not None
    # Real secrets are masked.
    assert "abcDEF1234567890ghijklmnopqrstuvwxyz" not in text
    assert "sig=AAAABBBBCCCCDDDD1234" not in text
    # The caller's own subscription id remains visible for debugging.
    assert "b052302c-4c8d-49a4-aa2f-9d60a7301a80" in text


def test_record_detail_persists_a_single_sample_with_redaction():
    rm.record_detail(
        request_id="req_abc",
        method="POST",
        path="/api/blast/jobs",
        status=201,
        duration_ms=42.0,
        caller="alice@example.com",
        client_ip="10.0.1.5",
        request_headers=[("Authorization", "Bearer leak"), ("Content-Type", "application/json")],
        request_body=b'{"q":"hello"}',
        request_content_type="application/json",
        response_headers=[("Content-Type", "application/json"), ("Set-Cookie", "sid=leak")],
        response_body=b'{"id":"job_1"}',
        response_content_type="application/json",
        response_size_bytes=14,
        ts=1_700_000_000.0,
    )
    items = rm.details().list_recent(limit=10)
    assert len(items) == 1
    s = items[0]
    assert s["request_id"] == "req_abc"
    assert s["method"] == "POST"
    assert s["path"] == "/api/blast/jobs"
    assert s["status"] == 201
    assert s["caller"] == "alice@example.com"
    assert s["client_ip"] == "10.0.1.5"
    req_headers = {h["name"].lower(): h["value"] for h in s["request_headers"]}
    assert req_headers["authorization"] == rm.DETAIL_REDACT_PLACEHOLDER
    assert req_headers["content-type"] == "application/json"
    res_headers = {h["name"].lower(): h["value"] for h in s["response_headers"]}
    assert res_headers["set-cookie"] == rm.DETAIL_REDACT_PLACEHOLDER
    assert s["request_body"] == '{"q":"hello"}'
    assert s["response_body"] == '{"id":"job_1"}'
    assert s["response_size_bytes"] == 14


def test_record_detail_swallows_malformed_input():
    # Must not raise even with type-confused inputs.
    rm.record_detail(
        request_id="x",
        method="GET",
        path="/api/health",
        status=200,
        duration_ms=1.0,
        caller=None,
        client_ip=None,
        request_headers=[(b"badname", "v")],  # type: ignore[list-item]
        request_body=b"",
        request_content_type=None,
        response_headers=[],
        response_body=b"ok",
        response_content_type=None,
    )
    items = rm.details().list_recent(limit=10)
    # The bad header tuple is dropped by redact_headers; sample still recorded.
    assert len(items) == 1
    assert items[0]["request_headers"] == []


def test_ring_buffer_evicts_oldest_when_capacity_exceeded():
    cap = rm.details().capacity
    for i in range(cap + 5):
        rm.record_detail(
            request_id=f"req_{i:04d}",
            method="GET",
            path=f"/api/test/{i}",
            status=200,
            duration_ms=1.0,
            caller=None,
            client_ip=None,
            request_headers=[],
            request_body=None,
            request_content_type=None,
            response_headers=[],
            response_body=None,
            response_content_type=None,
        )
    items = rm.details().list_recent(limit=cap + 100)
    assert len(items) == cap
    # newest-first, so the first item should be the last recorded
    assert items[0]["request_id"] == f"req_{cap + 4:04d}"
    # the oldest surviving id is cap+5 - cap = 5 → req_0005
    assert items[-1]["request_id"] == "req_0005"


# ---------------------------------------------------------------------------
# Integration: middleware captures, route returns the buffer.
# ---------------------------------------------------------------------------
def _make_app(monkeypatch):
    """Build a minimal app that re-uses the production middleware so we
    exercise the real capture path rather than the test helpers."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("REQUEST_DETAIL_CAPTURE_ENABLED", "true")
    # Disable the cgroup reporter — it would try to talk to redis in this
    # in-process test which has no broker.
    monkeypatch.setenv("SIDECAR_REPORTER_DISABLED", "true")
    # Force re-import of the create_app factory so middleware sees the env.
    from api import main as _main

    app = _main.create_app()
    return app


def _make_app_with_default_inspector(monkeypatch):
    """Build the app with the production default inspector mode.

    The default records request metadata but avoids body buffering unless
    REQUEST_DETAIL_CAPTURE_ENABLED is explicitly true.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("REQUEST_DETAIL_CAPTURE_ENABLED", raising=False)
    monkeypatch.setenv("SIDECAR_REPORTER_DISABLED", "true")
    from api import main as _main

    app = _main.create_app()
    return app


@pytest.mark.slow
def test_middleware_captures_post_body_and_route_returns_it(monkeypatch):
    rm.reset_details_for_tests()
    app = _make_app(monkeypatch)
    client = TestClient(app)
    payload = {"q": "hello", "k": 1}
    r = client.post("/api/resources/_does_not_exist", json=payload)
    # We don't care about the response status — only that the middleware
    # captured something. (404 is fine; the route isn't real.)
    assert r.status_code in (404, 405, 422, 503)
    items = rm.details().list_recent(limit=10)
    # At least one captured sample for the POST we just sent.
    posts = [s for s in items if s["method"] == "POST" and s["path"].startswith("/api/")]
    assert posts, f"no POST captured in inspector buffer; saw {items}"
    s = posts[0]
    # Body must be the JSON we sent (Content-Length / framing intact).
    assert s["request_body"] is not None and "hello" in s["request_body"]
    # request_id should round-trip into the response and the buffer.
    assert s["request_id"]


def test_middleware_records_polling_get_metadata_by_default(monkeypatch):
    rm.reset_details_for_tests()
    app = _make_app_with_default_inspector(monkeypatch)
    client = TestClient(app)

    response = client.get("/api/me/_inspector_probe")

    assert response.status_code == 404
    items = rm.details().list_recent(limit=10)
    matches = [
        sample
        for sample in items
        if sample["method"] == "GET" and sample["path"] == "/api/me/_inspector_probe"
    ]
    assert matches
    assert matches[0]["request_id"]
    assert matches[0]["request_body"] is None
    assert matches[0]["response_body"] is None


def test_middleware_can_disable_detail_buffer(monkeypatch):
    rm.reset_details_for_tests()
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("REQUEST_DETAIL_CAPTURE_ENABLED", "false")
    monkeypatch.setenv("SIDECAR_REPORTER_DISABLED", "true")
    from api import main as _main

    app = _main.create_app()
    client = TestClient(app)

    response = client.post("/api/resources/_inspector_disabled", json={"k": 1})

    assert response.status_code in (404, 405, 422, 503)
    assert rm.details().list_recent(limit=10) == []


def test_sidecar_requests_route_returns_redacted_payload(monkeypatch):
    rm.reset_details_for_tests()
    app = _make_app(monkeypatch)
    client = TestClient(app)
    # Send a request with a fake bearer to verify Authorization is redacted.
    client.post(
        "/api/resources/_smoke",
        headers={"Authorization": "Bearer fake.jwt.token", "Content-Type": "application/json"},
        json={"k": 1},
    )
    r = client.get("/api/monitor/sidecar-requests?limit=20")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and isinstance(body["items"], list)
    assert body["capacity"] == rm.details().capacity
    assert body["count"] == len(body["items"])
    # Find the POST sample and confirm Authorization was redacted.
    posts = [s for s in body["items"] if s["method"] == "POST"]
    assert posts
    headers = {h["name"].lower(): h["value"] for h in posts[0]["request_headers"]}
    assert headers.get("authorization") == rm.DETAIL_REDACT_PLACEHOLDER


def test_middleware_captures_body_size_guard_413(monkeypatch):
    rm.reset_details_for_tests()
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("REQUEST_DETAIL_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "8")
    monkeypatch.setenv("SIDECAR_REPORTER_DISABLED", "true")
    from api import main as _main

    app = _main.create_app()
    client = TestClient(app)

    response = client.post(
        "/api/resources/_too_large",
        content=b'{"too":"large"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.headers.get("x-request-id")
    items = rm.details().list_recent(limit=10)
    matches = [
        sample
        for sample in items
        if sample["method"] == "POST" and sample["path"] == "/api/resources/_too_large"
    ]
    assert matches
    assert matches[0]["status"] == 413
    assert matches[0]["response_body"] is not None
    assert "payload_too_large" in str(matches[0]["response_body"])

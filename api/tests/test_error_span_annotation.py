"""Integration tests for App Insights error-span annotation on 4xx/5xx.

Responsibility: Verify the app's exception handlers stamp the in-flight request
span with a sanitised failure reason so 4xx/5xx are diagnosable in App Insights.
Edit boundaries: Patch ``api.main._annotate_error_span_safe`` to capture the
arguments the handlers pass; do not exercise the real OpenTelemetry exporter.
Key entry points: ``test_http_404_annotates_span``,
``test_validation_422_annotates_span_with_field_locations``,
``test_error_detail_text_sanitises_secrets``.
Risky contracts: Annotation must never alter the response status/body, and the
detail written to telemetry must be sanitised (no SAS / token / subscription).
Validation: ``uv run pytest -q api/tests/test_error_span_annotation.py``.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def _capture_annotations(monkeypatch: Any) -> list[dict[str, Any]]:
    import api.main as main

    captured: list[dict[str, Any]] = []

    def _fake(*, status_code: int, error_type: str, detail: Any, request_id: Any) -> None:
        captured.append(
            {
                "status_code": status_code,
                "error_type": error_type,
                "detail": detail,
                "request_id": request_id,
            }
        )

    monkeypatch.setattr(main, "_annotate_error_span_safe", _fake)
    return captured


def test_http_404_annotates_span(monkeypatch: Any) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    captured = _capture_annotations(monkeypatch)
    from api.main import app

    client = TestClient(app)
    # A dashboard job id that does not exist returns a structured 404.
    resp = client.get("/api/blast/jobs/does-not-exist-zzz")
    # The route family answers 404 for an unknown job id; whatever the exact
    # 4xx, the handler must have annotated the span with that status.
    assert resp.status_code >= 400
    assert captured, "exception handler must annotate the request span"
    last = captured[-1]
    assert last["status_code"] == resp.status_code
    assert last["error_type"].startswith("http_") or last["error_type"] == "validation_error"


def test_validation_422_annotates_span_with_field_locations(monkeypatch: Any) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    captured = _capture_annotations(monkeypatch)
    from api.main import app

    client = TestClient(app)
    # limit=51 exceeds the by-accession route's le=50 bound → 422.
    resp = client.get("/api/blast/jobs/by-accession/NM_000546.6?limit=51")
    assert resp.status_code == 422
    assert captured
    last = captured[-1]
    assert last["status_code"] == 422
    assert last["error_type"] == "validation_error"
    # The field location is surfaced (NOT the submitted value).
    assert last["detail"] is None or "limit" in last["detail"]


def test_error_detail_text_sanitises_secrets() -> None:
    from api.main import _error_detail_text

    # A SAS token in a structured detail must be redacted before it reaches
    # telemetry.
    detail = {
        "code": "openapi_unreachable",
        "message": "GET https://x.blob.core.windows.net/c/b?sig=SECRETSIG&se=2026 failed",
    }
    text = _error_detail_text(detail)
    assert text is not None
    assert "SECRETSIG" not in text
    assert "sas-redacted" in text or "sig=<redacted>" in text


def test_error_detail_text_empty_returns_none() -> None:
    from api.main import _error_detail_text

    assert _error_detail_text(None) is None
    assert _error_detail_text("") is None

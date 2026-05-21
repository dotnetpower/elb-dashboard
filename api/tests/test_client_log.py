"""Unit tests for browser client error log ingestion.

Responsibility: Unit tests for browser client error log ingestion
Edit boundaries: Keep assertions focused on route validation, auth, and sanitised log output.
Key entry points: `test_client_log_writes_sanitised_error_record`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_client_log.py`.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("SIDECAR_REPORTER_DISABLED", "true")
    from api.main import create_app

    return TestClient(create_app())


def test_client_log_writes_sanitised_error_record(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(monkeypatch)
    caplog.set_level(logging.ERROR, logger="api.routes.client_log")

    response = client.post(
        "/api/client-log",
        json={
            "level": "error",
            "source": "error-boundary",
            "message": "Render failed with Bearer abcdefghijklmnopqrstuvwxyz0123456789",
            "stack": "Error: bad\n    at Widget (https://example.test/app.js:1:2)",
            "component_stack": "    at Widget\n    at App",
            "url": "https://example.test/dashboard?sig=abcdefghijklmnopqrstuvwxyz0123456789",
            "user_agent": "pytest-browser",
        },
    )

    assert response.status_code == 204
    log_text = caplog.text
    assert "client_app_error" in log_text
    assert "source=error-boundary" in log_text
    assert "Bearer <redacted>" in log_text
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in log_text
    assert "at Widget at App" in log_text

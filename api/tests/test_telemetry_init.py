"""Tests for `api.app.telemetry.init_telemetry`.

Responsibility: Verify the OT distro init is a no-op without a connection
string and runs cleanly once when one is set.
Edit boundaries: Stub `configure_azure_monitor` so tests do not require an App
Insights resource.
Key entry points: `test_init_skipped_without_connection_string`,
    `test_init_calls_distro_when_connection_string_present`,
    `test_init_honors_explicit_logging_disable`,
    `test_init_honors_explicit_live_metrics_disable`.
Risky contracts: `init_telemetry` must never raise; tests assert it returns
    True/False instead of propagating exceptions.
Validation: `uv run pytest -q api/tests/test_telemetry_init.py`.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any

import pytest


def _fresh_telemetry_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    sys.modules.pop("api.app.telemetry", None)
    mod = importlib.import_module("api.app.telemetry")
    return mod


def test_init_skipped_without_connection_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    telemetry = _fresh_telemetry_module(monkeypatch)
    assert telemetry.init_telemetry("api") is False
    assert telemetry.is_initialized() is False


def test_init_calls_distro_when_connection_string_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=abc;IngestionEndpoint=https://example.local/",
    )

    calls: list[dict[str, Any]] = []

    def _fake_configure(**kwargs: Any) -> None:
        calls.append(kwargs)

    import azure.monitor.opentelemetry as distro

    monkeypatch.setattr(distro, "configure_azure_monitor", _fake_configure)

    # Block the FastAPI/Celery auto-instrumentors so this test stays decoupled.
    class _FakeInstrumentor:
        @staticmethod
        def instrument_app(app: object) -> None:
            calls.append({"instrumentor": "fastapi", "app": app})

        def instrument(self) -> None:
            calls.append({"instrumentor": "fastapi"})

    import opentelemetry.instrumentation.fastapi as fi_mod

    monkeypatch.setattr(fi_mod, "FastAPIInstrumentor", _FakeInstrumentor)

    telemetry = _fresh_telemetry_module(monkeypatch)
    app = object()
    assert telemetry.init_telemetry("api", app=app) is True
    assert telemetry.is_initialized() is True
    assert calls and "connection_string" in calls[0]
    assert calls[0]["logger_name"] == "api"
    assert calls[0]["instrumentation_options"] == {"fastapi": {"enabled": False}}
    assert calls[0]["enable_live_metrics"] is True
    assert "disable_logging" not in calls[0]
    assert calls[0]["resource"].attributes["service.name"] == "elb-api"
    assert calls[0]["resource"].attributes["service.namespace"] == "elb-dashboard"
    assert calls[1] == {"instrumentor": "fastapi", "app": app}
    # Second call must be a no-op (does not re-invoke the distro)
    before = len(calls)
    assert telemetry.init_telemetry("api") is True
    assert len(calls) == before, "init_telemetry must be idempotent"


def test_init_honors_explicit_logging_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=abc;IngestionEndpoint=https://example.local/",
    )
    monkeypatch.setenv("AZURE_MONITOR_DISABLE_LOGGING", "true")
    calls: list[dict[str, Any]] = []

    def _fake_configure(**kwargs: Any) -> None:
        calls.append(kwargs)

    import azure.monitor.opentelemetry as distro

    monkeypatch.setattr(distro, "configure_azure_monitor", _fake_configure)
    telemetry = _fresh_telemetry_module(monkeypatch)
    assert telemetry.init_telemetry("worker") is True
    assert calls and "disable_logging" not in calls[0]
    assert os.environ["OTEL_LOGS_EXPORTER"] == "none"


def test_init_honors_explicit_live_metrics_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=abc;IngestionEndpoint=https://example.local/",
    )
    monkeypatch.setenv("AZURE_MONITOR_DISABLE_LIVE_METRICS", "true")
    calls: list[dict[str, Any]] = []

    def _fake_configure(**kwargs: Any) -> None:
        calls.append(kwargs)

    import azure.monitor.opentelemetry as distro

    monkeypatch.setattr(distro, "configure_azure_monitor", _fake_configure)
    telemetry = _fresh_telemetry_module(monkeypatch)
    assert telemetry.init_telemetry("worker") is True
    assert calls and calls[0]["enable_live_metrics"] is False


def test_worker_process_init_initializes_worker_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _TelemetryModule:
        @staticmethod
        def init_telemetry(role: str) -> bool:
            calls.append(role)
            return True

    monkeypatch.setitem(sys.modules, "api.app.telemetry", _TelemetryModule)
    import api.celery_app as celery_app

    celery_app._on_worker_process_init()
    assert calls == ["worker"]


def test_init_never_raises_when_distro_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=abc;IngestionEndpoint=https://example.local/",
    )

    def _raise(**_kw: Any) -> None:
        raise RuntimeError("distro boom")

    import azure.monitor.opentelemetry as distro

    monkeypatch.setattr(distro, "configure_azure_monitor", _raise)
    telemetry = _fresh_telemetry_module(monkeypatch)
    assert telemetry.init_telemetry("worker") is False
    assert telemetry.is_initialized() is False

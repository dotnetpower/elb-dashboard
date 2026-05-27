"""Azure Monitor OpenTelemetry initialization for api and worker sidecars.

Responsibility: One-shot best-effort init of the `azure-monitor-opentelemetry`
distro so server-side traces, metrics, and api.* logs flow to Application
Insights when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set.
Edit boundaries: Initialization only. Add new manual instrumentations via
`opentelemetry.trace.get_tracer(__name__)` from the caller — do not centralize
custom spans here.
Key entry points: `init_telemetry(role, app=None)`.
Risky contracts: Must never raise. Must be safe to call multiple times in the
same process. Must remain a no-op when the connection string env var is unset
or empty so unit tests and `AUTH_DEV_BYPASS=true` local runs are unaffected.
Validation: `uv run pytest -q api/tests/test_telemetry_init.py`.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

LOGGER = logging.getLogger(__name__)

_INIT_LOCK = threading.Lock()
_INITIALIZED_FOR: str | None = None
_FASTAPI_INSTRUMENTED_APP_IDS: set[int] = set()
_OTEL_LOGS_EXPORTER_ENV = "OTEL_LOGS_EXPORTER"


def _bool_env(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resource_attributes(role: str) -> dict[str, str]:
    attributes = {
        "service.name": f"elb-{role}",
        "service.namespace": "elb-dashboard",
        "service.instance.id": os.environ.get(
            "CONTAINER_APP_REPLICA_NAME",
            os.environ.get("HOSTNAME", role),
        ),
    }
    revision = os.environ.get("CONTAINER_APP_REVISION", "").strip()
    if revision:
        attributes["service.version"] = revision
    return attributes


def _apply_logging_exporter_override() -> None:
    # azure-monitor-opentelemetry 1.6.x resolves logging enablement from
    # OTEL_LOGS_EXPORTER during configure_azure_monitor(), so translate our
    # app-specific opt-out env var into the standard OpenTelemetry switch.
    if _bool_env("AZURE_MONITOR_DISABLE_LOGGING") is True:
        os.environ[_OTEL_LOGS_EXPORTER_ENV] = "none"


def _instrument_fastapi(app: FastAPI | None) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        if app is None:
            FastAPIInstrumentor().instrument()
            return
        app_id = id(app)
        if app_id in _FASTAPI_INSTRUMENTED_APP_IDS:
            return
        FastAPIInstrumentor.instrument_app(app)
        _FASTAPI_INSTRUMENTED_APP_IDS.add(app_id)
    except Exception as exc:
        LOGGER.debug("fastapi instrumentor skipped: %s", type(exc).__name__)


def init_telemetry(role: str, app: FastAPI | None = None) -> bool:
    """Initialize Azure Monitor OpenTelemetry for the given sidecar role.

    Returns ``True`` when the distro was configured (or was already configured
    in this process), ``False`` when it was skipped because the connection
    string env var is missing/empty.

    Safe to call multiple times — subsequent calls in the same process are
    no-ops. ``role`` is propagated as ``service.name`` so api / worker / beat
    appear as separate cloud-role names in App Insights. When ``app`` is
    provided for the api sidecar, FastAPI request instrumentation is attached
    directly to that application instance.
    """
    connection_string = (os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or "").strip()
    if not connection_string:
        return False

    global _INITIALIZED_FOR
    with _INIT_LOCK:
        if _INITIALIZED_FOR is not None:
            if role == "api" and app is not None:
                _instrument_fastapi(app)
            return True

        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            from opentelemetry.sdk.resources import Resource

            _apply_logging_exporter_override()
            kwargs: dict[str, object] = {
                "connection_string": connection_string,
                "resource": Resource.create(_resource_attributes(role)),
                "instrumentation_options": {"fastapi": {"enabled": False}},
                # Limit stdlib log export to our application logger tree.
                # Root logging would also capture Azure SDK/exporter internals
                # and can create noisy feedback loops.
                "logger_name": "api",
                # Live Metrics (QuickPulse) streams per-second request / failure
                # / dependency counters to the App Insights blade so an
                # operator can correlate a dashboard click with backend
                # behaviour in real time. Opt-out via
                # AZURE_MONITOR_DISABLE_LIVE_METRICS=true.
                "enable_live_metrics": _bool_env("AZURE_MONITOR_DISABLE_LIVE_METRICS") is not True,
            }

            configure_azure_monitor(**kwargs)

            # Add FastAPI auto-instrumentation when this is the api role —
            # the distro covers requests / urllib / urllib3 / psycopg2 but
            # not FastAPI / Celery, those need explicit instrumentor calls.
            if role == "api":
                _instrument_fastapi(app)

            if role in {"worker", "beat"}:
                try:
                    from opentelemetry.instrumentation.celery import CeleryInstrumentor

                    CeleryInstrumentor().instrument()
                except Exception as exc:
                    LOGGER.debug("celery instrumentor skipped: %s", type(exc).__name__)

            _INITIALIZED_FOR = role
            LOGGER.info("azure monitor opentelemetry initialised for role=%s", role)
            return True
        except Exception as exc:
            LOGGER.warning(
                "azure monitor opentelemetry init failed (role=%s): %s",
                role,
                type(exc).__name__,
            )
            return False


def is_initialized() -> bool:
    """Return ``True`` when `init_telemetry` has configured the distro."""
    return _INITIALIZED_FOR is not None

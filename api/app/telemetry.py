"""Azure Monitor OpenTelemetry initialization for api and worker sidecars.

Responsibility: One-shot best-effort init of the `azure-monitor-opentelemetry`
distro so server-side traces, metrics, and logs flow to Application Insights
when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set.
Edit boundaries: Initialization only. Add new manual instrumentations via
`opentelemetry.trace.get_tracer(__name__)` from the caller — do not centralize
custom spans here.
Key entry points: `init_telemetry(role)`.
Risky contracts: Must never raise. Must be safe to call multiple times in the
same process. Must remain a no-op when the connection string env var is unset
or empty so unit tests and `AUTH_DEV_BYPASS=true` local runs are unaffected.
Validation: `uv run pytest -q api/tests/test_telemetry_init.py`.
"""

from __future__ import annotations

import logging
import os
import threading

LOGGER = logging.getLogger(__name__)

_INIT_LOCK = threading.Lock()
_INITIALIZED_FOR: str | None = None


def init_telemetry(role: str) -> bool:
    """Initialize Azure Monitor OpenTelemetry for the given sidecar role.

    Returns ``True`` when the distro was configured (or was already configured
    in this process), ``False`` when it was skipped because the connection
    string env var is missing/empty.

    Safe to call multiple times — subsequent calls in the same process are
    no-ops. ``role`` is propagated as ``service.name`` so api / worker / beat
    appear as separate cloud-role names in App Insights.
    """
    connection_string = (os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or "").strip()
    if not connection_string:
        return False

    global _INITIALIZED_FOR
    with _INIT_LOCK:
        if _INITIALIZED_FOR is not None:
            return True

        try:
            from azure.monitor.opentelemetry import configure_azure_monitor

            resource_attributes = {
                "service.name": f"elb-{role}",
                "service.namespace": "elb-dashboard",
                "service.instance.id": os.environ.get(
                    "CONTAINER_APP_REPLICA_NAME",
                    os.environ.get("HOSTNAME", role),
                ),
            }
            revision = os.environ.get("CONTAINER_APP_REVISION", "").strip()
            if revision:
                resource_attributes["service.version"] = revision

            configure_azure_monitor(
                connection_string=connection_string,
                resource_attributes=resource_attributes,
                # Disable internal logger collection to avoid log-loops — we
                # already JSON-log everything via stdlib logging and ship that
                # via Container Apps log streaming.
                disable_logging=os.environ.get(
                    "AZURE_MONITOR_DISABLE_LOGGING", "true"
                ).lower()
                == "true",
            )

            # Add FastAPI auto-instrumentation when this is the api role —
            # the distro covers requests / urllib / urllib3 / psycopg2 but
            # not FastAPI / Celery, those need explicit instrumentor calls.
            if role == "api":
                try:
                    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                    FastAPIInstrumentor().instrument()
                except Exception as exc:
                    LOGGER.debug("fastapi instrumentor skipped: %s", type(exc).__name__)

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

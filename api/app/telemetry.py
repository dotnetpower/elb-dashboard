"""Azure Monitor OpenTelemetry initialization for api and worker sidecars.

Responsibility: One-shot best-effort init of the `azure-monitor-opentelemetry`
distro so server-side traces, metrics, and api.* logs flow to Application
Insights when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set.
Edit boundaries: Initialization only. Add new manual instrumentations via
`opentelemetry.trace.get_tracer(__name__)` from the caller — do not centralize
custom spans here.
Key entry points: `init_telemetry(role, app=None)`, `annotate_error_span`,
`suppress_dependency_telemetry`.
Risky contracts: Must never raise. Must be safe to call multiple times in the
same process. Must remain a no-op when the connection string env var is unset
or empty so unit tests and `AUTH_DEV_BYPASS=true` local runs are unaffected.
Validation: `uv run pytest -q api/tests/test_telemetry_init.py`.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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


def _resolve_live_metrics_enabled(role: str) -> bool:
    """Decide whether Azure Monitor Live Metrics (QuickPulse) is enabled.

    Default ON for the single-process ``api`` role, OFF for the Celery prefork
    ``worker`` / ``beat`` roles (one QuickPulse stream per forked child is the
    boot cost that crash-loops the worker on a 0.5 vCPU budget). Either default
    is overridable: ``AZURE_MONITOR_DISABLE_LIVE_METRICS`` forces it off,
    ``AZURE_MONITOR_ENABLE_LIVE_METRICS`` forces it on. Disable wins if both are
    set, since the safer state is off.
    """
    if _bool_env("AZURE_MONITOR_DISABLE_LIVE_METRICS") is True:
        return False
    if _bool_env("AZURE_MONITOR_ENABLE_LIVE_METRICS") is True:
        return True
    return role == "api"


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


def _resolve_connection_string() -> str:
    """Resolve the connection string, healing from the durable store if wiped.

    Prefers the ``APPLICATIONINSIGHTS_CONNECTION_STRING`` env var; when empty
    (e.g. a full ``azd provision`` re-applied the Bicep template and reset the
    env to the empty azd value), falls back to the applied override persisted in
    the ``appinsightspref`` Table. This lets backend OpenTelemetry export
    self-heal on the next sidecar restart without a revision swap. The import is
    lazy and the lookup never raises — any failure degrades to the env value so
    telemetry init stays non-fatal at startup.
    """
    env_value = (os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or "").strip()
    if env_value:
        return env_value
    try:
        from api.services.app_insights_provisioning import deployment_connection_string

        return deployment_connection_string()
    except Exception as exc:
        LOGGER.debug("app insights persisted fallback skipped: %s", type(exc).__name__)
        return ""


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
    connection_string = _resolve_connection_string()
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
                # behaviour in real time.
                #
                # It is enabled by default ONLY for the ``api`` role. The api
                # sidecar is a single uvicorn process per replica, so the
                # dedicated QuickPulse streaming exporter + thread is cheap and
                # genuinely useful. The ``worker`` / ``beat`` sidecars run a
                # Celery *prefork* pool and call ``init_telemetry`` inside
                # EVERY forked child (``worker_process_init``); enabling live
                # metrics there spins up one QuickPulse stream per child. On the
                # worker's 0.5 vCPU budget that pushed child boot past
                # billiard's ``worker_proc_alive_timeout``, so the master
                # SIGKILL'd each child as "Timed out waiting for UP message" and
                # respawned it — a permanent crash loop that killed in-flight
                # BLAST tasks with ``WorkerLostError``. Default off for
                # worker/beat; either default can be overridden explicitly with
                # AZURE_MONITOR_DISABLE_LIVE_METRICS / AZURE_MONITOR_ENABLE_LIVE_METRICS.
                "enable_live_metrics": _resolve_live_metrics_enabled(role),
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


def annotate_error_span(
    *,
    status_code: int,
    error_type: str,
    detail: str | None = None,
    request_id: str | None = None,
) -> None:
    """Attach diagnostic attributes to the in-flight request span.

    Why this exists: the FastAPI OpenTelemetry instrumentor records a span per
    request but it does NOT mark 4xx responses as errors and never attaches the
    failure reason, so an operator looking at App Insights saw a bare
    ``resultCode=400`` request with no "why". This helper, called from the
    app's exception handlers, stamps the current span with ``elb.error.type`` /
    ``elb.error.detail`` / ``elb.request.id`` (which surface as
    ``customDimensions`` on the ``requests`` row) and, for 5xx, flips the span
    status to ERROR so the Failures blade and ``requests | where success ==
    false`` both light up.

    Attribute namespace: we deliberately use the ``elb.*`` prefix rather than
    the OpenTelemetry-standard ``error.type`` / ``http.response.status_code``.
    The ASGI instrumentor's ``_set_status`` sets the standard ``error.type`` to
    the bare status-code string ("404") under the new HTTP semantic-convention
    opt-in mode, and it runs AFTER this handler (on ``http.response.start``),
    so writing our richer value to ``error.type`` would be silently overwritten
    on some distro versions. ``elb.*`` keys are never touched by the
    instrumentor, so the detail always survives. The status code itself is
    already on the ``requests`` row (``resultCode``), so we do not duplicate it.

    Contract:
    * Never raises — telemetry annotation must not break request handling.
    * No-op when telemetry is not initialized (the current span is a
      non-recording span, so ``set_attribute`` is a cheap no-op anyway).
    * ``detail`` MUST already be sanitised by the caller (no tokens / SAS /
      subscription ids); this helper only length-caps it as a backstop.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return
        span.set_attribute("elb.error.type", error_type[:120])
        span.set_attribute("elb.error.status_code", int(status_code))
        if request_id:
            span.set_attribute("elb.request.id", request_id[:64])
        if detail:
            span.set_attribute("elb.error.detail", detail[:512])
        # 4xx stays a non-error span by HTTP semantics (it is a client
        # problem, not a server fault) but we still attach the reason above so
        # it is queryable. Only 5xx flips the span to ERROR so the Failures
        # blade reflects genuine server faults.
        if status_code >= 500:
            span.set_status(Status(StatusCode.ERROR, error_type[:120]))
    except Exception:
        # Telemetry must never break the request path.
        return


@contextmanager
def suppress_dependency_telemetry() -> Iterator[None]:
    """Suppress OpenTelemetry auto-instrumentation inside the ``with`` block.

    Why this exists: the ``azure-monitor-opentelemetry`` distro auto-instruments
    ``requests``/``urllib3``, and that instrumentation records an *exception*
    event on the client span whenever the underlying call raises — even when the
    application catches the error and degrades gracefully. For a high-frequency,
    best-effort, read-only call (the warmup pod-log GET fires up to 12x per
    monitor poll) a transient ``ConnectionError`` from AKS dropping a pooled
    keep-alive socket therefore still produces an App Insights exception row on
    every connection-pool churn, even though the app already falls back to an
    empty log. Wrapping such a call in this context manager sets the OTel
    suppression key so no dependency span — and thus no span exception event —
    is created for it.

    Scope discipline: use this ONLY for genuinely best-effort, failure-tolerant
    calls whose dependency timing we do not need in telemetry. It must never
    wrap a mutating or correctness-critical call, because it also hides
    successful dependency spans for the enclosed block.

    No-op (and never raises) when OpenTelemetry is not installed.
    """
    try:
        from opentelemetry.instrumentation.utils import suppress_instrumentation
    except Exception:
        yield
        return
    with suppress_instrumentation():
        yield

"""Best-effort feature lifecycle event emitter for Application Insights.

Responsibility: Emit a single structured "feature event" (warmup, cluster
    provisioning, prepare-db, BLAST submit lifecycle terminal states) on the
    ``api.events`` logger so that, WHEN Application Insights is configured, the
    Azure Monitor OpenTelemetry handler ships it as both a trace and a
    customEvent. When telemetry is disabled the call degrades to a local log
    line only, with zero Azure ingestion cost.
Edit boundaries: Pure emit helper. No Azure SDK calls, no state writes, no
    task-orchestration logic. Callers invoke this from inside best-effort
    state-update wrappers, so it MUST stay dependency-light and side-effect-free
    beyond the single log record.
Key entry points: ``record_feature_event``.
Risky contracts: MUST never raise — a logging fault must not break the calling
    Celery task. The ``microsoft.custom_event.name`` attribute key is the
    documented Azure Monitor signal that maps a stdlib log record to the App
    Insights ``customEvents`` table; do not rename it. Telemetry-disabled
    deployments rely on this being a no-op for App Insights (only stdout).
Validation: ``uv run pytest -q api/tests/test_feature_events.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from api.services.sanitise import sanitise

# Dedicated child of the ``api`` logger tree. ``api.app.telemetry`` configures
# the Azure Monitor handler with ``logger_name="api"``, so records emitted here
# are exported to Application Insights only when a connection string is set.
LOGGER = logging.getLogger("api.events")

# Azure Monitor OpenTelemetry maps a log record carrying this attribute to a
# customEvent (instead of a plain trace), with the value used as the event name.
_CUSTOM_EVENT_NAME_KEY = "microsoft.custom_event.name"

# Lifecycle outcomes that warrant a feature event. The task phase-update
# wrappers emit only on these so a per-tick ``running`` update never floods
# telemetry — one event per terminal transition.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# stdlib ``logging`` raises if an ``extra=`` key collides with a built-in
# ``LogRecord`` attribute, so any caller-supplied attribute matching one of
# these is prefixed before it reaches the record.
_RESERVED_LOGRECORD_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


def _coerce_attribute(value: Any) -> str | int | float | bool:
    """Reduce an attribute value to an App-Insights-safe scalar.

    Scalars pass through unchanged; everything else is stringified and run
    through ``sanitise`` so a stray token / SAS / connection string in a
    caller-supplied value cannot leak into telemetry.
    """
    if isinstance(value, bool | int | float):
        return value
    return sanitise(str(value))


def record_feature_event(event: str, *, status: str = "info", **attributes: Any) -> None:
    """Emit one feature lifecycle event.

    ``event`` is the customEvent name (e.g. ``"warmup"``, ``"cluster_provision"``,
    ``"prepare_db"``, ``"blast_submit"``). ``status`` is the lifecycle outcome
    (``"completed"`` / ``"failed"`` / ``"cancelled"`` / ``"info"``). Remaining
    keyword attributes (``job_id``, ``phase``, ``error_code``, ``database`` …)
    are attached as customDimensions; ``None`` values are dropped.

    Best-effort: never raises. When ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is
    unset the record is a plain local log line (no Azure cost); when set the
    Azure Monitor handler ships it as a trace and a customEvent.
    """
    try:
        extra: dict[str, Any] = {
            _CUSTOM_EVENT_NAME_KEY: event,
            "feature_event": event,
            "event_status": status,
        }
        for key, value in attributes.items():
            if value is None:
                continue
            safe_key = f"attr_{key}" if key in _RESERVED_LOGRECORD_KEYS else key
            extra[safe_key] = _coerce_attribute(value)
        LOGGER.info("feature_event %s status=%s", event, status, extra=extra)
    except Exception:  # pragma: no cover - logging must never break the caller
        LOGGER.debug("feature event emit failed event=%s", event, exc_info=True)

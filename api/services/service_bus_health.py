"""Bounded health snapshots for Service Bus queues and producer responses.

Responsibility: Read payload-free queue, execution-admission, and response-outbox
    health signals for periodic operational telemetry.
Edit boundaries: Observability reads and in-process outbox-flush state only; no
    queue settlement, response publication, HTTP shaping, or task scheduling.
Key entry points: ``collect_service_bus_health``, ``note_outbox_flush``.
Risky contracts: Outbox scans stay bounded, request bodies and response payloads
    never enter the returned snapshot, and failures in one signal family must
    not hide the others.
Validation: ``uv run pytest -q api/tests/test_service_bus_health.py``.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from typing import Any

from api.services import service_bus
from api.services.service_bus_outbox import list_pending_responses
from api.services.service_bus_pref import ServiceBusConfig

_OUTBOX_SAMPLE_LIMIT = 201
_FLUSH_LOCK = threading.Lock()
_FLUSH_STATE: dict[str, Any] = {
    "last_attempt_at": "",
    "last_success_at": "",
    "last_error_at": "",
    "last_scanned": 0,
    "last_delivered": 0,
    "last_errors": 0,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _non_negative_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, value)
    if not isinstance(value, str | float):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _age_seconds(value: str) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0, int((_now() - parsed).total_seconds()))


def note_outbox_flush(stats: dict[str, int], *, attempted: bool = True) -> None:
    """Record the latest in-process outbox flush result for health telemetry.

    The state intentionally resets when a Celery child recycles. Durable truth
    remains the outbox itself; these timestamps only explain the current
    worker's most recent publish attempt.
    """
    if not attempted:
        return
    now = _now_iso()
    errors = _non_negative_int(stats.get("errors"))
    with _FLUSH_LOCK:
        _FLUSH_STATE.update(
            {
                "last_attempt_at": now,
                "last_scanned": _non_negative_int(stats.get("scanned")),
                "last_delivered": _non_negative_int(stats.get("delivered")),
                "last_errors": errors,
            }
        )
        if errors:
            _FLUSH_STATE["last_error_at"] = now
        else:
            _FLUSH_STATE["last_success_at"] = now


def _flush_state() -> dict[str, Any]:
    with _FLUSH_LOCK:
        return dict(_FLUSH_STATE)


def _queue_snapshot(cfg: ServiceBusConfig) -> dict[str, Any]:
    try:
        counts = service_bus.entity_counts(cfg)
    except Exception as exc:
        return {
            "counts_available": False,
            "counts_error": type(exc).__name__,
            "active": None,
            "scheduled": None,
            "dead_letter": None,
            "total": None,
            "completion_subscription_count": None,
            "completion_accessible": None,
            "completion_error": "",
            "completion_active": None,
            "completion_dead_letter": None,
        }

    queue = counts.get("queue") if isinstance(counts, dict) else None
    queue = queue if isinstance(queue, dict) else {}
    subscriptions = counts.get("subscriptions") if isinstance(counts, dict) else []
    subscriptions = subscriptions if isinstance(subscriptions, list) else []
    completion_active = sum(
        _non_negative_int(item.get("active_message_count"))
        for item in subscriptions
        if isinstance(item, dict)
    )
    completion_dead_letter = sum(
        _non_negative_int(item.get("dead_letter_message_count"))
        for item in subscriptions
        if isinstance(item, dict)
    )
    return {
        "counts_available": bool(queue),
        "counts_error": "" if queue else "queue_counts_missing",
        "active": _non_negative_int(queue.get("active_message_count")) if queue else None,
        "scheduled": (_non_negative_int(queue.get("scheduled_message_count")) if queue else None),
        "dead_letter": (
            _non_negative_int(queue.get("dead_letter_message_count")) if queue else None
        ),
        "total": _non_negative_int(queue.get("total_message_count")) if queue else None,
        "completion_subscription_count": len(subscriptions),
        "completion_accessible": counts.get("completion_accessible"),
        "completion_error": str(counts.get("completion_error") or "")[:128],
        "completion_active": completion_active,
        "completion_dead_letter": completion_dead_letter,
    }


def _outbox_snapshot() -> dict[str, Any]:
    try:
        pending = list_pending_responses(limit=_OUTBOX_SAMPLE_LIMIT)
    except Exception as exc:
        return {
            "available": False,
            "error": type(exc).__name__,
            "pending": None,
            "pending_truncated": False,
            "oldest_age_seconds": None,
            **_flush_state(),
        }

    truncated = len(pending) >= _OUTBOX_SAMPLE_LIMIT
    sampled = pending[: _OUTBOX_SAMPLE_LIMIT - 1] if truncated else pending
    oldest = sampled[0].created_at if sampled else ""
    return {
        "available": True,
        "error": "",
        "pending": len(sampled),
        "pending_truncated": truncated,
        "oldest_age_seconds": _age_seconds(oldest),
        **_flush_state(),
    }


def collect_service_bus_health(
    cfg: ServiceBusConfig,
    *,
    admission: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return one bounded, payload-free health snapshot that never raises."""
    queue = _queue_snapshot(cfg)
    outbox = _outbox_snapshot()
    admission_values = admission if isinstance(admission, dict) else {}
    admission_available = admission is not None
    admission_allowed = bool(admission_values.get("allowed")) if admission_available else None
    admission_reason = str(admission_values.get("reason") or "")[:128]
    completion_configured = bool(str(getattr(cfg, "completion_topic", "") or "").strip())
    completion_kind = str(getattr(cfg, "completion_kind", "topic") or "topic")[:16]

    warnings: list[str] = []
    if not completion_configured:
        warnings.append("completion_not_configured")
    elif queue["completion_accessible"] is False:
        warnings.append("completion_unavailable")
    elif (
        completion_kind == "topic"
        and queue["completion_accessible"] is True
        and not queue["completion_subscription_count"]
    ):
        warnings.append("completion_topic_has_no_subscriptions")
    if not queue["counts_available"]:
        warnings.append("queue_counts_unavailable")
    if _non_negative_int(queue["dead_letter"]) > 0:
        warnings.append("request_dlq_nonempty")
    if _non_negative_int(queue["completion_dead_letter"]) > 0:
        warnings.append("completion_dlq_nonempty")
    if not outbox["available"]:
        warnings.append("outbox_unavailable")
    if outbox["pending_truncated"]:
        warnings.append("outbox_backlog_truncated")
    if _non_negative_int(outbox["last_errors"]) > 0:
        warnings.append("outbox_flush_failed")
    if admission_available and admission_allowed is False and _non_negative_int(queue["active"]):
        warnings.append("drain_admission_blocked")

    return {
        "status": "warning" if warnings else "ok",
        "warnings": tuple(warnings),
        "queue": queue,
        "outbox": outbox,
        "admission_available": admission_available,
        "admission_allowed": admission_allowed,
        "admission_reason": admission_reason,
        "completion_configured": completion_configured,
        "completion_kind": completion_kind,
        "resident_consumer_enabled": _bool_env("SERVICEBUS_RESIDENT_CONSUMER"),
        "drain_concurrency": max(
            1,
            min(32, _non_negative_int(os.environ.get("SERVICEBUS_DRAIN_CONCURRENCY", "1"))),
        ),
    }


def _reset_health_state_for_tests() -> None:
    with _FLUSH_LOCK:
        _FLUSH_STATE.update(
            {
                "last_attempt_at": "",
                "last_success_at": "",
                "last_error_at": "",
                "last_scanned": 0,
                "last_delivered": 0,
                "last_errors": 0,
            }
        )

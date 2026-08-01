"""Non-destructive Service Bus peek and preview operations.

Responsibility: Peek request/DLQ messages and shape sanitised, size-bounded
    previews for dashboard and operator surfaces without settling messages.
Edit boundaries: Read-only data-plane peek and presentation shaping only. SDK
    client construction, message parsing, settlement, deletion, and HTTP
    response shaping remain in their existing modules.
Key entry points: ``peek_requests``, ``peek_dead_letter``,
    ``preview_message``, ``dead_letter_preview``,
    ``peek_request_previews``, ``peek_dead_letter_previews``.
Risky contracts: Peeks require Data Receiver rather than Manage permission and
    never lock/remove messages; body and broker error text are sanitised and
    capped; the correlation/request-id fallback order and preview keys remain
    backward compatible.
Validation: ``uv run pytest -q api/tests/test_service_bus_peek.py
    api/tests/test_service_bus_dlq.py api/tests/test_message_flow.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from azure.servicebus import ServiceBusSubQueue

from api.services.service_bus_pref import ServiceBusConfig

ClientFactory = Callable[[ServiceBusConfig], AbstractContextManager[Any]]
MessageParser = Callable[[Any], Any]
TextSanitiser = Callable[[str], str]
IsoRenderer = Callable[[Any], str | None]
PeekFunction = Callable[[ServiceBusConfig | None, int], list[Any]]
PreviewFunction = Callable[[Any], dict[str, Any]]


def peek_requests(
    cfg: ServiceBusConfig | None,
    max_count: int,
    *,
    require_config: Callable[[ServiceBusConfig | None], ServiceBusConfig],
    client_factory: ClientFactory,
    parse_message: MessageParser,
) -> list[Any]:
    """Non-destructively peek the request queue."""
    resolved = require_config(cfg)
    messages: list[Any] = []
    with (
        client_factory(resolved) as client,
        client.get_queue_receiver(resolved.request_queue) as receiver,
    ):
        for message in receiver.peek_messages(max_message_count=max(1, min(max_count, 100))):
            messages.append(parse_message(message))
    return messages


def peek_dead_letter(
    cfg: ServiceBusConfig | None,
    max_count: int,
    *,
    require_config: Callable[[ServiceBusConfig | None], ServiceBusConfig],
    client_factory: ClientFactory,
    parse_message: MessageParser,
) -> list[Any]:
    """Non-destructively peek the request queue's dead-letter sub-queue."""
    resolved = require_config(cfg)
    messages: list[Any] = []
    with (
        client_factory(resolved) as client,
        client.get_queue_receiver(
            resolved.request_queue,
            sub_queue=ServiceBusSubQueue.DEAD_LETTER,
        ) as receiver,
    ):
        for message in receiver.peek_messages(max_message_count=max(1, min(max_count, 100))):
            messages.append(parse_message(message))
    return messages


def preview_message(
    parsed: Any,
    *,
    sanitise_text: TextSanitiser,
    body_max_chars: int,
    iso_renderer: IsoRenderer,
) -> dict[str, Any]:
    """Shape one request message into a sanitised, bounded preview."""
    body = parsed.body if isinstance(parsed.body, dict) else {}

    def first_value(*candidates: Any) -> str | None:
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return None

    try:
        body_json = json.dumps(body, default=str, ensure_ascii=False, indent=2)
    except Exception:
        body_json = parsed.raw_body or ""
    sanitised_body = sanitise_text(body_json)
    return {
        "message_id": parsed.message_id,
        "correlation_id": first_value(
            body.get("external_correlation_id"),
            parsed.correlation_id,
        ),
        "request_id": first_value(
            body.get("request_id"),
            parsed.application_properties.get("request_id"),
        ),
        "subject": parsed.subject,
        "sequence_number": parsed.sequence_number,
        "enqueued_time_utc": iso_renderer(parsed.enqueued_time_utc),
        "program": first_value(body.get("program")),
        "db": first_value(body.get("db")),
        "body_preview": sanitised_body[:body_max_chars],
        "body_truncated": len(sanitised_body) > body_max_chars,
    }


def dead_letter_preview(
    parsed: Any,
    *,
    preview: PreviewFunction,
    sanitise_text: TextSanitiser,
    body_max_chars: int,
) -> dict[str, Any]:
    """Add bounded broker dead-letter metadata to a base preview."""
    shaped = preview(parsed)
    reason = str(parsed.dead_letter_reason or "").strip()
    description = str(parsed.dead_letter_error_description or "").strip()
    shaped["dead_letter_reason"] = sanitise_text(reason)[:body_max_chars] if reason else None
    shaped["dead_letter_error_description"] = (
        sanitise_text(description)[:body_max_chars] if description else None
    )
    shaped["delivery_count"] = parsed.delivery_count
    return shaped


def peek_request_previews(
    cfg: ServiceBusConfig | None,
    max_count: int,
    *,
    peek: PeekFunction,
    preview: PreviewFunction,
) -> list[dict[str, Any]]:
    """Peek and shape request messages through injected facade operations."""
    return [preview(message) for message in peek(cfg, max_count)]


def peek_dead_letter_previews(
    cfg: ServiceBusConfig | None,
    max_count: int,
    *,
    peek: PeekFunction,
    preview: PreviewFunction,
) -> list[dict[str, Any]]:
    """Peek and shape DLQ messages through injected facade operations."""
    return [preview(message) for message in peek(cfg, max_count)]

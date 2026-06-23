"""Service Bus data-plane + management client wrapper (optional integration).

Responsibility: The ONLY module that imports ``azure.servicebus``. Builds
    senders/receivers/admin clients for both auth modes (Entra
    ``DefaultAzureCredential`` and SAS connection string), and exposes the
    bounded, side-effect-tagged operations the routes and tasks need: send a
    request, publish a transition event, peek (non-destructive), drain the
    request queue with explicit message settlement, read runtime counts, and
    purge the dead-letter queue with a mandatory audit backup.
Edit boundaries: Reusable cloud/data-plane logic only. No HTTP shaping, no
    Celery task bodies, no persistence of the config row (that is
    ``service_bus_pref``). Routes/tasks call THIS module; nothing else imports
    ``azure.servicebus``.
Key entry points: ``send_request``, ``publish_event``, ``peek_requests``,
    ``peek_request_previews``, ``peek_dead_letter_previews``, ``drain_requests``,
    ``entity_counts``, ``purge_dead_letter``, ``delete_dead_letter_messages``,
    ``promote_dead_letter_messages``, ``test_connection``, ``MessageAction``.
Risky contracts: Receivers settle EVERY message they receive (complete /
    abandon / dead-letter) — a leaked lock causes redelivery and duplicate BLAST
    runs. ``drain_requests``, ``purge_dead_letter``, ``delete_dead_letter_messages``
    and ``promote_dead_letter_messages`` are BOUNDED by ``max_messages`` so a
    backlog can never spin a single tick forever. ``promote_dead_letter_messages``
    re-sends to the main queue BEFORE removing from the DLQ so a crash never
    loses a message (the idempotent drain handler dedupes any resulting
    duplicate on ``external_correlation_id``). The SAS connection string is read
    from an env secret or Key Vault and is NEVER logged or returned to a caller.
    All errors are normalised to ``ServiceBusUnavailable`` / ``ServiceBusAuthError``
    so callers degrade instead of leaking SDK internals.
Validation: ``uv run pytest -q api/tests/test_service_bus_drain_loop.py``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from azure.core.exceptions import ClientAuthenticationError
from azure.servicebus import (
    ServiceBusClient,
    ServiceBusMessage,
    ServiceBusReceiveMode,
    ServiceBusSubQueue,
)
from azure.servicebus.exceptions import ServiceBusAuthenticationError, ServiceBusError
from azure.servicebus.management import ServiceBusAdministrationClient

from api.services import get_credential
from api.services.sanitise import sanitise
from api.services.service_bus_pref import (
    AUTH_MODE_SAS,
    ServiceBusConfig,
    completion_is_queue,
    get_service_bus_config,
)

LOGGER = logging.getLogger(__name__)

# Bounds. Drain and purge are explicitly capped so one tick cannot run forever.
_RECEIVE_MAX_WAIT_SECONDS = 5
_PEEK_DEFAULT = 5
# Cap the sanitised body preview a peek returns so a large query FASTA cannot
# bloat the response or the dashboard. A content preview never needs the full
# payload; the truncation is flagged via ``body_truncated``.
_PEEK_BODY_MAX_CHARS = 4000


class ServiceBusUnavailable(RuntimeError):
    """Config is disabled/incomplete, or the SAS secret could not be resolved."""


class ServiceBusAuthError(RuntimeError):
    """The namespace rejected the credential (Entra issuer / SAS disabled)."""


class MessageAction(StrEnum):
    """What ``drain_requests`` / ``purge_dead_letter`` should do with a message."""

    COMPLETE = "complete"
    ABANDON = "abandon"
    DEAD_LETTER = "dead_letter"


@dataclass
class ParsedMessage:
    """A non-SDK view of a Service Bus message handed to handlers/callers."""

    body: dict[str, Any]
    raw_body: str
    message_id: str | None
    correlation_id: str | None
    subject: str | None
    content_type: str | None
    enqueued_time_utc: datetime | None
    sequence_number: int | None
    application_properties: dict[str, Any] = field(default_factory=dict)
    dead_letter_reason: str | None = None
    dead_letter_error_description: str | None = None
    delivery_count: int | None = None


@dataclass
class DrainStats:
    received: int = 0
    completed: int = 0
    abandoned: int = 0
    dead_lettered: int = 0


@dataclass
class PurgeStats:
    scanned: int = 0
    purged: int = 0
    kept: int = 0
    backup_failed: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


def _require_enabled_config(cfg: ServiceBusConfig | None) -> ServiceBusConfig:
    cfg = cfg or get_service_bus_config()
    if not cfg.namespace_fqdn:
        raise ServiceBusUnavailable("Service Bus namespace is not configured")
    return cfg


def _resolve_sas_connection_string(cfg: ServiceBusConfig) -> str:
    """Resolve the SAS connection string without ever logging its value.

    Order: explicit ``SERVICEBUS_CONNECTION_STRING`` env secret (deploy-time
    Container Apps secret, matches the original external setup), then a Key
    Vault secret named ``cfg.sas_secret_name`` when ``KEY_VAULT_URI`` is wired
    (the runtime Settings path). Raises ``ServiceBusUnavailable`` when neither
    yields a value.
    """
    env_conn = (os.environ.get("SERVICEBUS_CONNECTION_STRING") or "").strip()
    if env_conn:
        return env_conn
    vault_uri = (os.environ.get("KEY_VAULT_URI") or "").strip()
    if vault_uri and cfg.sas_secret_name:
        from api.services.keyvault import get_secret

        value = get_secret(get_credential(), vault_uri, cfg.sas_secret_name).strip()
        if value:
            return value
    raise ServiceBusUnavailable(
        "SAS connection string is not available (set SERVICEBUS_CONNECTION_STRING "
        "or wire KEY_VAULT_URI + sas_secret_name)"
    )


@contextmanager
def _client(cfg: ServiceBusConfig) -> Iterator[ServiceBusClient]:
    """Yield a ServiceBusClient for the configured auth mode.

    SDK auth failures are normalised to ``ServiceBusAuthError`` so callers do
    not have to import the SDK exception hierarchy.
    """
    try:
        if cfg.auth_mode == AUTH_MODE_SAS:
            conn = _resolve_sas_connection_string(cfg)
            client = ServiceBusClient.from_connection_string(conn)
        else:
            client = ServiceBusClient(cfg.namespace_fqdn, get_credential())
    except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
        raise ServiceBusAuthError(str(exc)) from exc
    try:
        yield client
    finally:
        client.close()


@contextmanager
def _admin_client(cfg: ServiceBusConfig) -> Iterator[ServiceBusAdministrationClient]:
    try:
        if cfg.auth_mode == AUTH_MODE_SAS:
            conn = _resolve_sas_connection_string(cfg)
            admin = ServiceBusAdministrationClient.from_connection_string(conn)
        else:
            admin = ServiceBusAdministrationClient(cfg.namespace_fqdn, get_credential())
    except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
        raise ServiceBusAuthError(str(exc)) from exc
    try:
        yield admin
    finally:
        admin.close()


def _parse(message: Any) -> ParsedMessage:
    try:
        raw = b"".join(message.body).decode("utf-8", "replace")
    except Exception:
        raw = str(message)
    try:
        body = json.loads(raw) if raw else {}
        if not isinstance(body, dict):
            body = {"_value": body}
    except json.JSONDecodeError:
        body = {}
    return ParsedMessage(
        body=body,
        raw_body=raw,
        message_id=getattr(message, "message_id", None),
        correlation_id=getattr(message, "correlation_id", None),
        subject=getattr(message, "subject", None),
        content_type=getattr(message, "content_type", None),
        enqueued_time_utc=getattr(message, "enqueued_time_utc", None),
        sequence_number=getattr(message, "sequence_number", None),
        application_properties=dict(getattr(message, "application_properties", None) or {}),
        dead_letter_reason=getattr(message, "dead_letter_reason", None),
        dead_letter_error_description=getattr(message, "dead_letter_error_description", None),
        delivery_count=getattr(message, "delivery_count", None),
    )


# --------------------------------------------------------------------------- #
# Producer side
# --------------------------------------------------------------------------- #


def send_request(
    cfg: ServiceBusConfig | None,
    body: dict[str, Any],
    *,
    message_id: str | None = None,
    correlation_id: str | None = None,
    subject: str = "blast.request",
) -> str:
    """Enqueue a BLAST request message. Returns the message_id used."""
    cfg = _require_enabled_config(cfg)
    payload = json.dumps(body, default=str)
    message = ServiceBusMessage(
        payload,
        content_type="application/json",
        subject=subject,
        message_id=message_id,
        correlation_id=correlation_id,
    )
    with _client(cfg) as client, client.get_queue_sender(cfg.request_queue) as sender:
        sender.send_messages(message)
    # Event-driven auto-start: the moment a request lands on the queue, kick an
    # immediate idle/auto-start evaluation so a Stopped cluster starts within
    # seconds instead of waiting out the next 5-min beat tick. Gated + best-effort
    # (no-op when SERVICEBUS_QUEUE_AUTOSTART is off; never raises into the send).
    try:
        from api.services.aks.queue_autostart import request_autostart_evaluation

        request_autostart_evaluation(reason="servicebus_request_enqueued")
    except Exception as exc:  # never let the autostart trigger fail a send
        LOGGER.debug("autostart eval trigger import skipped: %s", type(exc).__name__)
    return message.message_id or ""


def publish_event(cfg: ServiceBusConfig | None, event: dict[str, Any]) -> None:
    """Publish a transition event to the completion entity.

    The completion entity is a topic by default (fan-out to many subscriptions);
    when ``cfg.completion_kind == "queue"`` it is a queue (point-to-point, a
    single competing consumer). No-op (logged) when no completion entity is
    configured — the integration can run request-only without one.
    """
    cfg = _require_enabled_config(cfg)
    if not cfg.completion_topic:
        LOGGER.debug("publish_event skipped: no completion entity configured")
        return
    # Echo the caller-supplied pass-through value onto the message envelope (not
    # just the JSON body) when present, so a subscriber/consumer can correlate /
    # filter on it without parsing the payload. Omitted when the producer set
    # none, keeping the envelope unchanged for the common case.
    request_id = str(event.get("request_id") or "").strip()
    application_properties = {"request_id": request_id} if request_id else None
    message = ServiceBusMessage(
        json.dumps(event, default=str),
        content_type="application/json",
        subject=str(event.get("event") or "blast.transition"),
        correlation_id=str(event.get("external_correlation_id") or "") or None,
        application_properties=application_properties,
    )
    with _client(cfg) as client:
        if completion_is_queue(cfg):
            with client.get_queue_sender(cfg.completion_topic) as sender:
                sender.send_messages(message)
        else:
            with client.get_topic_sender(cfg.completion_topic) as sender:
                sender.send_messages(message)


# --------------------------------------------------------------------------- #
# Consumer side
# --------------------------------------------------------------------------- #


def peek_requests(
    cfg: ServiceBusConfig | None, max_count: int = _PEEK_DEFAULT
) -> list[ParsedMessage]:
    """Non-destructive peek of the request queue (does not lock or remove)."""
    cfg = _require_enabled_config(cfg)
    out: list[ParsedMessage] = []
    with _client(cfg) as client, client.get_queue_receiver(cfg.request_queue) as receiver:
        for message in receiver.peek_messages(max_message_count=max(1, min(max_count, 100))):
            out.append(_parse(message))
    return out


def peek_dead_letter(
    cfg: ServiceBusConfig | None, max_count: int = _PEEK_DEFAULT
) -> list[ParsedMessage]:
    """Non-destructive peek of the request queue's dead-letter sub-queue.

    Mirrors :func:`peek_requests` but reads the DLQ. Like a main-queue peek it
    does not lock or remove the message, so it is safe to call repeatedly and
    needs only the ``Azure Service Bus Data Receiver`` claim (NOT ``Manage``).
    The parsed messages carry ``dead_letter_reason`` (+ the SDK's error
    description via ``application_properties`` is left untouched) so the caller
    can surface WHY each message was dead-lettered.
    """
    cfg = _require_enabled_config(cfg)
    out: list[ParsedMessage] = []
    with _client(cfg) as client, client.get_queue_receiver(
        cfg.request_queue,
        sub_queue=ServiceBusSubQueue.DEAD_LETTER,
    ) as receiver:
        for message in receiver.peek_messages(max_message_count=max(1, min(max_count, 100))):
            out.append(_parse(message))
    return out


def _preview_message(parsed: ParsedMessage) -> dict[str, Any]:
    """Shape a peeked request message into a sanitised, size-bounded preview.

    The request-queue body is the user-supplied BLAST request (query FASTA, db,
    program, options, correlation/request ids) — not credentials — but it is
    still run through :func:`api.services.sanitise.sanitise` defensively
    (charter §12: sanitise UI output) and capped at ``_PEEK_BODY_MAX_CHARS`` so
    a large query FASTA cannot bloat the response or the dashboard. Returns a
    JSON-safe dict the SPA renders directly. ``enqueued_time_utc`` reuses the
    same ISO rendering as the counts telemetry.
    """
    body = parsed.body if isinstance(parsed.body, dict) else {}

    def _opt(*candidates: Any) -> str | None:
        """First non-empty stripped candidate, else None (keeps preview compact)."""
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return None

    program = _opt(body.get("program"))
    db = _opt(body.get("db"))
    correlation_id = _opt(body.get("external_correlation_id"), parsed.correlation_id)
    request_id = _opt(body.get("request_id"), parsed.application_properties.get("request_id"))
    try:
        body_json = json.dumps(body, default=str, ensure_ascii=False, indent=2)
    except Exception:
        body_json = parsed.raw_body or ""
    sanitised_body = sanitise(body_json)
    body_preview = sanitised_body[:_PEEK_BODY_MAX_CHARS]
    return {
        "message_id": parsed.message_id,
        "correlation_id": correlation_id,
        "request_id": request_id,
        "subject": parsed.subject,
        "sequence_number": parsed.sequence_number,
        "enqueued_time_utc": _iso_or_none(parsed.enqueued_time_utc),
        "program": program,
        "db": db,
        "body_preview": body_preview,
        "body_truncated": len(sanitised_body) > _PEEK_BODY_MAX_CHARS,
    }


def _dead_letter_preview(parsed: ParsedMessage) -> dict[str, Any]:
    """Shape a peeked DLQ message into a preview with dead-letter metadata.

    Extends :func:`_preview_message` with the broker-supplied
    ``dead_letter_reason`` / ``dead_letter_error_description`` (sanitised +
    bounded so a long error body cannot bloat the response) and the
    ``delivery_count`` so the operator can see WHY the message was
    dead-lettered before deciding to delete or promote it. ``sequence_number``
    (already in the base preview) is the stable handle the delete / promote
    routes target.
    """
    preview = _preview_message(parsed)
    reason = str(parsed.dead_letter_reason or "").strip()
    description = str(parsed.dead_letter_error_description or "").strip()
    preview["dead_letter_reason"] = sanitise(reason)[:_PEEK_BODY_MAX_CHARS] if reason else None
    preview["dead_letter_error_description"] = (
        sanitise(description)[:_PEEK_BODY_MAX_CHARS] if description else None
    )
    preview["delivery_count"] = parsed.delivery_count
    return preview


def peek_request_previews(
    cfg: ServiceBusConfig | None, max_count: int = _PEEK_DEFAULT
) -> list[dict[str, Any]]:
    """Non-destructive peek shaped into sanitised previews for the dashboard.

    Thin shaping over :func:`peek_requests` so the Playground and Message Flow
    surfaces can show the actual messages currently sitting in the request
    queue. CRITICAL: this reads via the data-plane receiver, which needs only
    ``Azure Service Bus Data Receiver`` — NOT the ``Manage`` claim that
    :func:`entity_counts` requires — so it can surface content even when runtime
    counts degrade to ``no_manage_claim``. Never removes or locks a message.
    """
    return [_preview_message(m) for m in peek_requests(cfg, max_count=max_count)]


def peek_dead_letter_previews(
    cfg: ServiceBusConfig | None, max_count: int = _PEEK_DEFAULT
) -> list[dict[str, Any]]:
    """Non-destructive DLQ peek shaped into sanitised previews for the dashboard.

    Thin shaping over :func:`peek_dead_letter` (mirrors
    :func:`peek_request_previews` for the main queue). Each preview adds the
    dead-letter reason / error description / delivery count so the operator can
    triage a dead-lettered message before deleting or promoting it. Reads via
    the data-plane receiver (``Data Receiver`` claim, not ``Manage``). Never
    removes or locks a message.
    """
    return [_dead_letter_preview(m) for m in peek_dead_letter(cfg, max_count=max_count)]


@dataclass
class DeadLetterActionStats:
    """Outcome of a targeted DLQ delete / promote pass."""

    scanned: int = 0
    matched: int = 0
    deleted: int = 0
    promoted: int = 0
    kept: int = 0
    failed: int = 0


def delete_dead_letter_messages(
    cfg: ServiceBusConfig | None,
    *,
    sequence_numbers: list[int],
    max_messages: int = 100,
) -> DeadLetterActionStats:
    """Delete specific DLQ messages by sequence number (operator action).

    Receives the DLQ under PEEK_LOCK and completes (hard-deletes) each message
    whose sequence number is requested; everything else is abandoned (left in
    place). Unlike :func:`purge_dead_letter` there is no backup step — this is
    the explicit "delete these messages" the operator chose after peeking, so
    the SPA owns the confirmation. Bounded (by ``max_messages`` and by stopping
    once every requested sequence number is matched) and partial-failure
    isolated.
    """
    cfg = _require_enabled_config(cfg)
    stats = DeadLetterActionStats()
    if not sequence_numbers:
        return stats
    wanted = set(sequence_numbers)
    budget = max(1, max_messages)
    seen: set[int] = set()
    with _client(cfg) as client, client.get_queue_receiver(
        cfg.request_queue,
        sub_queue=ServiceBusSubQueue.DEAD_LETTER,
        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
        max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
    ) as receiver:
        while budget > 0 and wanted:
            batch = receiver.receive_messages(
                max_message_count=min(budget, 32),
                max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
            )
            if not batch:
                break
            wrapped = False
            for message in batch:
                parsed = _parse(message)
                seq = parsed.sequence_number
                if seq is not None and seq in seen and seq not in wanted:
                    _safe_abandon(receiver, message)
                    wrapped = True
                    break
                if seq is not None:
                    seen.add(seq)
                budget -= 1
                stats.scanned += 1
                if seq is None or seq not in wanted:
                    _safe_abandon(receiver, message)
                    stats.kept += 1
                    continue
                stats.matched += 1
                wanted.discard(seq)
                try:
                    receiver.complete_message(message)
                    stats.deleted += 1
                except ServiceBusError:
                    LOGGER.warning("DLQ delete complete failed (lock lost?) seq=%s", seq)
                    stats.failed += 1
            if wrapped:
                break
    return stats


def promote_dead_letter_messages(
    cfg: ServiceBusConfig | None,
    *,
    sequence_numbers: list[int],
    max_messages: int = 100,
) -> DeadLetterActionStats:
    """Re-queue specific DLQ messages onto the main request queue (operator action).

    For each targeted DLQ message: re-send its body to the main request queue
    FIRST, and only complete (remove from the DLQ) once the send succeeds.
    Ordering matters for at-least-once safety — if the send succeeds but the
    complete fails, the message stays in BOTH queues, but the drain handler is
    idempotent on ``external_correlation_id`` (a duplicate request completes
    without a second BLAST submit), so the worst case is one redundant
    drain-and-dedupe, never a lost message or a duplicate run. Non-targeted
    messages are abandoned (left in the DLQ). Bounded and partial-failure
    isolated.
    """
    cfg = _require_enabled_config(cfg)
    stats = DeadLetterActionStats()
    if not sequence_numbers:
        return stats
    wanted = set(sequence_numbers)
    budget = max(1, max_messages)
    seen: set[int] = set()
    with _client(cfg) as client, client.get_queue_receiver(
        cfg.request_queue,
        sub_queue=ServiceBusSubQueue.DEAD_LETTER,
        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
        max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
    ) as receiver, client.get_queue_sender(cfg.request_queue) as sender:
        while budget > 0 and wanted:
            batch = receiver.receive_messages(
                max_message_count=min(budget, 32),
                max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
            )
            if not batch:
                break
            wrapped = False
            for message in batch:
                parsed = _parse(message)
                seq = parsed.sequence_number
                if seq is not None and seq in seen and seq not in wanted:
                    _safe_abandon(receiver, message)
                    wrapped = True
                    break
                if seq is not None:
                    seen.add(seq)
                budget -= 1
                stats.scanned += 1
                if seq is None or seq not in wanted:
                    _safe_abandon(receiver, message)
                    stats.kept += 1
                    continue
                stats.matched += 1
                wanted.discard(seq)
                # Re-send to the main queue FIRST (preserve identity so the
                # drain handler dedupes on correlation_id), then remove from DLQ.
                requeued = ServiceBusMessage(
                    parsed.raw_body or json.dumps(parsed.body, default=str),
                    content_type=parsed.content_type or "application/json",
                    subject=parsed.subject or "blast.request",
                    message_id=parsed.message_id,
                    correlation_id=parsed.correlation_id,
                    application_properties=parsed.application_properties or None,
                )
                try:
                    sender.send_messages(requeued)
                except Exception:
                    LOGGER.warning("DLQ promote re-send failed seq=%s; keeping in DLQ", seq)
                    _safe_abandon(receiver, message)
                    stats.failed += 1
                    continue
                try:
                    receiver.complete_message(message)
                    stats.promoted += 1
                except ServiceBusError:
                    # Sent but not removed — duplicate in DLQ + main queue. The
                    # idempotent drain handler collapses it; log and count as
                    # promoted (the message IS back on the main queue).
                    LOGGER.warning(
                        "DLQ promote complete failed seq=%s (re-sent; drain will dedupe)", seq
                    )
                    stats.promoted += 1
            if wrapped:
                break
    return stats



def _safe_drain_handler(
    handler: Callable[[ParsedMessage], MessageAction], parsed: ParsedMessage
) -> MessageAction:
    """Run one drain handler, converting any exception to ABANDON (retry, not lost).

    Partial-failure isolation: a single bad message must never abort the batch
    and must never be silently dropped — abandoning returns it to the broker for
    redelivery on the next tick. Safe to call from a worker thread because it
    does NOT touch the Service Bus receiver / message lock (settlement stays on
    the main thread in :func:`drain_requests`).
    """
    try:
        return handler(parsed)
    except Exception:
        LOGGER.exception(
            "service bus drain handler raised; abandoning message seq=%s",
            parsed.sequence_number,
        )
        return MessageAction.ABANDON


def _run_drain_handlers(
    handler: Callable[[ParsedMessage], MessageAction],
    parsed_messages: list[ParsedMessage],
    pool: ThreadPoolExecutor | None,
) -> list[MessageAction]:
    """Compute the per-message action for one batch, optionally in parallel.

    Returns the actions in the SAME order as ``parsed_messages`` so the caller
    can zip them back to their receiver messages and settle in receiver order on
    the main thread. When ``pool`` is ``None`` (or a single message) the handlers
    run serially — byte-for-byte the legacy behaviour. When a pool is supplied
    the handler bodies (the slow sibling ``/v1/jobs`` submit) run concurrently;
    every handler is wrapped by :func:`_safe_drain_handler`, so ``future.result``
    never raises and one slow/failed submit cannot starve the rest.
    """
    if not parsed_messages:
        return []
    if pool is None or len(parsed_messages) == 1:
        return [_safe_drain_handler(handler, p) for p in parsed_messages]
    futures = [pool.submit(_safe_drain_handler, handler, p) for p in parsed_messages]
    return [f.result() for f in futures]


def drain_requests(
    cfg: ServiceBusConfig | None,
    handler: Callable[[ParsedMessage], MessageAction],
    *,
    max_messages: int,
    max_wait_seconds: int = _RECEIVE_MAX_WAIT_SECONDS,
    max_concurrency: int = 1,
) -> DrainStats:
    """Receive up to ``max_messages`` request messages and settle each one.

    The handler decides the action per message. CRITICAL: the handler must NOT
    block for the duration of a BLAST run — it should create the job + enqueue
    the submit task and return ``COMPLETE`` promptly. Any handler exception
    settles the message as ABANDON (so it is retried, not lost) and is logged;
    one bad message never aborts the whole batch (partial-failure isolation).

    ``max_concurrency`` (default 1 = legacy serial) bounds how many handler
    bodies run at once. The slow part of the handler is the synchronous sibling
    ``/v1/jobs`` submit, so a value >1 lets one tick clear a burst instead of
    serialising N submit latencies. Concurrency applies ONLY to the handler
    bodies: messages are received and **settled on this (main) thread in
    receiver order**, because an Azure Service Bus receiver and its message
    locks are not safe to touch from multiple threads. The per-tick redelivery
    guard (``seen``) is likewise evaluated on the main thread before any handler
    runs, so parallelism never changes which messages are processed — only how
    fast their submits complete.
    """
    cfg = _require_enabled_config(cfg)
    stats = DrainStats()
    budget = max(1, max_messages)
    concurrency = max(1, max_concurrency)
    # An abandoned message becomes immediately receivable again, so without a
    # guard the same message can be re-received within THIS drain tick and burn
    # its whole delivery count (→ premature dead-letter) on a transient handler
    # failure. Track the message ids already settled this tick and stop the loop
    # once we see one again — the message is then retried on the NEXT tick, not
    # spun in a hot loop. Keyed by message_id (falls back to sequence_number).
    seen: set[str] = set()
    pool = (
        ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="sb-drain")
        if concurrency > 1
        else None
    )
    try:
        with _client(cfg) as client, client.get_queue_receiver(
            cfg.request_queue,
            max_wait_time=max_wait_seconds,
        ) as receiver:
            while budget > 0:
                batch = receiver.receive_messages(
                    max_message_count=min(budget, 32),
                    max_wait_time=max_wait_seconds,
                )
                if not batch:
                    break
                # Phase 1 (main thread): apply the per-tick redelivery guard and
                # claim budget. Stops at the first re-seen message so a hot loop
                # cannot burn an abandoned message's delivery count this tick.
                claimed: list[tuple[Any, ParsedMessage]] = []
                wrapped = False
                for message in batch:
                    parsed = _parse(message)
                    ident = str(parsed.message_id or parsed.sequence_number or "")
                    if ident and ident in seen:
                        _safe_abandon(receiver, message)
                        wrapped = True
                        break
                    if ident:
                        seen.add(ident)
                    stats.received += 1
                    budget -= 1
                    claimed.append((message, parsed))
                # Phase 2 (worker threads when concurrency>1): run handler bodies.
                # NEVER settles a message here — only computes the action.
                actions = _run_drain_handlers(
                    handler, [parsed for _m, parsed in claimed], pool
                )
                # Phase 3 (main thread): settle in receiver order.
                for (message, _parsed), action in zip(claimed, actions, strict=True):
                    _settle(receiver, message, action, stats)
                if wrapped:
                    break
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
    return stats


def _settle(receiver: Any, message: Any, action: MessageAction, stats: DrainStats) -> None:
    try:
        if action == MessageAction.COMPLETE:
            receiver.complete_message(message)
            stats.completed += 1
        elif action == MessageAction.DEAD_LETTER:
            receiver.dead_letter_message(message, reason="handler_rejected")
            stats.dead_lettered += 1
        else:
            receiver.abandon_message(message)
            stats.abandoned += 1
    except ServiceBusError:
        # Lock already lost/expired — the broker will redeliver. Count as
        # abandoned for observability; do not raise (best-effort settlement).
        LOGGER.warning("service bus settle failed (lock lost?) action=%s", action)
        stats.abandoned += 1


# --------------------------------------------------------------------------- #
# Management / runtime
# --------------------------------------------------------------------------- #


def _iso_or_none(value: Any) -> str | None:
    """Render an SDK ``datetime`` field as ISO-8601, tolerant of ``None``.

    The admin SDK returns naive UTC datetimes for created/updated/accessed
    timestamps; callers want a JSON-safe ISO string with the ``Z`` suffix.
    """
    if value is None:
        return None
    try:
        # Treat naive timestamps as UTC (matches the SDK's contract).
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=UTC)  # type: ignore[union-attr]
        return value.isoformat().replace("+00:00", "Z")  # type: ignore[union-attr]
    except Exception:
        # Best-effort — a bad timestamp must never break the counts call.
        return None


def pending_request_count(cfg: ServiceBusConfig | None) -> int | None:
    """Active (deliverable) request-queue message count, or ``None``.

    A lightweight, best-effort read of the request queue's
    ``active_message_count`` for the AKS auto-stop evaluator: a Running
    cluster with pending requests still has work in flight even when no
    ``app=blast`` Job exists on the cluster yet (the drain has not bridged
    them to the execution plane). Returns ``None`` -- never raises -- when
    Service Bus is disabled, the credential lacks ``Manage`` / ``EntityRead``
    claims, or the runtime-properties call fails, so the caller degrades to
    the existing state_repo + live-K8s signals (an unreadable queue must
    never strand a cluster running forever). ``scheduled_message_count`` is
    intentionally excluded -- a future-dated message is not immediate work --
    and dead-lettered messages are already excluded by
    ``active_message_count``, so a poison message that exhausts its delivery
    count drops out of this signal and the cluster can idle-stop normally.
    """
    try:
        cfg = _require_enabled_config(cfg)
    except Exception:
        return None
    try:
        with _admin_client(cfg) as admin:
            q = admin.get_queue_runtime_properties(cfg.request_queue)
            return max(0, int(getattr(q, "active_message_count", 0) or 0))
    except Exception:
        LOGGER.debug("pending_request_count unavailable", exc_info=True)
        return None


def entity_counts(cfg: ServiceBusConfig | None) -> dict[str, Any]:
    """Return runtime message counts for the queue (and topic subscriptions).

    Requires ``Manage``/``EntityRead`` claims (Entra ``Azure Service Bus Data
    Owner`` or a Manage SAS rule). When the credential lacks them this raises
    ``ServiceBusAuthError`` and the caller degrades to "counts unavailable".

    The returned ``queue`` dict carries the four message counters the SPA has
    always rendered (active/dead-letter/scheduled/total) plus an additive
    ``telemetry`` block with the richer fields Azure Portal surfaces:
    queue capacity (``size_in_bytes`` / ``max_size_in_mb`` / a derived
    ``size_pct``), transfer counters (``transfer_message_count`` /
    ``transfer_dead_letter_message_count`` — non-zero values are a strong
    forwarding-failure signal), entity ``status`` (Active / Disabled /
    SendDisabled / ReceiveDisabled), and ``accessed_at`` / ``updated_at`` /
    ``created_at`` timestamps so an operator can tell a quiet queue from a
    dead one. Every telemetry field is best-effort — a missing SDK attribute
    silently degrades to ``None`` so an SDK version bump can never break the
    existing counts contract.
    """
    cfg = _require_enabled_config(cfg)
    result: dict[str, Any] = {
        "queue": None,
        "dead_letter": None,
        "subscriptions": [],
        "completion_kind": getattr(cfg, "completion_kind", "topic"),
    }
    with _admin_client(cfg) as admin:
        try:
            q = admin.get_queue_runtime_properties(cfg.request_queue)
            # Try to read the static queue properties too so we know the
            # capacity ceiling (max_size_in_megabytes) and entity status. This
            # is a separate admin call and is bounded by the SDK; if it fails
            # we still return the counts the SPA has always rendered.
            qprops: Any = None
            try:
                qprops = admin.get_queue(cfg.request_queue)
            except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
                # Same auth failure as the counters above — surface it.
                raise ServiceBusAuthError(str(exc)) from exc
            except ServiceBusError:
                LOGGER.debug("queue static properties unavailable", exc_info=True)

            size_in_bytes = getattr(q, "size_in_bytes", None)
            max_size_in_mb = getattr(qprops, "max_size_in_megabytes", None) if qprops else None
            size_pct: float | None = None
            if (
                isinstance(size_in_bytes, int)
                and isinstance(max_size_in_mb, int)
                and max_size_in_mb > 0
            ):
                size_pct = round(size_in_bytes / (max_size_in_mb * 1024 * 1024) * 100, 2)

            result["queue"] = {
                "active_message_count": q.active_message_count,
                "dead_letter_message_count": q.dead_letter_message_count,
                "scheduled_message_count": q.scheduled_message_count,
                "total_message_count": q.total_message_count,
                # Additive telemetry — older SPAs that only read the four
                # counters above keep working unchanged.
                "telemetry": {
                    "size_in_bytes": size_in_bytes,
                    "max_size_in_mb": max_size_in_mb,
                    "size_pct": size_pct,
                    "transfer_message_count": getattr(q, "transfer_message_count", None),
                    "transfer_dead_letter_message_count": getattr(
                        q, "transfer_dead_letter_message_count", None
                    ),
                    "status": str(getattr(qprops, "status", "") or "") if qprops else None,
                    "created_at": _iso_or_none(getattr(q, "created_at_utc", None)),
                    "updated_at": _iso_or_none(getattr(q, "updated_at_utc", None)),
                    "accessed_at": _iso_or_none(getattr(q, "accessed_at_utc", None)),
                },
            }
            result["dead_letter"] = q.dead_letter_message_count
        except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
            raise ServiceBusAuthError(str(exc)) from exc
        if cfg.completion_topic:
            if completion_is_queue(cfg):
                # Queue completion entity: read its runtime counters and surface
                # them as a single pseudo-subscription row named after the queue
                # so the SPA's subscription-list rendering keeps working. There
                # is no fan-out / per-subscription split in queue mode.
                try:
                    cq = admin.get_queue_runtime_properties(cfg.completion_topic)
                    result["subscriptions"].append(
                        {
                            "name": cfg.completion_topic,
                            "active_message_count": cq.active_message_count,
                            "dead_letter_message_count": cq.dead_letter_message_count,
                            "transfer_message_count": getattr(
                                cq, "transfer_message_count", None
                            ),
                            "transfer_dead_letter_message_count": getattr(
                                cq, "transfer_dead_letter_message_count", None
                            ),
                        }
                    )
                except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
                    raise ServiceBusAuthError(str(exc)) from exc
                except ServiceBusError:
                    LOGGER.debug("completion queue counts unavailable", exc_info=True)
            else:
                try:
                    for sub in admin.list_subscriptions(cfg.completion_topic):
                        srt = admin.get_subscription_runtime_properties(
                            cfg.completion_topic, sub.name
                        )
                        result["subscriptions"].append(
                            {
                                "name": sub.name,
                                "active_message_count": srt.active_message_count,
                                "dead_letter_message_count": srt.dead_letter_message_count,
                                # Additive transfer counters per subscription so the
                                # SPA can flag forwarding failures on a per-sub basis.
                                "transfer_message_count": getattr(
                                    srt, "transfer_message_count", None
                                ),
                                "transfer_dead_letter_message_count": getattr(
                                    srt, "transfer_dead_letter_message_count", None
                                ),
                            }
                        )
                except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
                    raise ServiceBusAuthError(str(exc)) from exc
                except ServiceBusError:
                    LOGGER.debug("subscription listing unavailable", exc_info=True)
    return result


def test_connection(cfg: ServiceBusConfig | None) -> dict[str, Any]:
    """Non-destructive reachability probe: peek the request queue.

    Returns ``{reachable, peeked, auth_mode}`` and never raises for an auth
    failure — it reports ``reachable=false`` with a reason so the Settings UI
    can render a status line.
    """
    cfg = _require_enabled_config(cfg)
    try:
        peeked = peek_requests(cfg, max_count=1)
        return {"reachable": True, "peeked": len(peeked), "auth_mode": cfg.auth_mode}
    except ServiceBusAuthError as exc:
        return {
            "reachable": False,
            "reason": "auth_failed",
            "detail": str(exc)[:200],
            "auth_mode": cfg.auth_mode,
        }
    except (ServiceBusUnavailable, ServiceBusError) as exc:
        return {
            "reachable": False,
            "reason": "unreachable",
            "detail": str(exc)[:200],
            "auth_mode": cfg.auth_mode,
        }


def purge_dead_letter(
    cfg: ServiceBusConfig | None,
    *,
    predicate: Callable[[ParsedMessage], bool],
    backup: Callable[[ParsedMessage], bool],
    max_messages: int,
) -> PurgeStats:
    """Receive from the request queue's DLQ and delete messages matching ``predicate``.

    For each matching message, ``backup`` is invoked FIRST; only if it returns
    True is the message completed (deleted). A failed backup keeps the message
    (abandon) so evidence is never lost — there is no unconditional delete here.
    Non-matching messages are abandoned (left in place). Bounded by
    ``max_messages``.
    """
    cfg = _require_enabled_config(cfg)
    stats = PurgeStats()
    budget = max(1, max_messages)
    with _client(cfg) as client, client.get_queue_receiver(
        cfg.request_queue,
        sub_queue=ServiceBusSubQueue.DEAD_LETTER,
        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
        max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
    ) as receiver:
        while budget > 0:
            batch = receiver.receive_messages(
                max_message_count=min(budget, 32),
                max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
            )
            if not batch:
                break
            for message in batch:
                budget -= 1
                stats.scanned += 1
                parsed = _parse(message)
                if not predicate(parsed):
                    _safe_abandon(receiver, message)
                    stats.kept += 1
                    continue
                backed_up = False
                try:
                    backed_up = backup(parsed)
                except Exception:
                    LOGGER.exception("DLQ backup raised; keeping message")
                if backed_up:
                    try:
                        receiver.complete_message(message)
                        stats.purged += 1
                    except ServiceBusError:
                        LOGGER.warning("DLQ complete failed (lock lost?)")
                        stats.kept += 1
                else:
                    _safe_abandon(receiver, message)
                    stats.backup_failed += 1
                    stats.kept += 1
    return stats


def purge_queue(cfg: ServiceBusConfig | None, *, dead_letter: bool, max_messages: int) -> int:
    """Hard-delete messages from the main queue or its DLQ (manual action).

    Used by the Settings "Purge" buttons. The DLQ path here is the
    unconditional variant; the automatic policy path uses ``purge_dead_letter``
    with mandatory backup instead. Bounded by ``max_messages``.
    """
    cfg = _require_enabled_config(cfg)
    removed = 0
    budget = max(1, max_messages)
    sub_queue = ServiceBusSubQueue.DEAD_LETTER if dead_letter else None
    with _client(cfg) as client, client.get_queue_receiver(
        cfg.request_queue,
        sub_queue=sub_queue,
        receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE,
        max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
    ) as receiver:
        while budget > 0:
            batch = receiver.receive_messages(
                max_message_count=min(budget, 64),
                max_wait_time=_RECEIVE_MAX_WAIT_SECONDS,
            )
            if not batch:
                break
            removed += len(batch)
            budget -= len(batch)
    return removed


def _safe_abandon(receiver: Any, message: Any) -> None:
    try:
        receiver.abandon_message(message)
    except ServiceBusError:
        LOGGER.debug("abandon failed (lock lost?)", exc_info=True)


# --------------------------------------------------------------------------- #
# Discovery (ARM + admin client) — Settings dropdowns
# --------------------------------------------------------------------------- #


def discover_namespaces(subscription_id: str) -> list[dict[str, Any]]:
    """List Service Bus namespaces in a subscription via ARM (shared MI)."""
    from api.services.azure_clients import resource_client

    rc = resource_client(get_credential(), subscription_id)
    out: list[dict[str, Any]] = []
    for res in rc.resources.list(filter="resourceType eq 'Microsoft.ServiceBus/namespaces'"):
        name = getattr(res, "name", "") or ""
        out.append(
            {
                "name": name,
                "id": getattr(res, "id", "") or "",
                "location": getattr(res, "location", "") or "",
                "fqdn": f"{name}.servicebus.windows.net" if name else "",
            }
        )
    return out


def discover_entities(cfg: ServiceBusConfig | None) -> dict[str, list[str]]:
    """List queues and topics in the configured namespace via the admin client.

    Requires ``Manage``/``EntityRead`` claims. Raises ``ServiceBusAuthError``
    when the credential lacks them so the route degrades to manual entry.
    """
    cfg = _require_enabled_config(cfg)
    queues: list[str] = []
    topics: list[str] = []
    with _admin_client(cfg) as admin:
        try:
            for q in admin.list_queues():
                queues.append(q.name)
            for t in admin.list_topics():
                topics.append(t.name)
        except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
            raise ServiceBusAuthError(str(exc)) from exc
    return {"queues": queues, "topics": topics}

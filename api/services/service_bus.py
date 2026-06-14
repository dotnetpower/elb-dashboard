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
    ``drain_requests``, ``entity_counts``, ``purge_dead_letter``,
    ``test_connection``, ``MessageAction``.
Risky contracts: Receivers settle EVERY message they receive (complete /
    abandon / dead-letter) — a leaked lock causes redelivery and duplicate BLAST
    runs. ``drain_requests`` and ``purge_dead_letter`` are BOUNDED by
    ``max_messages`` so a backlog can never spin a single tick forever. The SAS
    connection string is read from an env secret or Key Vault and is NEVER
    logged or returned to a caller. All errors are normalised to
    ``ServiceBusUnavailable`` / ``ServiceBusAuthError`` so callers degrade
    instead of leaking SDK internals.
Validation: ``uv run pytest -q api/tests/test_service_bus_drain_loop.py``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
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
from api.services.service_bus_pref import (
    AUTH_MODE_SAS,
    ServiceBusConfig,
    get_service_bus_config,
)

LOGGER = logging.getLogger(__name__)

# Bounds. Drain and purge are explicitly capped so one tick cannot run forever.
_RECEIVE_MAX_WAIT_SECONDS = 5
_PEEK_DEFAULT = 5


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
    return message.message_id or ""


def publish_event(cfg: ServiceBusConfig | None, event: dict[str, Any]) -> None:
    """Publish a transition event to the completion topic.

    No-op (logged) when no completion topic is configured — the integration can
    run request-only without a topic.
    """
    cfg = _require_enabled_config(cfg)
    if not cfg.completion_topic:
        LOGGER.debug("publish_event skipped: no completion_topic configured")
        return
    message = ServiceBusMessage(
        json.dumps(event, default=str),
        content_type="application/json",
        subject=str(event.get("event") or "blast.transition"),
        correlation_id=str(event.get("external_correlation_id") or "") or None,
    )
    with _client(cfg) as client, client.get_topic_sender(cfg.completion_topic) as sender:
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


def drain_requests(
    cfg: ServiceBusConfig | None,
    handler: Callable[[ParsedMessage], MessageAction],
    *,
    max_messages: int,
    max_wait_seconds: int = _RECEIVE_MAX_WAIT_SECONDS,
) -> DrainStats:
    """Receive up to ``max_messages`` request messages and settle each one.

    The handler decides the action per message. CRITICAL: the handler must NOT
    block for the duration of a BLAST run — it should create the job + enqueue
    the submit task and return ``COMPLETE`` promptly. Any handler exception
    settles the message as ABANDON (so it is retried, not lost) and is logged;
    one bad message never aborts the whole batch (partial-failure isolation).
    """
    cfg = _require_enabled_config(cfg)
    stats = DrainStats()
    budget = max(1, max_messages)
    # An abandoned message becomes immediately receivable again, so without a
    # guard the same message can be re-received within THIS drain tick and burn
    # its whole delivery count (→ premature dead-letter) on a transient handler
    # failure. Track the message ids already settled this tick and stop the loop
    # once we see one again — the message is then retried on the NEXT tick, not
    # spun in a hot loop. Keyed by message_id (falls back to sequence_number).
    seen: set[str] = set()
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
            wrapped = False
            for message in batch:
                parsed = _parse(message)
                ident = str(parsed.message_id or parsed.sequence_number or "")
                if ident and ident in seen:
                    # Re-delivery of a message we already abandoned this tick.
                    # Abandon it again WITHOUT counting another attempt-burn and
                    # stop draining — it will be retried next tick.
                    _safe_abandon(receiver, message)
                    wrapped = True
                    break
                if ident:
                    seen.add(ident)
                stats.received += 1
                budget -= 1
                try:
                    action = handler(parsed)
                except Exception:
                    LOGGER.exception(
                        "service bus drain handler raised; abandoning message seq=%s",
                        parsed.sequence_number,
                    )
                    action = MessageAction.ABANDON
                _settle(receiver, message, action, stats)
            if wrapped:
                break
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
    result: dict[str, Any] = {"queue": None, "dead_letter": None, "subscriptions": []}
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

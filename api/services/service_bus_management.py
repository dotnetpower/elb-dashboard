"""Read-only Service Bus entity policy, health, and discovery operations.

Responsibility: Project Service Bus queue/topic runtime properties and discover
    namespaces/entities for settings and operational health consumers.
Edit boundaries: Read-only management-plane operations only. Client creation,
    message send/receive/settlement, and route response shaping remain in
    ``api.services.service_bus`` and route modules.
Key entry points: ``pending_request_count``, ``entity_counts``,
    ``discover_namespaces``, ``discover_entities``.
Risky contracts: The legacy counter keys and additive ``telemetry`` payload must
    remain stable; optional static policy reads degrade without hiding runtime
    counters; auth failures are normalized through the injected domain error;
    pending-count failures return ``None`` so auto-stop does not strand a cluster.
Validation: ``uv run pytest -q api/tests/test_service_bus_entity_counts.py
    api/tests/test_settings_service_bus.py api/tests/test_auto_stop_sb_signal.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

from azure.core.exceptions import ClientAuthenticationError
from azure.servicebus.exceptions import ServiceBusAuthenticationError, ServiceBusError

from api.services.service_bus_pref import ServiceBusConfig

AdminClientFactory = Callable[[ServiceBusConfig], AbstractContextManager[Any]]
ConfigResolver = Callable[[ServiceBusConfig | None], ServiceBusConfig]
AuthErrorFactory = Callable[[str], Exception]


def _iso_or_none(value: Any) -> str | None:
    """Render an SDK datetime field as a UTC ISO-8601 string."""
    if value is None:
        return None
    try:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return str(value.isoformat().replace("+00:00", "Z"))
    except Exception:
        return None


def _duration_seconds(value: Any) -> int | None:
    """Render an SDK timedelta-like property as non-negative seconds."""
    if not isinstance(value, timedelta):
        return None
    return max(0, int(value.total_seconds()))


def _entity_policy(properties: Any) -> dict[str, Any]:
    """Return nullable, payload-free static entity policy fields."""
    if properties is None:
        return {
            "available": False,
            "error": "static_properties_unavailable",
            "default_ttl_seconds": None,
            "dead_letter_on_expiration": None,
            "max_delivery_count": None,
            "lock_duration_seconds": None,
        }
    return {
        "available": True,
        "error": "",
        "default_ttl_seconds": _duration_seconds(
            getattr(properties, "default_message_time_to_live", None)
        ),
        "dead_letter_on_expiration": getattr(
            properties, "dead_lettering_on_message_expiration", None
        ),
        "max_delivery_count": getattr(properties, "max_delivery_count", None),
        "lock_duration_seconds": _duration_seconds(getattr(properties, "lock_duration", None)),
    }


def pending_request_count(
    cfg: ServiceBusConfig | None,
    *,
    require_config: ConfigResolver,
    admin_client: AdminClientFactory,
    logger: logging.Logger,
) -> int | None:
    """Return active plus scheduled request messages, or ``None`` on failure."""
    try:
        resolved = require_config(cfg)
    except Exception:
        return None
    try:
        with admin_client(resolved) as admin:
            queue = admin.get_queue_runtime_properties(resolved.request_queue)
            active = int(getattr(queue, "active_message_count", 0) or 0)
            scheduled = int(getattr(queue, "scheduled_message_count", 0) or 0)
            return max(0, active + scheduled)
    except Exception:
        logger.debug("pending_request_count unavailable", exc_info=True)
        return None


def entity_counts(
    cfg: ServiceBusConfig | None,
    *,
    require_config: ConfigResolver,
    admin_client: AdminClientFactory,
    completion_is_queue: Callable[[ServiceBusConfig], bool],
    auth_error: AuthErrorFactory,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Return queue runtime counters and additive static policy telemetry."""
    resolved = require_config(cfg)
    result: dict[str, Any] = {
        "queue": None,
        "dead_letter": None,
        "subscriptions": [],
        "completion_kind": getattr(resolved, "completion_kind", "topic"),
        "completion_configured": bool(resolved.completion_topic),
        "completion_accessible": None if not resolved.completion_topic else False,
        "completion_error": "",
    }
    with admin_client(resolved) as admin:
        try:
            queue = admin.get_queue_runtime_properties(resolved.request_queue)
            queue_props: Any = None
            try:
                queue_props = admin.get_queue(resolved.request_queue)
            except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
                raise auth_error(str(exc)) from exc
            except ServiceBusError:
                logger.debug("queue static properties unavailable", exc_info=True)

            size_in_bytes = getattr(queue, "size_in_bytes", None)
            max_size_in_mb = (
                getattr(queue_props, "max_size_in_megabytes", None) if queue_props else None
            )
            size_pct: float | None = None
            if (
                isinstance(size_in_bytes, int)
                and isinstance(max_size_in_mb, int)
                and max_size_in_mb > 0
            ):
                size_pct = round(
                    size_in_bytes / (max_size_in_mb * 1024 * 1024) * 100,
                    2,
                )

            result["queue"] = {
                "active_message_count": queue.active_message_count,
                "dead_letter_message_count": queue.dead_letter_message_count,
                "scheduled_message_count": queue.scheduled_message_count,
                "total_message_count": queue.total_message_count,
                "telemetry": {
                    "size_in_bytes": size_in_bytes,
                    "max_size_in_mb": max_size_in_mb,
                    "size_pct": size_pct,
                    "transfer_message_count": getattr(queue, "transfer_message_count", None),
                    "transfer_dead_letter_message_count": getattr(
                        queue, "transfer_dead_letter_message_count", None
                    ),
                    "status": (
                        str(getattr(queue_props, "status", "") or "") if queue_props else None
                    ),
                    "created_at": _iso_or_none(getattr(queue, "created_at_utc", None)),
                    "updated_at": _iso_or_none(getattr(queue, "updated_at_utc", None)),
                    "accessed_at": _iso_or_none(getattr(queue, "accessed_at_utc", None)),
                    "policy": _entity_policy(queue_props),
                },
            }
            result["dead_letter"] = queue.dead_letter_message_count
        except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
            raise auth_error(str(exc)) from exc

        if not resolved.completion_topic:
            return result
        if completion_is_queue(resolved):
            try:
                completion = admin.get_queue_runtime_properties(resolved.completion_topic)
                try:
                    completion_props = admin.get_queue(resolved.completion_topic)
                except (AttributeError, ServiceBusError):
                    completion_props = None
                    logger.debug(
                        "completion queue static properties unavailable",
                        exc_info=True,
                    )
                result["subscriptions"].append(
                    {
                        "name": resolved.completion_topic,
                        "active_message_count": completion.active_message_count,
                        "dead_letter_message_count": completion.dead_letter_message_count,
                        "transfer_message_count": getattr(
                            completion, "transfer_message_count", None
                        ),
                        "transfer_dead_letter_message_count": getattr(
                            completion, "transfer_dead_letter_message_count", None
                        ),
                        "policy": _entity_policy(completion_props),
                    }
                )
                result["completion_accessible"] = True
            except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
                raise auth_error(str(exc)) from exc
            except ServiceBusError as exc:
                result["completion_error"] = type(exc).__name__
                logger.debug("completion queue counts unavailable", exc_info=True)
            return result

        try:
            for subscription in admin.list_subscriptions(resolved.completion_topic):
                runtime = admin.get_subscription_runtime_properties(
                    resolved.completion_topic,
                    subscription.name,
                )
                try:
                    props = admin.get_subscription(
                        resolved.completion_topic,
                        subscription.name,
                    )
                except (AttributeError, ServiceBusError):
                    props = None
                    logger.debug(
                        "completion subscription static properties unavailable name=%s",
                        subscription.name,
                        exc_info=True,
                    )
                result["subscriptions"].append(
                    {
                        "name": subscription.name,
                        "active_message_count": runtime.active_message_count,
                        "dead_letter_message_count": runtime.dead_letter_message_count,
                        "transfer_message_count": getattr(runtime, "transfer_message_count", None),
                        "transfer_dead_letter_message_count": getattr(
                            runtime, "transfer_dead_letter_message_count", None
                        ),
                        "policy": _entity_policy(props),
                    }
                )
            result["completion_accessible"] = True
        except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
            raise auth_error(str(exc)) from exc
        except ServiceBusError as exc:
            result["completion_error"] = type(exc).__name__
            logger.debug("subscription listing unavailable", exc_info=True)
    return result


def discover_namespaces(
    subscription_id: str,
    *,
    credential: Any,
) -> list[dict[str, Any]]:
    """List Service Bus namespaces in a subscription through ARM."""
    from api.services.azure_clients import resource_client

    client = resource_client(credential, subscription_id)
    namespaces: list[dict[str, Any]] = []
    for resource in client.resources.list(
        filter="resourceType eq 'Microsoft.ServiceBus/namespaces'"
    ):
        name = getattr(resource, "name", "") or ""
        namespaces.append(
            {
                "name": name,
                "id": getattr(resource, "id", "") or "",
                "location": getattr(resource, "location", "") or "",
                "fqdn": f"{name}.servicebus.windows.net" if name else "",
            }
        )
    return namespaces


def discover_entities(
    cfg: ServiceBusConfig | None,
    *,
    require_config: ConfigResolver,
    admin_client: AdminClientFactory,
    auth_error: AuthErrorFactory,
) -> dict[str, list[str]]:
    """List queues and topics in the configured namespace."""
    resolved = require_config(cfg)
    queues: list[str] = []
    topics: list[str] = []
    with admin_client(resolved) as admin:
        try:
            queues.extend(queue.name for queue in admin.list_queues())
            topics.extend(topic.name for topic in admin.list_topics())
        except (ServiceBusAuthenticationError, ClientAuthenticationError) as exc:
            raise auth_error(str(exc)) from exc
    return {"queues": queues, "topics": topics}

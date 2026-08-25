"""Service Bus execution-target integrity helpers.

Responsibility: Detect caller-selected routing scope and compare a queued target
    with the active configuration.
Edit boundaries: Pure mapping/config comparison only; no queue, HTTP, task, or
    persistence operations belong here.
Key entry points: ``has_explicit_routing_scope``, ``target_mismatch_fields``.
Risky contracts: Missing target fields remain backward-compatible, while any
    explicit field must match case-insensitively so a request cannot silently run
    on a different subscription, resource group, cluster, or storage account.
Validation: ``uv run pytest -q api/tests/test_submit_ingress.py
    api/tests/test_servicebus_tasks.py api/tests/test_servicebus_v1_multitoken.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from api.services.service_bus_pref import ServiceBusConfig

TARGET_FIELDS = (
    "subscription_id",
    "resource_group",
    "cluster_name",
    "storage_account",
)
ROUTING_SCOPE_FIELDS = TARGET_FIELDS[:3]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def has_explicit_routing_scope(payload: Mapping[str, Any]) -> bool:
    """Return whether a request explicitly selects an OpenAPI cluster scope."""
    return any(_clean(payload.get(field)) for field in ROUTING_SCOPE_FIELDS)


def target_mismatch_fields(payload: Mapping[str, Any], cfg: ServiceBusConfig) -> tuple[str, ...]:
    """Return explicit target fields that differ from the active queue target."""
    mismatches: list[str] = []
    for field in TARGET_FIELDS:
        requested = _clean(payload.get(field))
        if not requested:
            continue
        configured = _clean(getattr(cfg, field, ""))
        if not configured or requested.casefold() != configured.casefold():
            mismatches.append(field)
    return tuple(mismatches)


__all__ = [
    "ROUTING_SCOPE_FIELDS",
    "TARGET_FIELDS",
    "has_explicit_routing_scope",
    "target_mismatch_fields",
]

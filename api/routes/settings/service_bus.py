"""Service Bus integration settings routes.

Responsibility: HTTP shaping for the optional Service Bus BLAST integration —
    read/update the deployment-wide config row, discover namespaces/entities via
    ARM + admin client, run a non-destructive connection test, surface runtime
    counts, and perform operator-triggered manual purges. All long-running and
    SDK work lives in ``api.services.service_bus`` / ``service_bus_pref``.
Edit boundaries: HTTP only — no Service Bus SDK calls inline, no persistence
    logic. Every route enforces ``require_caller``.
Key entry points: ``get_status``, ``put_config``, ``test``, ``discover``,
    ``purge``.
Risky contracts: The SAS connection string is never returned to the browser
    (only the Key Vault secret name). Runtime counts degrade gracefully when the
    credential lacks ``Manage`` claims. ``purge`` is a hard-to-reverse action;
    the confirmation gate is the SPA's responsibility, but the route still caps
    the deletion batch.
Validation: ``uv run pytest -q api/tests/test_settings_service_bus.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.services import service_bus
from api.services.sanitise import redact_oid
from api.services.service_bus_pref import (
    ServiceBusConfig,
    get_service_bus_config,
    normalise_config,
    save_service_bus_config,
    service_bus_enabled,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_PURGE_MAX_CAP = 5000


def _runtime_counts(cfg: ServiceBusConfig) -> dict[str, Any]:
    if not cfg.namespace_fqdn:
        return {"available": False, "reason": "not_configured"}
    try:
        counts = service_bus.entity_counts(cfg)
        return {"available": True, **counts}
    except service_bus.ServiceBusAuthError:
        return {"available": False, "reason": "no_manage_claim"}
    except service_bus.ServiceBusUnavailable as exc:
        return {"available": False, "reason": "unavailable", "detail": str(exc)[:160]}
    except Exception:
        LOGGER.debug("service bus counts failed", exc_info=True)
        return {"available": False, "reason": "error"}


@router.get("")
def get_status(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Return the saved config (no secrets), env gate, and best-effort counts."""
    cfg = get_service_bus_config()
    counts = (
        _runtime_counts(cfg)
        if cfg.enabled
        else {"available": False, "reason": "disabled"}
    )
    return {
        "config": cfg.public_dict(),
        "env_enabled": service_bus_enabled() or cfg.enabled,
        "effective_enabled": service_bus_enabled(),
        "counts": counts,
    }


@router.put("")
def put_config(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Validate and persist the Service Bus integration config."""
    try:
        cfg = normalise_config(body, owner_oid=caller.object_id, tenant_id=caller.tenant_id)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid_config", "message": str(exc)}) from exc
    saved = save_service_bus_config(cfg)
    LOGGER.info(
        "service bus config saved by oid=%s enabled=%s ns=%s mode=%s",
        redact_oid(caller.object_id),
        saved.enabled,
        saved.namespace_fqdn,
        saved.auth_mode,
    )
    return {"status": "saved", "config": saved.public_dict()}


def _transient_config(body: dict[str, Any]) -> ServiceBusConfig:
    """Build an un-saved config from a request body for test/discover."""
    return ServiceBusConfig.from_dict(body)


@router.post("/test")
def test(
    body: dict[str, Any] = Body(default_factory=dict),
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Non-destructive reachability probe (peeks the request queue)."""
    cfg = _transient_config(body) if body else get_service_bus_config()
    if not cfg.namespace_fqdn:
        raise HTTPException(400, detail={"code": "not_configured", "message": "namespace required"})
    return service_bus.test_connection(cfg)


@router.post("/discover")
def discover(
    body: dict[str, Any] = Body(default_factory=dict),
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Discover namespaces (ARM) or queues/topics (admin client).

    With a ``namespace_fqdn`` in the body, list its queues + topics; otherwise
    list the Service Bus namespaces in ``subscription_id``.
    """
    namespace_fqdn = str(body.get("namespace_fqdn") or "").strip()
    if namespace_fqdn:
        cfg = _transient_config(body)
        try:
            return {"namespace_fqdn": namespace_fqdn, **service_bus.discover_entities(cfg)}
        except service_bus.ServiceBusAuthError:
            return {
                "namespace_fqdn": namespace_fqdn,
                "queues": [],
                "topics": [],
                "reason": "no_manage_claim",
            }
    subscription_id = str(body.get("subscription_id") or "").strip()
    if not subscription_id:
        raise HTTPException(
            400,
            detail={
                "code": "subscription_required",
                "message": "subscription_id or namespace_fqdn",
            },
        )
    return {"namespaces": service_bus.discover_namespaces(subscription_id)}


@router.post("/purge")
def purge(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Manual purge of the main queue or its DLQ (operator action)."""
    cfg = get_service_bus_config()
    if not cfg.namespace_fqdn:
        raise HTTPException(400, detail={"code": "not_configured", "message": "namespace required"})
    dead_letter = bool(body.get("dead_letter"))
    try:
        max_messages = int(body.get("max_messages") or _PURGE_MAX_CAP)
    except (TypeError, ValueError):
        max_messages = _PURGE_MAX_CAP
    max_messages = max(1, min(max_messages, _PURGE_MAX_CAP))
    removed = service_bus.purge_queue(cfg, dead_letter=dead_letter, max_messages=max_messages)
    LOGGER.info(
        "service bus manual purge by oid=%s dead_letter=%s removed=%s",
        redact_oid(caller.object_id),
        dead_letter,
        removed,
    )
    return {"status": "purged", "dead_letter": dead_letter, "removed": removed}

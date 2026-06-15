"""Service Bus integration settings routes.

Responsibility: HTTP shaping for the optional Service Bus BLAST integration —
    read/update the deployment-wide config row, discover namespaces/entities via
    ARM + admin client, run a non-destructive connection test, surface runtime
    counts, and perform operator-triggered manual purges. All long-running and
    SDK work lives in ``api.services.service_bus`` / ``service_bus_pref``.
Edit boundaries: HTTP only — no Service Bus SDK calls inline, no persistence
    logic. Every route enforces ``require_caller``.
Key entry points: ``get_status``, ``put_config``, ``test``, ``discover``,
    ``purge``, ``send``.
Risky contracts: The SAS connection string is never returned to the browser
    (only the Key Vault secret name). Runtime counts degrade gracefully when the
    credential lacks ``Manage`` claims. ``purge`` is a hard-to-reverse action;
    the confirmation gate is the SPA's responsibility, but the route still caps
    the deletion batch. ``send`` is intentionally callable by a subscription
    Reader (Playground) — the enqueue runs under the shared MI and never returns
    a SAS token; keep its allowlist entry in ``persona_reader_allowlist.py``.
Validation: ``uv run pytest -q api/tests/test_settings_service_bus.py``.
"""

from __future__ import annotations

import logging
import os
import uuid
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
    service_bus_env_gate_on,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_PURGE_MAX_CAP = 5000

# Playground send backpressure: refuse to enqueue when the request queue's
# pending backlog is already at/over this depth. The dashboard consumer drains
# at a bounded rate (``SERVICEBUS_DRAIN_MAX_MESSAGES`` per tick), and every
# request runs BLAST (AKS compute = real cost), so an unbounded producer — now
# reachable by a Reader via the Playground — could pile up cost. This is a
# best-effort ceiling (a brief admin/counts outage fails open, see
# ``_assert_send_capacity``), not a security control.
_SEND_MAX_QUEUE_DEPTH = max(1, int(os.environ.get("SERVICEBUS_SEND_MAX_QUEUE_DEPTH", "2000")))


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
        # Raw deployment master switch, independent of the saved config. Lets
        # the SPA distinguish "deployment gate OFF" from "namespace missing"
        # when an operator-enabled config is still not live.
        "env_gate_enabled": service_bus_env_gate_on(),
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


def _validate_send_body(body: dict[str, Any]) -> Any:
    """Validate a Playground send body against the matching submit contract.

    Two shapes are accepted, mirroring the queue consumer's routing:

    * A body carrying ``blast_options`` (the sibling ``/v1/jobs`` shape) is
      validated against ``ExternalBlastV1Request`` so a multi-token tabular
      ``outfmt`` (e.g. ``"7 std staxids sstrand qseq sseq"``) + ``extra`` survive
      into the queue message. The consumer routes it to ``/v1/jobs``.
    * Any other body keeps ``ExternalBlastSubmitRequest`` (the XML-locked
      ``/api/v1/elastic-blast/submit`` contract).

    Validating the SAME model the consumer uses means a send accepted here can
    never dead-letter on the consumer for a schema reason. Imported lazily to
    avoid a route-import cycle. Raises ``HTTPException(400)`` on any validation
    error.
    """
    from api.routes.elastic_blast import (
        ExternalBlastSubmitRequest,
        ExternalBlastV1Request,
    )

    model = (
        ExternalBlastV1Request
        if isinstance(body.get("blast_options"), dict)
        else ExternalBlastSubmitRequest
    )
    try:
        return model(**body)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            400,
            detail={"code": "invalid_request", "message": str(exc)[:400]},
        ) from exc


def _record_send_audit(
    caller: CallerIdentity,
    cfg: ServiceBusConfig,
    *,
    correlation_id: str,
    message_id: str,
    program: str,
    db: str,
) -> None:
    """Best-effort durable producer-side audit row for a Playground send.

    Keyed by ``correlation_id`` (the only id known at send time; the consumer
    later creates the jobstate row keyed by the OpenAPI job id, joinable via its
    ``external_correlation_id``). Never raises — an audit failure must not fail
    an already-enqueued send.
    """
    try:
        from api.services.state_repo import get_state_repo

        get_state_repo().append_history(
            correlation_id,
            "servicebus.send",
            {
                "caller_oid": caller.object_id,
                "tenant_id": caller.tenant_id,
                "queue": cfg.request_queue,
                "namespace_fqdn": cfg.namespace_fqdn,
                "message_id": message_id,
                "program": program,
                "db": db,
                "submission_source": "servicebus",
            },
        )
    except Exception:
        LOGGER.debug("servicebus send audit append failed", exc_info=True)


def _assert_send_capacity(cfg: ServiceBusConfig) -> None:
    """Refuse a Playground send when the request queue backlog is too deep.

    Best-effort cost ceiling: reads the queue's pending backlog (active +
    scheduled) and raises HTTP 429 when it is at/over ``_SEND_MAX_QUEUE_DEPTH``.
    Dead-lettered messages are excluded — they are not pending work. A counts
    failure (no Manage claim / namespace momentarily unreachable) fails OPEN
    (logs, proceeds) because this is a ceiling, not a security control, and must
    not block a working integration when the admin plane hiccups.
    """
    try:
        counts = service_bus.entity_counts(cfg)
    except Exception:
        LOGGER.debug("send capacity check skipped (counts unavailable)", exc_info=True)
        return
    queue = counts.get("queue") if isinstance(counts, dict) else None
    if not isinstance(queue, dict):
        return
    active = queue.get("active_message_count")
    scheduled = queue.get("scheduled_message_count")
    backlog = (active if isinstance(active, int) else 0) + (
        scheduled if isinstance(scheduled, int) else 0
    )
    if backlog >= _SEND_MAX_QUEUE_DEPTH:
        raise HTTPException(
            429,
            detail={
                "code": "queue_full",
                "message": (
                    f"Request queue backlog ({backlog}) is at or over the send "
                    f"ceiling ({_SEND_MAX_QUEUE_DEPTH}). Wait for the consumer to "
                    "drain before sending more."
                ),
                "backlog": backlog,
                "limit": _SEND_MAX_QUEUE_DEPTH,
            },
        )


@router.post("/send")
def send(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Enqueue a BLAST request message onto the request queue (Playground).

    Intentionally available to any authenticated caller — including a
    subscription ``Reader`` (see ``api/tests/persona_reader_allowlist.py``). The
    actual enqueue runs under the shared managed identity; no SAS token is ever
    returned to the browser (charter §9). ``dry_run`` validates the body and
    returns without sending so the Playground "Validate" action works offline
    even when the integration is not active. A real send is rejected with 409
    when the integration is off and with 429 when the queue backlog is at the
    send ceiling.
    """
    dry_run = bool(body.pop("dry_run", False))
    # Caller-supplied pass-through tracking value. Popped BEFORE validation so it
    # is never treated as an OpenAPI submit option (it is not part of that
    # contract), then re-attached to the queue message body so the consumer can
    # echo it onto every completion-topic event. Length-bounded to keep the
    # message envelope small.
    request_id = str(body.pop("request_id", "") or "").strip()[:256]
    request = _validate_send_body(body)
    payload: dict[str, Any] = request.model_dump(exclude_none=True)
    correlation_id = str(payload.get("external_correlation_id") or "").strip() or uuid.uuid4().hex
    payload["external_correlation_id"] = correlation_id
    if request_id:
        payload["request_id"] = request_id

    if dry_run:
        # Validation is independent of the data plane — usable offline so an
        # operator can compose/verify a request before activating the
        # integration. Never enqueues, never touches Service Bus.
        return {
            "status": "valid",
            "dry_run": True,
            "external_correlation_id": correlation_id,
            "request_id": request_id,
            "queue": get_service_bus_config().request_queue,
        }

    if not service_bus_enabled():
        raise HTTPException(
            409,
            detail={
                "code": "disabled",
                "message": (
                    "Service Bus integration is not active "
                    "(SERVICEBUS_ENABLED + saved config must both be on)."
                ),
            },
        )

    cfg = get_service_bus_config()
    _assert_send_capacity(cfg)
    try:
        message_id = service_bus.send_request(
            cfg,
            payload,
            correlation_id=correlation_id,
            subject="blast.request",
        )
    except service_bus.ServiceBusUnavailable as exc:
        raise HTTPException(
            503, detail={"code": "unavailable", "message": str(exc)[:200]}
        ) from exc
    except service_bus.ServiceBusAuthError as exc:
        raise HTTPException(
            403, detail={"code": "auth_failed", "message": str(exc)[:200]}
        ) from exc
    except Exception as exc:
        LOGGER.warning("servicebus send failed: %s", type(exc).__name__)
        raise HTTPException(
            502, detail={"code": "send_failed", "message": "send to Service Bus failed"}
        ) from exc

    _record_send_audit(
        caller,
        cfg,
        correlation_id=correlation_id,
        message_id=message_id,
        program=str(payload.get("program") or ""),
        db=str(payload.get("db") or ""),
    )
    LOGGER.info(
        "servicebus playground send by oid=%s corr=%s queue=%s msg_id=%s",
        redact_oid(caller.object_id),
        correlation_id,
        cfg.request_queue,
        message_id,
    )
    return {
        "status": "queued",
        "message_id": message_id,
        "external_correlation_id": correlation_id,
        "request_id": request_id,
        "queue": cfg.request_queue,
    }


@router.get("/observed-completions")
def observed_completions(
    limit: int = 50,
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Recent completion-topic events the demo external consumer observed.

    Read-only (Reader-accessible). Returns the shared Redis observation ring
    written by the worker-side external consumer (default-OFF). When that
    consumer is not running the list is simply empty — the route never errors.
    """
    try:
        from api.services.service_bus_completions import list_recent

        bounded = max(1, min(int(limit), 200))
        events = list_recent(bounded)
    except Exception:
        LOGGER.debug("observed-completions read failed", exc_info=True)
        events = []
    from api.services.service_bus_external_consumer import (
        completion_subscription,
        external_consumer_enabled,
    )

    return {
        "events": events,
        "consumer_enabled": external_consumer_enabled(),
        "subscription": completion_subscription(),
        "topic": get_service_bus_config().completion_topic,
    }


@router.post("/drain")
def drain_now(caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Trigger one real request-queue drain pass immediately (Playground).

    Runs the SAME ``drain_and_resubmit`` the 30 s beat runs, so a sent message
    is picked up and bridged to BLAST execution now instead of waiting for the
    next tick. Synchronous + bounded (the drain caps messages per pass). 409
    when the integration is not active.
    """
    if not service_bus_enabled():
        raise HTTPException(
            409,
            detail={
                "code": "disabled",
                "message": "Service Bus integration is not active.",
            },
        )
    try:
        from api.tasks.servicebus.tasks import drain_and_resubmit

        stats = drain_and_resubmit()
    except Exception as exc:
        LOGGER.warning("servicebus manual drain failed: %s", type(exc).__name__)
        raise HTTPException(
            502, detail={"code": "drain_failed", "message": "drain pass failed"}
        ) from exc
    LOGGER.info(
        "servicebus manual drain by oid=%s stats=%s",
        redact_oid(caller.object_id),
        stats,
    )
    return {"status": "drained", **(stats if isinstance(stats, dict) else {})}

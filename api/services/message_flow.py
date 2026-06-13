"""Message-flow snapshot for the Service Bus visualization card.

Responsibility: Build a single read-only snapshot that powers the dashboard
    "Message Flow" card (Producers -> Broker -> Consumers). It maps the live
    control-plane state onto the three lanes: producers are derived from the
    submitters of currently-active BLAST jobs, the broker boxes are the active
    ``JobState`` rows themselves (sized by query sequence length), and consumers
    are the AKS clusters those jobs target. Service Bus runtime counts ride
    along as a best-effort badge.
Edit boundaries: Pure aggregation/shaping over ``state_repo`` rows and the
    Service Bus config. No HTTP, no FastAPI, no direct ``azure.mgmt.*`` import.
    The HTTP route in ``api.routes.monitor.message_flow`` wraps this and owns
    auth + graceful degradation.
Key entry points: ``build_message_flow``.
Risky contracts: The broker boxes intentionally reflect ACTIVE ``JobState``
    rows (status ``queued``/``running``), NOT raw Service Bus queue messages —
    the request queue drains in under a second so its depth is almost always
    zero (see docs/architecture/service-bus-integration.md). Raw query FASTA is
    never stored on the job row (only sha256 + counts), so ``query_size`` is the
    sequence-letter count, never the raw upload. Submitter aliases come from
    ``owner_upn`` and are shown as-is by the SPA; never emit a raw ``owner_oid``.
    Consumers are grouped by cluster NAME (not the sub/rg/name triple) so a job
    that is queued before its rg/sub are backfilled does not split one logical
    cluster into two cards; the tradeoff is that two genuinely-distinct clusters
    sharing a name across resource groups would merge (acceptable in the
    single-tenant-per-deployment model).
Validation: ``uv run pytest -q api/tests/test_message_flow.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from api.services.sanitise import redact_oid

LOGGER = logging.getLogger(__name__)

# Statuses that mean a job is in flight and therefore worth drawing in the
# broker lane. Anything else (completed/failed/cancelled/deleted) is not an
# "active message".
_ACTIVE_STATUSES = frozenset({"queued", "running"})

# Hard caps so a pathological jobstate table can never make this snapshot
# unbounded. The list read is already limited; these bound the response shape.
# The broker boxes are tiny (textless, tooltip-on-hover) so a larger cap stays
# cheap to render while still being bounded.
_DEFAULT_LIST_LIMIT = 200
_MAX_BROKER_BOXES = 120


def _payload_dict(state: Any) -> dict[str, Any]:
    payload = getattr(state, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _submission_source(payload: dict[str, Any]) -> str:
    """Best-effort server-derived submission source for an active job.

    Looks at the canonical top-level key first, then the nested metadata block
    used by the OpenAPI/Service Bus projection. Defaults to ``dashboard`` which
    is the source of an interactively-submitted job.
    """
    source = payload.get("submission_source")
    if not source:
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            source = metadata.get("submission_source")
    source = str(source or "dashboard").strip().lower()
    if source not in {"dashboard", "external_api", "servicebus"}:
        return "dashboard"
    return source


def _submitter_alias(state: Any, source: str) -> str:
    """Human-facing producer label, safe to render verbatim.

    For non-interactive sources the alias is the source itself so the producer
    lane stays meaningful when there is no signed-in user behind the job. For
    dashboard submissions prefer the UPN (shown as-is per product decision); if
    it is missing fall back to a non-reversible short hash of the object id so
    we never surface a raw GUID.
    """
    if source == "servicebus":
        return "servicebus"
    if source == "external_api":
        return "external"
    upn = (getattr(state, "owner_upn", None) or "").strip()
    if upn:
        return upn
    short = redact_oid(getattr(state, "owner_oid", None))
    return f"user-{short}" if short else "unknown"


def _query_size(payload: dict[str, Any]) -> int | None:
    """Sequence-letter count used to size the broker box.

    The dashboard submit path stores ``query={kind, total_letters, query_count,
    ...}`` (the raw FASTA is uploaded to blob, never inlined on the row), so we
    prefer ``total_letters`` and fall back to the record count. Returns ``None``
    when no size metadata is available so the SPA can render a minimum-width box
    instead of faking a length.
    """
    query = payload.get("query")
    candidates: list[Any] = []
    if isinstance(query, dict):
        candidates.extend([query.get("total_letters"), query.get("query_count")])
    candidates.extend([payload.get("total_letters"), payload.get("query_count")])
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            parsed = int(value)
            if parsed > 0:
                return parsed
    return None


def _counts(cfg: Any) -> dict[str, Any]:
    """Best-effort Service Bus runtime counts; never raises.

    Mirrors the degraded shape the Settings route uses so the SPA can reuse the
    same ``available``/``reason`` handling.
    """
    from api.services import service_bus

    if not getattr(cfg, "namespace_fqdn", ""):
        return {"available": False, "reason": "not_configured"}
    try:
        return {"available": True, **service_bus.entity_counts(cfg)}
    except service_bus.ServiceBusAuthError:
        return {"available": False, "reason": "no_manage_claim"}
    except service_bus.ServiceBusUnavailable as exc:
        return {"available": False, "reason": "unavailable", "detail": str(exc)[:160]}
    except Exception:
        LOGGER.debug("message-flow service bus counts failed", exc_info=True)
        return {"available": False, "reason": "error"}


def _active_rows(caller_oid: str, *, list_limit: int) -> tuple[list[Any], str]:
    """Return ``(active_rows, scope)`` for the caller.

    ``scope`` is ``"shared"`` when the dev shared-visibility flag relaxes the
    per-owner boundary (every submitter's active jobs are visible), else
    ``"own"`` (only the caller's own active jobs). The SPA renders the producer
    lane subtitle from this so an operator is never misled into reading a
    self-only view as a deployment-wide one.
    """
    from api.services.blast.job_state import blast_shared_visibility_enabled
    from api.services.state_repo import get_state_repo

    repo = get_state_repo()
    if blast_shared_visibility_enabled() and hasattr(repo, "list_all"):
        rows = repo.list_all(limit=list_limit, include_payload=True)
        scope = "shared"
    else:
        rows = repo.list_for_owner(caller_oid, limit=list_limit, include_payload=True)
        scope = "own"
    active = [r for r in rows if (getattr(r, "status", "") or "").lower() in _ACTIVE_STATUSES]
    return active, scope


def build_message_flow(
    caller_oid: str,
    *,
    list_limit: int = _DEFAULT_LIST_LIMIT,
    max_boxes: int = _MAX_BROKER_BOXES,
) -> dict[str, Any]:
    """Return the Producers/Broker/Consumers snapshot for the message-flow card.

    When the Service Bus integration is OFF the card is not meant to render, so
    this returns ``{"enabled": False}`` and skips all jobstate work. When ON it
    always returns a full (possibly empty) snapshot; the route layer adds
    graceful degradation around unexpected failures.
    """
    from api.services.service_bus_pref import get_service_bus_config, service_bus_enabled

    if not service_bus_enabled():
        return {"enabled": False}

    cfg = get_service_bus_config()
    active, scope = _active_rows(caller_oid, list_limit=list_limit)
    # When the active set is larger than the table read window the counts below
    # are a floor, not the true total. Surface that honestly via
    # ``read_truncated`` so the SPA can label "showing first N" instead of
    # implying it sees every active job.
    read_truncated = len(active) >= list_limit

    producers: dict[str, dict[str, Any]] = {}
    clusters: dict[str, dict[str, Any]] = {}
    broker: list[dict[str, Any]] = []

    for state in active:
        payload = _payload_dict(state)
        source = _submission_source(payload)
        alias = _submitter_alias(state, source)
        status = (getattr(state, "status", "") or "").lower()

        prod = producers.setdefault(
            alias, {"alias": alias, "job_count": 0, "sources": set()}
        )
        prod["job_count"] += 1
        prod["sources"].add(source)

        sub_id = getattr(state, "subscription_id", None) or ""
        rg = getattr(state, "resource_group", None) or ""
        cluster_name = (getattr(state, "cluster_name", None) or "").strip()
        # Group consumers by cluster NAME, not the (sub, rg, name) triple.
        # A job that is queued but not yet placed on a cluster carries an empty
        # rg/sub; once it starts the same row gains them. Keying on the full
        # triple therefore split one real cluster into two cards ("elb-cluster-01"
        # with an rg AND a second one without), and every not-yet-placed job into
        # its own "unassigned" card. Collapsing on the name (empty name -> a
        # single "unassigned" bucket) keeps one card per logical consumer.
        key = cluster_name or "\x00unassigned"
        cluster = clusters.setdefault(
            key,
            {
                "cluster_name": cluster_name,
                "resource_group": rg,
                "subscription_id": sub_id,
                "running": 0,
                "queued": 0,
                "total": 0,
            },
        )
        # Backfill rg/sub the first time a row for this cluster carries them, so a
        # bucket created from a not-yet-placed job still shows its rg/sub once a
        # running row arrives.
        if not cluster["resource_group"] and rg:
            cluster["resource_group"] = rg
        if not cluster["subscription_id"] and sub_id:
            cluster["subscription_id"] = sub_id
        cluster["total"] += 1
        if status == "running":
            cluster["running"] += 1
        elif status == "queued":
            cluster["queued"] += 1

        if len(broker) < max_boxes:
            broker.append(
                {
                    "job_id": getattr(state, "job_id", ""),
                    "program": getattr(state, "program", None),
                    "db": getattr(state, "db", None),
                    "status": status,
                    "phase": getattr(state, "phase", None),
                    "query_label": getattr(state, "query_label", None),
                    "query_size": _query_size(payload),
                    "alias": alias,
                    "submission_source": source,
                    "cluster_name": cluster_name,
                    "created_at": getattr(state, "created_at", None),
                }
            )

    producer_list = sorted(
        (
            {"alias": p["alias"], "job_count": p["job_count"], "sources": sorted(p["sources"])}
            for p in producers.values()
        ),
        key=lambda p: (-p["job_count"], p["alias"]),
    )
    cluster_list = sorted(
        clusters.values(),
        key=lambda c: (-c["total"], c["cluster_name"]),
    )

    return {
        "enabled": True,
        "scope": scope,
        "namespace_fqdn": getattr(cfg, "namespace_fqdn", ""),
        "request_queue": getattr(cfg, "request_queue", ""),
        "completion_topic": getattr(cfg, "completion_topic", ""),
        "sb_counts": _counts(cfg),
        "active_total": len(active),
        "active_shown": len(broker),
        "broker_truncated": len(active) > len(broker),
        "read_truncated": read_truncated,
        "producers": producer_list,
        "broker": broker,
        "consumers": {"clusters": cluster_list},
    }

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
    Before reading the Table it best-effort syncs external OpenAPI ``/v1/jobs``
    rows in via ``collect_and_sync_external_jobs`` (the same shared orchestration
    the Recent-searches list route uses) so directly-submitted jobs surface on
    the card; that sync is bounded and never raises into the snapshot. The HTTP
    route in ``api.routes.monitor.message_flow`` wraps this and owns auth +
    graceful degradation.
Key entry points: ``build_message_flow``, ``_sync_external_jobs_best_effort``.
Risky contracts: The broker boxes intentionally reflect ACTIVE ``JobState``
    rows (status ``queued``/``pending``/``running``/``reducing`` — the canonical
    in-flight set shared with ``JobStateRepository.list_active`` and the
    auto-stop gate; a ``reducing`` job is still running its result-merge phase
    and MUST keep showing), NOT raw Service Bus queue messages — the request
    queue drains in under a second so its depth is almost always zero (see
    docs/architecture/service-bus-integration.md). Recently-terminal rows
    (``completed``/``failed``/``cancelled`` whose ``updated_at`` is within
    ``_SETTLING_WINDOW_SECONDS``) ride along as ``lifecycle="settling"`` boxes so
    the card does not yank a finished/failed job the instant it leaves the active
    set — the SPA fades them out and shows the terminal status. Settling boxes
    are REAL jobstate rows, never fabricated, and do NOT inflate the
    producer/consumer active counts. Raw query FASTA is
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
import os
from datetime import UTC, datetime
from typing import Any

from api.services.sanitise import redact_oid

LOGGER = logging.getLogger(__name__)

# Statuses that mean a job is in flight and therefore worth drawing in the
# broker lane as a live message. This is the canonical active set shared with
# ``JobStateRepository.list_active`` and ``auto_stop_evaluator`` — keep it in
# sync. A ``reducing`` job is still running its result-merge phase, so dropping
# it here made a long run visibly vanish mid-flight; ``pending`` covers the
# freshly-submitted-but-not-yet-queued window.
_ACTIVE_STATUSES = frozenset({"queued", "pending", "running", "reducing"})

# Active statuses that represent compute actively on the cluster (folded into
# the consumer "running" badge) vs jobs still waiting (folded into "queued").
_RUNNING_LIKE = frozenset({"running", "reducing"})
_QUEUED_LIKE = frozenset({"queued", "pending"})

# Terminal statuses we keep drawing for a short settling window after the job
# leaves the active set, so the card does not yank a finished/failed job the
# instant it completes. ``deleted`` is a soft-delete the repo already filters
# out, so it is intentionally NOT here.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _settling_window_seconds() -> int:
    """How long (seconds) a terminal job lingers as a ``settling`` broker box.

    Configurable via ``MESSAGE_FLOW_SETTLING_SECONDS`` for tuning; defaults to
    90s — long enough for an operator to notice a job finished or failed without
    keeping stale rows on the card indefinitely.
    """
    raw = os.getenv("MESSAGE_FLOW_SETTLING_SECONDS", "").strip()
    if raw.isdigit():
        parsed = int(raw)
        if 0 < parsed <= 3600:
            return parsed
    return 90


# Hard caps so a pathological jobstate table can never make this snapshot
# unbounded. The list read is already limited; these bound the response shape.
# The broker boxes are tiny (textless, tooltip-on-hover) so a larger cap stays
# cheap to render while still being bounded.
_DEFAULT_LIST_LIMIT = 200
_MAX_BROKER_BOXES = 120


def _parse_iso_ms(value: Any) -> float | None:
    """Parse an ISO-8601 timestamp string to epoch ms, or ``None`` when absent
    or unparseable. Tolerates a trailing ``Z`` and naive (UTC-assumed) stamps."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp() * 1000.0


def _is_settling(state: Any, *, now_ms: float, window_ms: float) -> bool:
    """True when a terminal row finished recently enough to keep showing.

    Uses ``updated_at`` (the terminal-transition time) and falls back to
    ``created_at``. A terminal row with no usable timestamp is treated as
    just-finished (shown) rather than hidden, so a clock/serialisation gap never
    silently drops a job the operator was watching.
    """
    finished = _parse_iso_ms(getattr(state, "updated_at", None))
    if finished is None:
        finished = _parse_iso_ms(getattr(state, "created_at", None))
    if finished is None:
        return True
    return (now_ms - finished) <= window_ms



def _payload_dict(state: Any) -> dict[str, Any]:
    payload = getattr(state, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _submission_source(payload: dict[str, Any]) -> str:
    """Best-effort server-derived submission source for an active job.

    Looks at the canonical top-level key first, then the nested metadata block
    used by the OpenAPI/Service Bus projection, then the ``external`` block an
    OpenAPI ``/v1/jobs`` row is synced under (``payload={"external": job}``).
    Defaults to ``dashboard`` for an interactively-submitted job, but to
    ``external_api`` when only the ``external`` block is present — otherwise a
    directly-submitted ``/v1/jobs`` job would mislabel its producer as a
    dashboard user.
    """
    source = payload.get("submission_source")
    external = payload.get("external")
    if not source and isinstance(external, dict):
        source = external.get("submission_source")
    if not source:
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            source = metadata.get("submission_source")
    default = "external_api" if isinstance(external, dict) else "dashboard"
    source = str(source or default).strip().lower()
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


def _visible_rows(
    caller_oid: str, *, list_limit: int
) -> tuple[list[Any], list[Any], str, int]:
    """Return ``(active_rows, settling_rows, scope, total_read)`` for the caller.

    ``scope`` is ``"shared"`` when the dev shared-visibility flag relaxes the
    per-owner boundary (every submitter's active jobs are visible), else
    ``"own"`` (only the caller's own active jobs). The SPA renders the producer
    lane subtitle from this so an operator is never misled into reading a
    self-only view as a deployment-wide one.

    ``settling_rows`` are recently-terminal jobs (completed/failed/cancelled
    within the settling window) so the card can fade a finished/failed job out
    instead of dropping it the instant it leaves the active set. ``total_read``
    is the raw row count returned by the repository, used to detect a truncated
    table read.
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

    now_ms = datetime.now(UTC).timestamp() * 1000.0
    window_ms = _settling_window_seconds() * 1000.0
    active: list[Any] = []
    settling: list[Any] = []
    for r in rows:
        status = (getattr(r, "status", "") or "").lower()
        if status in _ACTIVE_STATUSES:
            active.append(r)
        elif status in _TERMINAL_STATUSES and _is_settling(
            r, now_ms=now_ms, window_ms=window_ms
        ):
            settling.append(r)
    return active, settling, scope, len(rows)


def _sync_external_jobs_best_effort(*, tenant_id: str = "") -> None:
    """Upsert external OpenAPI ``/v1/jobs`` rows for the platform subscription.

    Resolves the platform subscription from ``AZURE_SUBSCRIPTION_ID`` and runs
    the shared discovery+sync orchestration with detail enrichment disabled (the
    card needs only the list-row fields: status / db / cluster / program). The
    orchestration is itself best-effort, but this wrapper also swallows any
    unexpected error so a transient discovery/sync failure can never break the
    Message Flow snapshot. A missing subscription is a no-op.
    """
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    if not subscription_id:
        return
    try:
        from api.services.blast.external_jobs import collect_and_sync_external_jobs

        collect_and_sync_external_jobs(
            subscription_id=subscription_id,
            tenant_id=tenant_id,
            detail_enrich_budget=0,
        )
    except Exception:
        LOGGER.debug("message-flow external jobs sync failed", exc_info=True)


def build_message_flow(
    caller_oid: str,
    *,
    list_limit: int = _DEFAULT_LIST_LIMIT,
    max_boxes: int = _MAX_BROKER_BOXES,
    tenant_id: str = "",
) -> dict[str, Any]:
    """Return the Producers/Broker/Consumers snapshot for the message-flow card.

    When the Service Bus integration is OFF the card is not meant to render, so
    this returns ``{"enabled": False}`` and skips all jobstate work. When ON it
    always returns a full (possibly empty) snapshot; the route layer adds
    graceful degradation around unexpected failures.

    Before reading the Table it best-effort syncs external OpenAPI ``/v1/jobs``
    submissions in (they never create a dashboard Table row on their own), so a
    job submitted directly through the sibling plane appears on the card without
    the operator first opening Recent searches.

    The broker lane is active jobs (``lifecycle="active"``) followed by
    recently-terminal jobs (``lifecycle="settling"``) so a finished/failed job
    fades out instead of vanishing the instant it leaves the active set. Only
    active jobs contribute to the producer/consumer counts; settling jobs are
    drawn but counted separately via ``settling_total``.
    """
    from api.services.service_bus_pref import get_service_bus_config, service_bus_enabled

    if not service_bus_enabled():
        return {"enabled": False}

    # Pull external OpenAPI `/v1/jobs` rows into the Table before reading it.
    # Best-effort and bounded (70 s caches, detail enrichment skipped, a
    # stopped/unreachable cluster degrades to a no-op), so the card surfaces
    # directly-submitted jobs without depending on Recent searches being opened.
    _sync_external_jobs_best_effort(tenant_id=tenant_id)

    cfg = get_service_bus_config()
    active, settling, scope, total_read = _visible_rows(caller_oid, list_limit=list_limit)
    # When the table read window is hit the counts below are a floor, not the
    # true total. Surface that honestly via ``read_truncated`` so the SPA can
    # label "showing first N" instead of implying it sees every active job.
    read_truncated = total_read >= list_limit

    producers: dict[str, dict[str, Any]] = {}
    clusters: dict[str, dict[str, Any]] = {}
    broker: list[dict[str, Any]] = []

    def _ensure_cluster(rg: str, sub_id: str, cluster_name: str) -> dict[str, Any]:
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
                "settling": 0,
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
        return cluster

    def _append_box(state: Any, *, lifecycle: str) -> None:
        payload = _payload_dict(state)
        source = _submission_source(payload)
        alias = _submitter_alias(state, source)
        status = (getattr(state, "status", "") or "").lower()
        cluster_name = (getattr(state, "cluster_name", None) or "").strip()
        if len(broker) >= max_boxes:
            return
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
                "updated_at": getattr(state, "updated_at", None),
                "lifecycle": lifecycle,
                "error_code": getattr(state, "error_code", None) or None,
            }
        )

    # ---- active jobs: full aggregation (producers + consumer counts + boxes) --
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
        cluster = _ensure_cluster(rg, sub_id, cluster_name)
        cluster["total"] += 1
        # Fold reducing into "running" (compute still on the cluster) and pending
        # into "queued" (still waiting) so the two-badge consumer card stays
        # consistent with the broadened active set.
        if status in _RUNNING_LIKE:
            cluster["running"] += 1
        elif status in _QUEUED_LIKE:
            cluster["queued"] += 1

        _append_box(state, lifecycle="active")

    # ---- settling jobs: drawn + cluster node kept (so the link resolves) but
    # they do NOT inflate the producer/consumer active counts. -----------------
    for state in settling:
        sub_id = getattr(state, "subscription_id", None) or ""
        rg = getattr(state, "resource_group", None) or ""
        cluster_name = (getattr(state, "cluster_name", None) or "").strip()
        cluster = _ensure_cluster(rg, sub_id, cluster_name)
        cluster["settling"] += 1
        _append_box(state, lifecycle="settling")

    producer_list = sorted(
        (
            {"alias": p["alias"], "job_count": p["job_count"], "sources": sorted(p["sources"])}
            for p in producers.values()
        ),
        key=lambda p: (-p["job_count"], p["alias"]),
    )
    cluster_list = sorted(
        clusters.values(),
        key=lambda c: (-c["total"], -c["settling"], c["cluster_name"]),
    )

    visible_total = len(active) + len(settling)
    return {
        "enabled": True,
        "scope": scope,
        "namespace_fqdn": getattr(cfg, "namespace_fqdn", ""),
        "request_queue": getattr(cfg, "request_queue", ""),
        "completion_topic": getattr(cfg, "completion_topic", ""),
        "sb_counts": _counts(cfg),
        "active_total": len(active),
        "settling_total": len(settling),
        "active_shown": len(broker),
        "broker_truncated": visible_total > len(broker),
        "read_truncated": read_truncated,
        "producers": producer_list,
        "broker": broker,
        "consumers": {"clusters": cluster_list},
    }


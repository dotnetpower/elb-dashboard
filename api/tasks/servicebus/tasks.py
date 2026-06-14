"""Service Bus integration Celery tasks — drain, publish transitions, DLQ cleanup.

Responsibility: The beat-driven side effects of the optional Service Bus BLAST
    integration. ``drain_and_resubmit`` receives request messages and bridges
    each to the sibling OpenAPI execution plane (``/v1/jobs``), completing the
    message immediately (never holds the lock for the run).
    ``publish_transitions`` polls the sibling status for active bridge rows and
    emits one event per state change to the completion topic.
    ``dlq_cleanup`` enforces the operator's dead-letter retention policy with a
    mandatory audit backup before deletion.
Edit boundaries: Long-running side effects only. Service Bus data-plane calls go
    through ``api.services.service_bus``; the OpenAPI submit/status calls go
    through ``api.services.external_blast``; persistence is
    ``service_bus_pref`` (config) + ``service_bus_tracking`` (bridge rows).
Key entry points: ``drain_and_resubmit``, ``publish_transitions``,
    ``dlq_cleanup`` (registered as ``api.tasks.servicebus.*``).
Risky contracts: Every task no-ops when ``service_bus_enabled()`` is False — the
    env gate plus the saved config must both opt in. The drain handler is
    idempotent on ``external_correlation_id`` (Service Bus is at-least-once);
    a duplicate completes the message without a second submit. All three tasks
    are BOUNDED per tick (drain/publish/cleanup caps) so a backlog drains over
    several ticks instead of spinning one tick forever. Transition events are
    emitted only on an actual status change (``last_status`` marker) so the
    topic does not flood.
Validation: ``uv run pytest -q api/tests/test_servicebus_tasks.py``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from celery import shared_task

from api.services import external_blast, service_bus
from api.services.service_bus import MessageAction, ParsedMessage
from api.services.service_bus_pref import (
    ServiceBusConfig,
    get_service_bus_config,
    service_bus_enabled,
)
from api.services.service_bus_tracking import (
    BridgeRecord,
    get_bridge,
    list_active_bridges,
    mark_done,
    mark_published,
    upsert_bridge,
)
from api.tasks.servicebus.dlq_backup import backup_dead_letter_message

LOGGER = logging.getLogger(__name__)

# Per-tick bounds (self-critique: no unbounded loop). Tunable via env.
_DRAIN_MAX_MESSAGES = int(os.environ.get("SERVICEBUS_DRAIN_MAX_MESSAGES", "50"))
_PUBLISH_MAX_ROWS = int(os.environ.get("SERVICEBUS_PUBLISH_MAX_ROWS", "200"))
# Give-up deadline for a bridge whose sibling job never reaches a terminal
# status — without it a permanently-stuck job's row would stay "active" forever
# and be polled every tick, growing the active set without bound (liveness).
_BRIDGE_MAX_AGE_SECONDS = int(
    os.environ.get("SERVICEBUS_BRIDGE_MAX_AGE_SECONDS", str(7 * 24 * 3600))
)

# External status vocabulary published to subscribers.
_STATUS_QUEUED = "queued"
_STATUS_RUNNING = "running"
_STATUS_SUCCEEDED = "succeeded"
_STATUS_FAILED = "failed"
_TERMINAL = frozenset({_STATUS_SUCCEEDED, _STATUS_FAILED})

_SUCCESS_RAW = frozenset({"complete", "completed", "success", "succeeded"})
_FAILED_RAW = frozenset({"canceled", "cancelled", "error", "failed", "failure", "timeout"})
_QUEUED_RAW = frozenset({"accepted", "created", "pending", "queued", "scheduled"})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _bridge_expired(created_at: str) -> bool:
    """True when a bridge row is older than the give-up deadline.

    Guards against a permanently-stuck sibling job keeping its row "active"
    forever (unbounded active-set growth). A malformed/blank timestamp is
    treated as not-expired so a parse glitch never silently abandons a job.
    """
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (datetime.now(UTC) - created).total_seconds() > _BRIDGE_MAX_AGE_SECONDS


def _classify(raw_status: str) -> str:
    """Map a sibling OpenAPI status onto the external transition vocabulary."""
    s = (raw_status or "").strip().lower()
    if s in _SUCCESS_RAW:
        return _STATUS_SUCCEEDED
    if s in _FAILED_RAW:
        return _STATUS_FAILED
    if s in _QUEUED_RAW:
        return _STATUS_QUEUED
    return _STATUS_RUNNING


def _result_ref(openapi_job_id: str) -> dict[str, str]:
    return {
        "api": f"GET /api/v1/elastic-blast/jobs/{openapi_job_id}",
        "files": f"GET /api/v1/elastic-blast/jobs/{openapi_job_id}/files/{{file_id}}",
    }


def _event_id(correlation_id: str, status: str) -> str:
    """Deterministic id for a (correlation_id, status) completion event.

    At-least-once delivery means a subscriber can receive the same terminal
    transition twice (a publish that succeeded but whose ``mark_done`` write was
    retried, a re-poll after a worker restart, …). A stable ``event_id`` lets the
    external consumer dedupe idempotently without guessing. It is a short
    hex digest of ``corr:status`` — same inputs always yield the same id.
    """
    import hashlib

    return hashlib.sha256(f"{correlation_id}:{status}".encode()).hexdigest()[:32]


def _transition_event(
    *,
    correlation_id: str,
    openapi_job_id: str,
    status: str,
    attempt: int,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build a completion-topic ``blast.transition`` event with idempotency keys.

    Every event carries ``event_id`` (stable per corr+status) and ``attempt``
    (1 on the first publish of this status, ≥2 on a re-publish) so a subscriber
    can dedupe and tell an original from a retry. ``result_ref`` points at the
    dashboard result API (pointers only — never result bytes; charter §9).
    """
    event: dict[str, Any] = {
        "event": "blast.transition",
        "event_id": _event_id(correlation_id, status),
        "attempt": max(1, int(attempt)),
        "external_correlation_id": correlation_id,
        "openapi_job_id": openapi_job_id,
        "status": status,
        "ts": _now_iso(),
        "result_ref": _result_ref(openapi_job_id),
    }
    if error_code:
        event["error_code"] = error_code
    return event


def _record_transition_trace(openapi_job_id: str, status: str) -> None:
    """Record the status stage + ``completion_published`` on a transition.

    Called after a transition event is successfully published to the completion
    topic, so the dashboard's per-job message trace shows running/terminal hops
    and exactly when the result/transition was delivered to subscribers. Keyed
    by ``openapi_job_id`` to match the row the consumer created at drain time.
    Best-effort — never raises into the publish loop.
    """
    if not openapi_job_id:
        return
    try:
        from api.services.blast.message_trace import record_stage
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        # Map the published status vocabulary onto a trace stage.
        if status == _STATUS_RUNNING:
            record_stage(repo, openapi_job_id, "running")
        elif status == _STATUS_SUCCEEDED:
            record_stage(repo, openapi_job_id, "succeeded")
        elif status == _STATUS_FAILED:
            record_stage(repo, openapi_job_id, "failed")
        record_stage(repo, openapi_job_id, "completion_published", status=status)
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.debug("transition trace skipped job=%s: %s", openapi_job_id, type(exc).__name__)



def _openapi_kwargs(cfg: ServiceBusConfig) -> dict[str, str]:
    """Resolve OpenAPI base_url + token from the configured cluster.

    The drain/publish path runs in the worker/beat sidecars, where the
    OpenAPI runtime base-url cache (ephemeral per-revision Redis) may be empty
    after a redeploy. ``external_blast.submit_job`` / ``get_job`` would then
    fail with ``openapi_not_configured``. Resolving the kwargs from the saved
    cluster context (the same helper the dashboard's /api/blast/jobs listing
    uses) makes the integration self-healing: it re-discovers the elb-openapi
    Service IP and re-populates the cache. Returns ``{}`` when the cluster
    context is incomplete; the SDK then falls back to env / cache as before.
    """
    if not (cfg.subscription_id and cfg.resource_group and cfg.cluster_name):
        return {}
    try:
        from api.services.blast.external_jobs import _openapi_client_kwargs_from_cluster

        return _openapi_client_kwargs_from_cluster(
            cfg.subscription_id, cfg.resource_group, cfg.cluster_name
        )
    except Exception:
        LOGGER.debug("openapi kwargs resolution failed", exc_info=True)
        return {}


def _build_request_payload(msg: ParsedMessage, cfg: ServiceBusConfig) -> dict[str, Any] | None:
    """Map a queue message body to a validated OpenAPI submit payload.

    Returns ``None`` when the message cannot ever succeed (malformed / missing
    required fields) — the caller dead-letters it rather than retrying forever.
    """
    from api.routes.elastic_blast import ExternalBlastSubmitRequest
    from api.services.blast.submit_payload import canonical_submit_metadata

    body = dict(msg.body or {})
    correlation_id = (
        str(body.get("external_correlation_id") or "").strip()
        or (msg.correlation_id or "").strip()
        or (msg.message_id or "").strip()
    )
    if not correlation_id:
        return None

    # Build the submit options exactly like the OpenAPI /v1/jobs body: an
    # explicit `options` object wins, and flat convenience keys (word_size,
    # evalue, dust, max_target_seqs, outfmt) are merged in for producers that
    # send a flat message. Only the keys ExternalBlastOptions declares are
    # meaningful; Pydantic ignores any extra (e.g. a stray `searchsp`, which is
    # a local dashboard-submit precision-sharding option NOT part of the
    # OpenAPI contract), so the message stays consistent with /v1/jobs.
    options: dict[str, Any] = {}
    raw_options = body.get("options")
    if isinstance(raw_options, dict):
        options.update(raw_options)
    for key in ("outfmt", "word_size", "dust", "evalue", "max_target_seqs"):
        if key in body and key not in options:
            options[key] = body[key]

    candidate: dict[str, Any] = {
        "query_fasta": body.get("query_fasta"),
        "db": body.get("db"),
        "program": body.get("program") or "blastn",
        "external_correlation_id": correlation_id,
    }
    if options:
        candidate["options"] = options
    # Forward every other top-level ExternalBlastSubmitRequest field the
    # producer set, so a Service Bus submit is byte-for-byte the same shape as
    # a direct POST /api/v1/elastic-blast/submit. `submission_source` and the
    # final `external_correlation_id` are stamped by canonical_submit_metadata
    # below (server-derived; a producer cannot spoof the source).
    for key in (
        "taxid",
        "is_inclusive",
        "priority",
        "batch_len",
        "idempotency_key",
        "resource_profile",
    ):
        if body.get(key) is not None:
            candidate[key] = body[key]

    try:
        request = ExternalBlastSubmitRequest(**candidate)
    except Exception:
        LOGGER.warning("service bus request validation failed corr=%s", correlation_id)
        return None

    payload = request.model_dump(exclude_none=True)
    payload.update(
        canonical_submit_metadata(
            payload,
            submission_source="servicebus",
            correlation_id=correlation_id,
        )
    )
    return payload


def _drain_handler(msg: ParsedMessage, cfg: ServiceBusConfig) -> MessageAction:
    payload = _build_request_payload(msg, cfg)
    if payload is None:
        # Cannot ever succeed → dead-letter (do not loop forever).
        return MessageAction.DEAD_LETTER

    correlation_id = str(payload["external_correlation_id"])
    received_ts = _now_iso()

    # Idempotency: at-least-once delivery means we may see this twice.
    existing = get_bridge(correlation_id)
    if existing is not None:
        LOGGER.info("service bus duplicate request corr=%s (already bridged)", correlation_id)
        return MessageAction.COMPLETE

    try:
        upstream = external_blast.submit_job(payload, **_openapi_kwargs(cfg))
    except Exception:
        LOGGER.exception("service bus → OpenAPI submit failed corr=%s", correlation_id)
        return MessageAction.ABANDON

    openapi_job_id = str(upstream.get("job_id") or "")
    upsert_bridge(
        BridgeRecord(
            correlation_id=correlation_id,
            openapi_job_id=openapi_job_id,
            last_status="",
            done=False,
        )
    )
    # Consumer is the writer: persist the durable jobstate row NOW (at drain
    # time) so the dashboard tracks the job immediately instead of waiting for
    # the periodic ~70 s /v1/jobs discovery poll to create it. Reuses the proven
    # external-jobs sync so the row shape / heal rules stay identical and the
    # later poll is a no-op (same job_id). Also records the message-flow trace
    # stages (enqueued → received → row_created → routed → submitted). Fully
    # best-effort: a tracking-side failure must never abandon an
    # already-accepted submit (that would re-submit on redelivery).
    _persist_drain_row_and_trace(
        cfg,
        payload=payload,
        correlation_id=correlation_id,
        openapi_job_id=openapi_job_id,
        enqueued_at=msg.enqueued_time_utc,
        received_ts=received_ts,
    )
    # Publish the initial "queued" transition (best-effort; a publish failure
    # is recovered by publish_transitions on the next tick).
    try:
        service_bus.publish_event(
            cfg,
            _transition_event(
                correlation_id=correlation_id,
                openapi_job_id=openapi_job_id,
                status=_STATUS_QUEUED,
                attempt=1,
            ),
        )
        mark_published(correlation_id, _STATUS_QUEUED)
    except Exception:
        LOGGER.warning("queued-event publish failed corr=%s (will retry)", correlation_id)
    return MessageAction.COMPLETE


def _persist_drain_row_and_trace(
    cfg: ServiceBusConfig,
    *,
    payload: dict[str, Any],
    correlation_id: str,
    openapi_job_id: str,
    enqueued_at: Any,
    received_ts: str,
) -> None:
    """Create the durable jobstate row + record message-flow trace stages.

    Best-effort: any failure here is logged and swallowed so the drain handler
    still completes the message (the submit already succeeded; abandoning would
    cause a duplicate submit on redelivery). Only runs when an ``openapi_job_id``
    is known (the row is keyed by it, matching the later sync + webhook paths).
    """
    if not openapi_job_id:
        return
    try:
        from api.services.blast.external_jobs import _sync_external_jobs_to_table

        ext_row = {
            "job_id": openapi_job_id,
            "status": _STATUS_QUEUED,
            "program": payload.get("program"),
            "db": payload.get("db"),
            "created_at": received_ts,
            "submission_source": "servicebus",
            "external_correlation_id": correlation_id,
            "cluster_name": getattr(cfg, "cluster_name", "") or "",
        }
        _sync_external_jobs_to_table([ext_row], caller_oid="", tenant_id="")
    except Exception as exc:
        LOGGER.warning(
            "drain jobstate row create failed corr=%s: %s", correlation_id, type(exc).__name__
        )
    try:
        from api.services.blast.message_trace import record_stage
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        record_stage(repo, openapi_job_id, "enqueued", stage_ts=enqueued_at)
        record_stage(repo, openapi_job_id, "received", stage_ts=received_ts)
        record_stage(repo, openapi_job_id, "row_created")
        record_stage(repo, openapi_job_id, "routed", target="openapi")
        record_stage(repo, openapi_job_id, "submitted", openapi_job_id=openapi_job_id)
    except Exception as exc:
        LOGGER.debug(
            "drain trace record skipped corr=%s: %s", correlation_id, type(exc).__name__
        )



@shared_task(name="api.tasks.servicebus.drain_and_resubmit")
def drain_and_resubmit() -> dict[str, Any]:
    """Drain the request queue → bridge each message to the OpenAPI plane."""
    if not service_bus_enabled():
        return {"skipped": "disabled"}
    cfg = get_service_bus_config()
    stats = service_bus.drain_requests(
        cfg,
        lambda m: _drain_handler(m, cfg),
        max_messages=_DRAIN_MAX_MESSAGES,
    )
    return {
        "received": stats.received,
        "completed": stats.completed,
        "abandoned": stats.abandoned,
        "dead_lettered": stats.dead_lettered,
    }


@shared_task(name="api.tasks.servicebus.publish_transitions")
def publish_transitions() -> dict[str, Any]:
    """Poll sibling status for active bridges and emit one event per change."""
    if not service_bus_enabled():
        return {"skipped": "disabled"}
    cfg = get_service_bus_config()
    published = 0
    finished = 0
    scanned = 0
    openapi_kwargs = _openapi_kwargs(cfg)
    for rec in list_active_bridges(limit=_PUBLISH_MAX_ROWS):
        scanned += 1
        if not rec.openapi_job_id:
            # Never bridged to a job id (drain crashed mid-flight). Give up
            # once it ages past the deadline so it cannot linger forever.
            if _bridge_expired(rec.created_at):
                mark_done(rec.correlation_id, _STATUS_FAILED)
                finished += 1
            continue
        try:
            job = external_blast.get_job(rec.openapi_job_id, **openapi_kwargs)
        except Exception:  # transient; retry next tick
            LOGGER.debug("status poll failed corr=%s", rec.correlation_id, exc_info=True)
            continue
        status = _classify(str(job.get("status") or ""))
        if status == rec.last_status:
            # No transition since last publish. If the job has been
            # non-terminal for too long, give up and emit a timeout failure so
            # the active set stays bounded and the subscriber is not left hanging.
            if status not in _TERMINAL and _bridge_expired(rec.created_at):
                timeout_event = _transition_event(
                    correlation_id=rec.correlation_id,
                    openapi_job_id=rec.openapi_job_id,
                    status=_STATUS_FAILED,
                    attempt=1,
                    error_code="bridge_timeout",
                )
                try:
                    service_bus.publish_event(cfg, timeout_event)
                except Exception:  # retry next tick (marker unchanged)
                    LOGGER.warning("timeout publish failed corr=%s", rec.correlation_id)
                    continue
                _record_transition_trace(rec.openapi_job_id, _STATUS_FAILED)
                mark_done(rec.correlation_id, _STATUS_FAILED)
                published += 1
                finished += 1
            continue
        # attempt ≥ 2 only when we are re-publishing a status the marker already
        # records (a publish that succeeded but whose mark write was retried);
        # the normal transition path advances last_status so attempt is 1.
        attempt = 2 if status == rec.last_status else 1
        error_code: str | None = None
        if status == _STATUS_FAILED:
            err = job.get("error") if isinstance(job.get("error"), dict) else {}
            error_code = str((err or {}).get("code") or "failed")
        event = _transition_event(
            correlation_id=rec.correlation_id,
            openapi_job_id=rec.openapi_job_id,
            status=status,
            attempt=attempt,
            error_code=error_code,
        )
        try:
            service_bus.publish_event(cfg, event)
        except Exception:
            LOGGER.warning("transition publish failed corr=%s", rec.correlation_id)
            continue
        _record_transition_trace(rec.openapi_job_id, status)
        published += 1
        if status in _TERMINAL:
            mark_done(rec.correlation_id, status)
            finished += 1
        else:
            mark_published(rec.correlation_id, status)
    return {"scanned": scanned, "published": published, "finished": finished}


def _dlq_predicate(cfg: ServiceBusConfig, total_dlq: int) -> Any:
    """Return a predicate(ParsedMessage) -> bool for cleanup-eligible messages.

    Age-based: enqueued older than ``dlq_max_age_days``. Count-based: when the
    DLQ exceeds ``dlq_max_count`` every scanned message is eligible (oldest-first
    receive order means the excess drains first). OR-combined.
    """
    cutoff = datetime.now(UTC).timestamp() - cfg.dlq_max_age_days * 86400
    over_count = total_dlq > cfg.dlq_max_count

    def predicate(msg: ParsedMessage) -> bool:
        if over_count:
            return True
        enq = msg.enqueued_time_utc
        if enq is None:
            return False
        return enq.timestamp() <= cutoff

    return predicate


@shared_task(name="api.tasks.servicebus.dlq_cleanup")
def dlq_cleanup() -> dict[str, Any]:
    """Enforce the dead-letter retention policy (backup-then-delete)."""
    if not service_bus_enabled():
        return {"skipped": "disabled"}
    cfg = get_service_bus_config()
    if not cfg.dlq_cleanup_enabled:
        return {"skipped": "cleanup_disabled"}

    total_dlq = 0
    try:
        counts = service_bus.entity_counts(cfg)
        total_dlq = int((counts.get("queue") or {}).get("dead_letter_message_count") or 0)
    except service_bus.ServiceBusAuthError:
        # No Manage claim → cannot read counts; fall back to age-only cleanup.
        LOGGER.info("DLQ count unavailable (no Manage claim); age-only cleanup")
    except Exception:
        LOGGER.debug("DLQ count read failed", exc_info=True)

    predicate = _dlq_predicate(cfg, total_dlq)

    def backup(msg: ParsedMessage) -> bool:
        return backup_dead_letter_message(
            {
                "ts": _now_iso(),
                "correlation_id": msg.correlation_id,
                "message_id": msg.message_id,
                "sequence_number": msg.sequence_number,
                "enqueued_time_utc": (
                    msg.enqueued_time_utc.isoformat() if msg.enqueued_time_utc else None
                ),
                "dead_letter_reason": msg.dead_letter_reason,
                "body": msg.body,
            }
        )

    stats = service_bus.purge_dead_letter(
        cfg,
        predicate=predicate,
        backup=backup,
        max_messages=cfg.dlq_cleanup_batch,
    )
    return {
        "scanned": stats.scanned,
        "purged": stats.purged,
        "kept": stats.kept,
        "backup_failed": stats.backup_failed,
        "total_dlq_observed": total_dlq,
    }

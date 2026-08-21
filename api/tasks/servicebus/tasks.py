"""Service Bus integration Celery tasks — drain, responses, transitions, DLQ.

Responsibility: Orchestrate the beat-driven Service Bus BLAST state machines.
    ``drain_and_resubmit`` receives request messages and bridges
    each to the sibling OpenAPI execution plane (``/v1/jobs``), completing the
    message immediately (never holds the lock for the run).
    ``publish_transitions`` polls the sibling status for active bridge rows and
    emits one event per state change to the completion topic.
    ``emit_service_bus_health`` records bounded queue/outbox/admission health.
    ``reconcile_dead_letter_responses`` turns every DLQ outcome into a durable
    producer failure response plus mandatory audit backup before deletion.
    ``dlq_cleanup`` enforces the remaining operator retention policy.
Edit boundaries: Task entry points, request/bridge state transitions, and
    producer-response sequencing only. Redis drain lease and stop-intent
    mechanics live in ``drain_coordination``; request validation and OpenAPI
    payload shaping live in ``request_translation``. Service Bus data-plane
    calls go through ``api.services.service_bus``; OpenAPI calls go through
    ``api.services.external_blast``; persistence is delegated to focused repos.
Key entry points: ``drain_and_resubmit``, ``publish_transitions``,
    ``emit_service_bus_health``, ``reconcile_dead_letter_responses``,
    ``dlq_cleanup`` (registered as ``api.tasks.servicebus.*``).
Risky contracts: Every task no-ops when ``service_bus_enabled()`` is False — the
    env gate plus the saved config must both opt in. All three beat tasks also
    skip the current tick (returning ``{"skipped": "transient"}``) on a
    transient connectivity/DNS error from a top-level Table / Service Bus read,
    so a brief platform blip self-heals on the next tick instead of crashing
    with an exception Celery cannot pickle. The drain handler is
    idempotent on ``external_correlation_id`` plus a canonical execution
    fingerprint (Service Bus is at-least-once). An exact retry replays its
    queued ACK before completing without a second submit; a correlation reused
    for different execution semantics emits a terminal conflict and is
    dead-lettered. All three tasks
    share strict AKS lifecycle/database-warmup admission with the resident
    consumer; blocked drains must not open a receiver, and the handler must
    re-check immediately before submit to close the receive/barrier race.
    A failed start barrier is cancelled only after strict live admission proves
    the target nodes and current per-node warmup have converged; cancellation
    failure leaves the proof path fail-closed on the next tick without blocking
    the already-proven current drain.
    Producer responses are persisted to the response outbox before terminal
    request settlement or bridge terminalisation. Transient submit failures are
    future-scheduled with stable execution identity; DB lifecycle admission
    never receives messages and therefore never consumes retry attempts.
    Parallel drain requires the atomic correlation-id claim. All three tasks
    are BOUNDED per tick (drain/publish/cleanup caps) so a backlog drains over
    several ticks instead of spinning one tick forever. Transition events are
    emitted only on an actual status change (``last_status`` marker) so the
    topic does not flood. A caller-supplied ``request_id`` pass-through value on
    a request message is captured at drain time and echoed onto every published
    transition event (body + topic envelope) so a topic subscriber correlates on
    the same value the producer set. A succeeded transition event additionally
    carries ``result_files`` (per-file metadata + a dashboard ``download_url``
    for the authenticated streaming gateway — pointers only, never a SAS URL or
    result bytes; charter §9). Celery soft deadlines are process-control signals
    and must never be converted into a degraded-success health/admission result.
Validation: ``uv run pytest -q api/tests/test_servicebus_tasks.py
    api/tests/test_service_bus_drain_loop.py api/tests/test_service_bus_outbox.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from billiard.exceptions import SoftTimeLimitExceeded
from celery import shared_task
from fastapi import HTTPException

from api.services import external_blast, service_bus
from api.services.service_bus import MessageAction, ParsedMessage
from api.services.service_bus_health import (
    collect_service_bus_health,
    note_outbox_flush,
)
from api.services.service_bus_observability import (
    record_service_bus_health_event,
    record_service_bus_request_event,
)
from api.services.service_bus_outbox import (
    ResponseOutboxPersistenceError,
    defer_response,
    enqueue_response,
    has_pending_response,
    list_pending_responses,
    mark_response_delivered,
    pending_response_correlations,
)
from api.services.service_bus_pref import (
    ServiceBusConfig,
    get_service_bus_config,
    service_bus_enabled,
)
from api.services.service_bus_tracking import (
    BridgeRecord,
    claim_bridge,
    get_bridge,
    list_active_bridges_page,
    mark_done,
    mark_published,
    release_bridge,
    upsert_bridge,
)
from api.tasks.servicebus import drain_coordination, request_translation
from api.tasks.servicebus.dlq_backup import backup_dead_letter_message
from api.tasks.transient import skip_tick_on_transient_infra

LOGGER = logging.getLogger(__name__)
# Warning-log dedup is intentionally process-local. The worker topology pins
# every ``api.tasks.servicebus.*`` task to the isolated reconcile queue with one
# prefork child (api/run_celery_workers.py); do not treat this as cross-replica
# state if that topology ever changes. The App Insights event still emits every
# five minutes regardless of this log-noise guard.
_LAST_SERVICE_BUS_HEALTH_WARNING = ""
_TRANSITION_CURSOR_KEY = "servicebus:transition-poll:cursor"
_TRANSITION_CURSOR_TTL_SECONDS = 30 * 24 * 60 * 60
_LOCAL_TRANSITION_CURSOR = ""

# Per-tick bounds (self-critique: no unbounded loop). Tunable via env.
_DRAIN_MAX_MESSAGES = int(os.environ.get("SERVICEBUS_DRAIN_MAX_MESSAGES", "50"))


# How many request messages may be bridged to the sibling /v1/jobs plane
# concurrently within one drain tick. Default 1 = legacy serial behaviour
# (charter §12a Rule 4: a new throughput knob ships default-OFF). The slow part
# of the drain handler is the synchronous sibling submit, so raising this clears
# a parallel burst in one tick instead of serialising N submit latencies. Bound
# it (1..32) so a misconfiguration cannot spawn an unbounded thread pool; 32
# matches the receive batch ceiling. Settlement always stays on the main thread
# (see service_bus.drain_requests), so this only parallelises the submit I/O.
def _drain_concurrency_from_env() -> int:
    """Resolve the drain fan-out from env, clamped to [1, 32], fail-safe to 1.

    A non-numeric override must never crash module import (which would take the
    whole worker down on startup); it logs and falls back to the serial default.
    """
    return drain_coordination.drain_concurrency_from_env(LOGGER)


_DRAIN_CONCURRENCY = _drain_concurrency_from_env()

# Atomic single-writer claim gate. It shipped default-OFF, completed its June
# soak/load validation, and now defaults ON. When ON the
# drain reserves each correlation id with an atomic insert BEFORE submitting, so
# a parallel / multi-worker drain can never submit the same request twice (the
# get_bridge → upsert_bridge read-modify-write is otherwise racy). OFF keeps the
# legacy "any existing bridge row dedups" behaviour unchanged. Pair this ON with
# SERVICEBUS_DRAIN_CONCURRENCY>1 — parallel submit is only safe with the claim.
_ATOMIC_CLAIM = os.environ.get("SERVICEBUS_ATOMIC_CLAIM", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
if _DRAIN_CONCURRENCY > 1 and not _ATOMIC_CLAIM:
    LOGGER.error(
        "SERVICEBUS_DRAIN_CONCURRENCY=%d requires SERVICEBUS_ATOMIC_CLAIM=true; "
        "forcing serial drain",
        _DRAIN_CONCURRENCY,
    )
    _DRAIN_CONCURRENCY = 1

# Single-flight drain gate. It shipped default-OFF, completed the same June
# soak/load validation, and now defaults ON. When ON, a drain
# tick takes a short-lived Redis lease before draining so two overlapping beat
# ticks (a tick that ran longer than the 10s interval) or two workers cannot
# drain the same queue at once. The atomic claim (#2) already prevents duplicate
# submits; this just removes the wasted receiver contention / lock churn / log
# noise of N workers racing the same queue. FAIL-OPEN: a Redis error never
# blocks a drain (the lease is an optimisation, not a correctness gate), so a
# broker blip degrades to the legacy every-tick drain instead of stalling.
_DRAIN_SINGLEFLIGHT = os.environ.get(
    "SERVICEBUS_DRAIN_SINGLEFLIGHT", "true"
).strip().lower() in {"1", "true", "yes"}
_DRAIN_LOCK_KEY = "servicebus:drain:singleflight"
_DRAIN_STOP_INTENT_KEY = "servicebus:drain:stop-intent"
_DRAIN_STOP_INTENT_TTL_SECONDS = 300


def _drain_lock_key(queue_name: str) -> str:
    """Queue-scoped lease key so distinct request queues never block each other."""
    return drain_coordination.drain_lock_key(queue_name, base_key=_DRAIN_LOCK_KEY)


def _drain_stop_intent_key(queue_name: str) -> str:
    return drain_coordination.drain_stop_intent_key(
        queue_name,
        base_key=_DRAIN_STOP_INTENT_KEY,
    )


def _drain_lock_ttl_from_env() -> int:
    """Lease TTL in seconds, floored at 10s, fail-safe on a bad value.

    Must exceed a normal tick's drain time so the holder finishes before it
    expires, but stay small enough that a crashed holder (which never runs the
    release) frees the lease quickly. The release is best-effort; the TTL is the
    backstop.
    """
    return drain_coordination.drain_lock_ttl_from_env(LOGGER)


_DRAIN_LOCK_TTL = _drain_lock_ttl_from_env()
# Atomic compare-and-delete so a tick only releases a lease it still owns (never
# one a later tick re-acquired after this one's TTL expired).
_DRAIN_LOCK_RELEASE_LUA = drain_coordination.LOCK_RELEASE_LUA
_DRAIN_LOCK_ACQUIRE_LUA = drain_coordination.LOCK_ACQUIRE_LUA


def _acquire_drain_lock(queue_name: str = "") -> tuple[bool, str | None]:
    """Try to take the single-flight drain lease for ``queue_name``.

    Returns ``(proceed, token)``. ``proceed`` is False ONLY when another drain
    demonstrably holds the lease (skip this tick). It is True both when we won
    the lease (``token`` is the release handle) and when the gate is off or Redis
    is unreachable (``token`` is None → nothing to release, fail-open so a broker
    blip never stalls the drain).
    """
    return drain_coordination.acquire_drain_lock(
        queue_name,
        enabled=_DRAIN_SINGLEFLIGHT,
        lock_ttl=_DRAIN_LOCK_TTL,
        lock_base_key=_DRAIN_LOCK_KEY,
        stop_intent_base_key=_DRAIN_STOP_INTENT_KEY,
        logger=LOGGER,
    )


def _release_drain_lock(token: str | None, queue_name: str = "") -> None:
    """Release the drain lease iff we still own it (best-effort, TTL backstop)."""
    drain_coordination.release_drain_lock(
        token,
        queue_name,
        lock_base_key=_DRAIN_LOCK_KEY,
        logger=LOGGER,
    )


def acquire_drain_stop_intent(queue_name: str) -> tuple[bool, str | None]:
    """Fence new drains before auto-stop checks the active drain lease."""
    return drain_coordination.acquire_drain_stop_intent(
        queue_name,
        lock_base_key=_DRAIN_LOCK_KEY,
        stop_intent_base_key=_DRAIN_STOP_INTENT_KEY,
        stop_intent_ttl=_DRAIN_STOP_INTENT_TTL_SECONDS,
        logger=LOGGER,
    )


def _release_drain_stop_intent(queue_name: str, token: str | None) -> None:
    drain_coordination.release_drain_stop_intent(
        queue_name,
        token,
        stop_intent_base_key=_DRAIN_STOP_INTENT_KEY,
        logger=LOGGER,
    )


def release_drain_stop_intent(queue_name: str, token: str | None) -> None:
    """Release an auto-stop drain fence after stop starts, skips, or fails."""
    _release_drain_stop_intent(queue_name, token)


_PUBLISH_MAX_ROWS = int(os.environ.get("SERVICEBUS_PUBLISH_MAX_ROWS", "20"))
_OUTBOX_MAX_EVENTS = int(os.environ.get("SERVICEBUS_OUTBOX_MAX_EVENTS", "200"))
_DLQ_RESPONSE_MAX_MESSAGES = int(
    os.environ.get("SERVICEBUS_DLQ_RESPONSE_MAX_MESSAGES", "100")
)
# Give-up deadline for a bridge whose sibling job never reaches a terminal
# status — without it a permanently-stuck job's row would stay "active" forever
# and be polled every tick, growing the active set without bound (liveness).
_BRIDGE_MAX_AGE_SECONDS = int(
    os.environ.get("SERVICEBUS_BRIDGE_MAX_AGE_SECONDS", str(7 * 24 * 3600))
)
_RETRY_MAX_ATTEMPTS = max(
    1,
    min(int(os.environ.get("SERVICEBUS_RETRY_MAX_ATTEMPTS", "24")), 100),
)
_RETRY_MAX_AGE_SECONDS = max(
    3600,
    min(int(os.environ.get("SERVICEBUS_RETRY_MAX_AGE_SECONDS", str(24 * 3600))), 7 * 24 * 3600),
)
_LIFECYCLE_INTERRUPTION_SECONDS = max(
    60, int(os.environ.get("SERVICEBUS_LIFECYCLE_INTERRUPTION_SECONDS", "600"))
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


def _transition_cursor() -> str:
    """Read the fair-poll cursor, degrading to process-local state on Redis loss."""
    global _LOCAL_TRANSITION_CURSOR
    if not os.environ.get("CONTAINER_APP_NAME"):
        return _LOCAL_TRANSITION_CURSOR
    try:
        from api.services.redis_clients import get_broker_redis_client

        raw = get_broker_redis_client(socket_timeout=2).get(_TRANSITION_CURSOR_KEY)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")[:512]
        if isinstance(raw, str):
            return raw[:512]
    except Exception:
        LOGGER.debug("transition cursor read degraded to process-local state", exc_info=True)
    return _LOCAL_TRANSITION_CURSOR


def _save_transition_cursor(cursor: str) -> None:
    """Advance the fair-poll cursor best-effort; bridge markers remain authoritative."""
    global _LOCAL_TRANSITION_CURSOR
    _LOCAL_TRANSITION_CURSOR = cursor[:512]
    if not os.environ.get("CONTAINER_APP_NAME"):
        return
    try:
        from api.services.redis_clients import get_broker_redis_client

        get_broker_redis_client(socket_timeout=2).setex(
            _TRANSITION_CURSOR_KEY,
            _TRANSITION_CURSOR_TTL_SECONDS,
            _LOCAL_TRANSITION_CURSOR,
        )
    except Exception:
        LOGGER.debug("transition cursor persist degraded to process-local state", exc_info=True)


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


def _retry_exhausted(msg: ParsedMessage) -> bool:
    """True when a transient request has consumed its bounded retry envelope."""
    if service_bus.retry_would_outlive_request(msg):
        return True
    if msg.retry_attempt >= _RETRY_MAX_ATTEMPTS:
        return True
    raw = msg.first_enqueued_at
    if not raw and msg.enqueued_time_utc is not None:
        first = msg.enqueued_time_utc
    elif raw:
        try:
            first = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
    else:
        return False
    if first.tzinfo is None:
        first = first.replace(tzinfo=UTC)
    return (datetime.now(UTC) - first).total_seconds() >= _RETRY_MAX_AGE_SECONDS


def _retry_terminal_error(msg: ParsedMessage) -> tuple[str, str]:
    """Return the terminal code/detail for an exhausted transient retry."""
    if service_bus.retry_would_outlive_request(msg):
        return (
            "servicebus_request_expired",
            "request could not reach the execution plane before its broker expiry",
        )
    return (
        "servicebus_retry_exhausted",
        "request could not reach the execution plane before the retry deadline",
    )


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


# Cap the number of result-file download links embedded on a single completion
# event so a job that produced an unexpectedly large file list cannot bloat the
# topic message past the Service Bus size limit. A subscriber that needs more
# can still enumerate every file via ``result_ref.api``.
_MAX_RESULT_FILES = 25


def _result_files_for_event(
    job: dict[str, Any], openapi_job_id: str
) -> list[dict[str, Any]]:
    """Build the succeeded event's ``result_files`` with concrete download URLs.

    Each entry carries the file metadata plus a ``download_url`` pointing at the
    dashboard's authenticated file-streaming gateway
    (``GET /api/v1/elastic-blast/jobs/{job_id}/files/{file_id}``). The URL is the
    dashboard's own public base (resolved from the operator setting / Container
    Apps FQDN), NOT a Storage SAS — the ``api`` sidecar streams the bytes
    (charter §9: never hand a SAS / direct Storage URL to a consumer). When URL
    signing is available the link carries a scoped, expiring ``?token=...`` so a
    subscriber can download by URL alone (no bearer / interactive ``az`` login);
    the token authorises exactly this one ``(job_id, file_id)``. When signing is
    disabled the bearer-only URL is emitted unchanged and the consumer supplies a
    bearer token instead. When the dashboard public URL cannot be resolved the
    metadata is still emitted but ``download_url`` is omitted so a subscriber can
    fall back to ``result_ref``.

    Each entry also carries ``compressed`` (the stored file is gzip) and
    ``media_type`` (its as-stored content type) so a consumer can pick a download
    option without a HEAD request: fetch the stored bytes, append
    ``?decompress=1`` to stream the plain file, or append ``?format=csv|tsv|json``
    to have the gateway re-render the same hits (errors come back as a JSON body).
    """
    from api.services.blast.external_job_projection import _external_result_files
    from api.services.control_plane_url import resolve_control_plane_url
    from api.services.download_token import mint_download_token
    from api.services.storage.blob_ids import result_media_type

    files = _external_result_files(job)
    if not files:
        return []
    base, _source = resolve_control_plane_url()
    base = base.rstrip("/")
    out: list[dict[str, Any]] = []
    for item in files[:_MAX_RESULT_FILES]:
        file_id = str(item.get("file_id") or "")
        if not file_id:
            continue
        name = str(item.get("name") or file_id)
        # Compression + media-type metadata lets a consumer decide download
        # options up front (charter §9 keeps bytes flowing through the gateway):
        # gzip results can be fetched as-is, decompressed via ``?decompress=1``,
        # or re-rendered via ``?format=csv|tsv|json`` on the same download_url.
        compressed = name.lower().endswith(".gz")
        entry: dict[str, Any] = {
            "file_id": file_id,
            "name": item.get("name"),
            "format": item.get("format"),
            "size": item.get("size"),
            "compressed": compressed,
            "media_type": result_media_type(name),
        }
        if base:
            url = (
                f"{base}/api/v1/elastic-blast/jobs/{openapi_job_id}/files/{file_id}"
            )
            # Sign the link so a topic consumer can download by URL alone (no
            # bearer / interactive az login). Still the dashboard's own
            # streaming gateway — never a Storage SAS (charter §9). When signing
            # is unavailable/disabled the bearer-only URL is emitted unchanged.
            download_token = mint_download_token(openapi_job_id, file_id)
            if download_token:
                url = f"{url}?token={download_token}"
            entry["download_url"] = url
        out.append(entry)
    return out


def _persist_result_manifest(openapi_job_id: str, job: dict[str, Any]) -> None:
    """Persist a ``file_id -> blob_path`` manifest as a durable JobState column.

    Captured at the succeeded transition while the cluster is up (the elb-openapi
    detail carrying ``result.files[].blob_path`` is in hand) so the download
    route can stream the result straight from Storage when the openapi proxy is
    later unreachable (the cluster auto-stopped). Best-effort: a failure here
    never blocks the completion event — the download just falls back to the
    openapi proxy as before. Blob paths are stored relative to
    ``results/{job_id}/`` (the sibling's contract), so the fallback maps each to
    ``stream_blob_bytes(account, "results", f"{job_id}/{blob_path}")``.
    """
    from api.services.blast.external_job_projection import _external_result_files

    if not openapi_job_id:
        return
    try:
        manifest = [
            {"file_id": str(f["file_id"]), "blob_path": str(f["blob_path"])}
            for f in _external_result_files(job)
            if f.get("file_id") and f.get("blob_path")
        ]
        if not manifest:
            return
        import json as _json

        from api.services.state_repo import get_state_repo

        get_state_repo().update(openapi_job_id, result_manifest=_json.dumps(manifest))
    except Exception:
        LOGGER.debug(
            "result manifest persist skipped job_id=%s", openapi_job_id, exc_info=True
        )


# Bound the pass-through value so a hostile/oversized producer value cannot bloat
# the topic message envelope (Service Bus caps total application-property size).
_REQUEST_ID_MAX_LEN = 256


def _extract_request_id(msg: ParsedMessage) -> str:
    """Extract the caller-supplied ``request_id`` pass-through value, if any.

    Looks first in the JSON body (``request_id``), then falls back to the
    Service Bus application property of the same name (a producer that sets it
    on the message envelope rather than the body). Coerced to a trimmed,
    length-bounded string; returns ``""`` when absent. This value is NEVER
    injected into the OpenAPI submit payload (it is not part of that contract) —
    it only rides the bridge row + completion-topic events so it survives
    end-to-end to a topic subscriber.
    """
    body = msg.body if isinstance(msg.body, dict) else {}
    candidate = body.get("request_id")
    if candidate is None:
        props = msg.application_properties or {}
        candidate = props.get("request_id")
    if candidate is None:
        return ""
    return str(candidate).strip()[:_REQUEST_ID_MAX_LEN]


def _event_id(correlation_id: str, status: str) -> str:
    """Deterministic id for a (correlation_id, status) completion event.

    At-least-once delivery means a subscriber can receive the same terminal
    transition twice (a publish that succeeded but whose ``mark_done`` write was
    retried, a re-poll after a worker restart, …). A stable ``event_id`` lets the
    external consumer dedupe idempotently without guessing. It is a short
    hex digest of ``corr:status`` — same inputs always yield the same id. The
    transition builder may additionally scope request-specific queued/conflict
    acknowledgements so separate ``request_id`` values are not deduplicated.
    """
    return hashlib.sha256(f"{correlation_id}:{status}".encode()).hexdigest()[:32]


_FINGERPRINT_EXCLUDED_FIELDS = frozenset(
    {
        "external_correlation_id",
        "idempotency_key",
        "request_id",
        "results_prefix",
        "submission_source",
    }
)


def _request_fingerprint(payload: dict[str, Any]) -> str:
    """Hash the canonical execution semantics without persisting request data."""
    canonical = {
        key: value
        for key, value in payload.items()
        if key not in _FINGERPRINT_EXCLUDED_FIELDS
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _transition_event(
    *,
    correlation_id: str,
    openapi_job_id: str,
    status: str,
    attempt: int,
    error_code: str | None = None,
    error_message: str | None = None,
    request_id: str = "",
    result_files: list[dict[str, Any]] | None = None,
    event_id_scope: str = "",
) -> dict[str, Any]:
    """Build a completion-topic ``blast.transition`` event with idempotency keys.

    Every event carries a stable ``event_id`` (sha256 digest of ``corr:status``)
    so a subscriber can dedupe an at-least-once re-delivery idempotently — this
    is the authoritative dedup key. ``attempt`` is an informational counter that
    in practice is always 1 (the publish loop cannot distinguish a first publish
    from a re-publish using the bridge marker alone, so it does not try; see
    ``publish_transitions``); it is kept in the schema for stability and for the
    explicit ``attempt=1`` timeout-failure event. Normal lifecycle events use
    ``corr:status``; request-specific queued ACKs and correlation conflicts use
    ``event_id_scope`` so an external event-id deduper preserves each caller's
    acknowledgement without exposing the scope in the event body. ``result_ref`` points at the
    dashboard result API (pointers only — never result bytes; charter §9).
    ``result_files`` (succeeded events only) carries the per-file metadata plus a
    concrete ``download_url`` for the dashboard's authenticated streaming gateway
    so a subscriber can download results directly — still pointers, never bytes
    or SAS URLs. ``request_id`` is the caller-supplied pass-through value from the
    request queue message; it is echoed onto the event (and the topic envelope)
    only when the producer set one, so a subscriber correlates on the SAME value.

    On a failure event ``error_code`` is a short machine-readable reason and
    ``error_message`` is a human-readable detail (sanitised + length-bounded by
    ``_error_message_for_event`` before it reaches this builder) so a subscriber
    sees WHY a job failed, not just THAT it failed.
    """
    event: dict[str, Any] = {
        "event": "blast.transition",
        "event_id": hashlib.sha256(
            f"{correlation_id}:{status}:{event_id_scope}".encode()
        ).hexdigest()[:32]
        if event_id_scope
        else _event_id(correlation_id, status),
        "attempt": max(1, int(attempt)),
        "external_correlation_id": correlation_id,
        "openapi_job_id": openapi_job_id,
        "status": status,
        "ts": _now_iso(),
        "result_ref": _result_ref(openapi_job_id),
    }
    if result_files is not None:
        event["result_files"] = result_files
    if request_id:
        event["request_id"] = request_id
    if error_code:
        event["error_code"] = error_code
    if error_message:
        event["error_message"] = error_message
    return event


# Cap the human-readable error detail embedded on a failure event so a verbose
# sibling stack/message cannot bloat the topic envelope past the Service Bus
# size limit (mirrors ``_REQUEST_ID_MAX_LEN``).
_ERROR_MESSAGE_MAX_LEN = 500


def _error_message_for_event(job: dict[str, Any]) -> str:
    """Extract a sanitised, length-bounded human-readable failure detail.

    Reads the sibling job's ``error.message`` (or ``error.detail`` fallback),
    runs it through ``sanitise`` so a token / subscription id / SAS URL can never
    leak onto the completion topic (charter §12), and trims it to
    ``_ERROR_MESSAGE_MAX_LEN``. Returns ``""`` when no detail is present.
    """
    err = job.get("error") if isinstance(job.get("error"), dict) else {}
    raw = str((err or {}).get("message") or (err or {}).get("detail") or "").strip()
    if not raw:
        return ""
    from api.services.sanitise import sanitise

    return str(sanitise(raw))[:_ERROR_MESSAGE_MAX_LEN]


def _enrich_failure_message_for_event(
    job: dict[str, Any], openapi_job_id: str, current_error: str | None
) -> str | None:
    """Recover the authoritative cluster-side blastn failure detail for an event.

    The sibling OpenAPI service stamps only a coarse/generic ``error`` on a
    failed job (``one or more BLAST jobs failed``, a bare Kubernetes
    ``CrashLoopBackOff``, or nothing at all) — that tells a completion-topic
    subscriber THAT the job failed, not WHY. The real diagnostics live in the
    workload results container (``metadata/FAILURE.txt`` +
    ``logs/BLAST_RUNTIME-NNN.out``); the dashboard detail view already recovers
    them via ``_enrich_external_failure_detail``. Reuse that same helper here so
    the failure event carries an actionable cause instead of a placeholder.

    The workload Storage account is resolved from the sibling job's ``db`` blob
    URL through ``extract_trusted_storage_account`` — the same trust gate the
    dashboard uses, so an attacker-influenced ``db`` URL can never redirect the
    shared MI Storage token to a foreign account. Best-effort and side-effect
    free: returns the sanitised, length-bounded detail, or ``None`` when the
    account cannot be trusted-resolved or no better detail is readable (the
    caller then keeps the coarse message). Only fires for a genuinely
    coarse/generic ``current_error`` — ``_enrich_external_failure_detail``
    leaves a specific sibling error untouched.
    """
    if not openapi_job_id:
        return None
    try:
        from api.services.blast.db_metadata import extract_trusted_storage_account
        from api.services.blast.external_job_projection import (
            _enrich_external_failure_detail,
        )

        storage_account = extract_trusted_storage_account(str(job.get("db") or ""))
        if not storage_account:
            return None
        return cast(
            str | None,
            _enrich_external_failure_detail(
                status="failed",
                current_error=current_error,
                storage_account=storage_account,
                results_job_id=openapi_job_id,
            ),
        )
    except Exception:
        LOGGER.debug(
            "completion failure-detail enrichment skipped job=%s",
            openapi_job_id,
            exc_info=True,
        )
        return None


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

        return cast(
            dict[str, str],
            _openapi_client_kwargs_from_cluster(
                cfg.subscription_id, cfg.resource_group, cfg.cluster_name
            ),
        )
    except Exception:
        LOGGER.debug("openapi kwargs resolution failed", exc_info=True)
        return {}


def _resolve_drain_cluster_context(cfg: ServiceBusConfig) -> tuple[str, str, str]:
    """Resolve ``(subscription_id, resource_group, cluster_name)`` for a drained job.

    The SB config routing is often blank (the integration falls back to the
    runtime cache to find the OpenAPI endpoint), which left the durable job row
    with no cluster scope, so the detail showed "Region —" until the periodic
    scope-backfill poll ran. Prefer the explicit SB config routing; otherwise
    discover the single ElasticBLAST cluster in the dashboard's subscription
    (cached ARM call, shared with the jobs-list discovery). Returns blanks for
    the ambiguous (multi-cluster) / unresolvable case so the existing
    scope-backfill still fills it later — never raises.
    """
    sub = str(getattr(cfg, "subscription_id", "") or "").strip()
    rg = str(getattr(cfg, "resource_group", "") or "").strip()
    cluster = str(getattr(cfg, "cluster_name", "") or "").strip()
    if sub and rg and cluster:
        return (sub, rg, cluster)
    if not sub:
        import os

        sub = str(os.environ.get("AZURE_SUBSCRIPTION_ID", "") or "").strip()
    if not sub:
        return (sub, rg, cluster)
    try:
        from api.services.blast.external_jobs import _discover_subscription_clusters

        pairs = _discover_subscription_clusters(sub)
        # Only stamp when the subscription has exactly ONE running ElasticBLAST
        # cluster — otherwise we cannot know which one this job ran on, so leave
        # it for the scope-backfill (which uses the per-target poll context).
        if len(pairs) == 1:
            _rg, _cluster = pairs[0]
            return (sub, rg or _rg, cluster or _cluster)
    except Exception:
        LOGGER.debug("drain cluster context discovery failed", exc_info=True)
    return (sub, rg, cluster)


def _execution_admission_for_drain(cfg: ServiceBusConfig) -> dict[str, Any]:
    """Return the shared pre-receive/pre-submit execution admission decision.

    Both the beat task and the resident consumer call this helper before opening
    a receiver. ``_drain_handler`` calls it again immediately before the sibling
    submit, closing the race where a lifecycle barrier is created while a
    long-poll receiver already holds messages.
    """
    from api.services.aks.execution_admission import evaluate_execution_admission

    subscription_id, resource_group, cluster_name = _resolve_drain_cluster_context(cfg)
    decision = evaluate_execution_admission(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    if decision.get("allowed") and not _openapi_ready_for_drain(cfg):
        return {
            "allowed": False,
            "reason": "openapi_not_ready",
            "retry_after_seconds": 10,
        }
    return dict(decision)


def _reconcile_recovered_start_failure(admission: dict[str, Any]) -> bool:
    """Cancel only the failed start token whose live state just converged."""
    if not (
        admission.get("allowed")
        and admission.get("recovered_lifecycle_failure")
        and admission.get("lifecycle_action") == "start"
    ):
        return False
    token = str(admission.get("barrier_token") or "").strip()
    if not token:
        return False
    try:
        from api.services.aks.execution_admission import cancel_lifecycle_barrier

        cancel_lifecycle_barrier(token, reason="start_failure_live_reconciled")
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        from api.services.log_dedup import dedup_log_warning

        dedup_log_warning(
            LOGGER,
            ("servicebus_admission_recovery", token[:12], type(exc).__name__),
            "servicebus admission recovery state write failed token=%s error=%s",
            token[:12],
            type(exc).__name__,
        )
        return False

    LOGGER.warning(
        "servicebus admission recovered failed start token=%s target_nodes=%s",
        token[:12],
        admission.get("target_node_count", ""),
    )
    try:
        from api.services.feature_events import record_feature_event

        record_feature_event(
            "servicebus_admission_recovery",
            status="completed",
            lifecycle_action="start",
            barrier_token=token[:12],
            target_node_count=int(admission.get("target_node_count") or 0),
        )
    except Exception:
        LOGGER.debug("servicebus admission recovery telemetry failed", exc_info=True)
    return True


def _build_request_payload(msg: ParsedMessage, cfg: ServiceBusConfig) -> dict[str, Any] | None:
    """Compatibility facade for XML-path Service Bus request translation."""
    return request_translation.build_request_payload(msg, cfg, logger=LOGGER)


def _is_v1_jobs_message(body: dict[str, Any]) -> bool:
    """Compatibility facade for Service Bus submit-path selection."""
    return request_translation.is_v1_jobs_message(body)


def _build_v1_jobs_payload(
    msg: ParsedMessage, cfg: ServiceBusConfig
) -> dict[str, Any] | None:
    """Compatibility facade for free-form Service Bus request translation."""
    return request_translation.build_v1_jobs_payload(msg, cfg, logger=LOGGER)


def _supersede_placeholder(correlation_id: str) -> None:
    """Soft-delete the send-time placeholder once the real row exists. Best-effort."""
    try:
        from api.services.blast.servicebus_placeholder import supersede_placeholder

        supersede_placeholder(correlation_id)
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.debug(
            "placeholder supersede skipped corr=%s: %s", correlation_id, type(exc).__name__
        )


def _fail_placeholder(correlation_id: str, *, error_code: str) -> None:
    """Mark the send-time placeholder failed on a terminal rejection. Best-effort."""
    try:
        from api.services.blast.servicebus_placeholder import fail_placeholder

        fail_placeholder(correlation_id, error_code=error_code)
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.debug("placeholder fail skipped corr=%s: %s", correlation_id, type(exc).__name__)


def _correlation_id_from_message(msg: ParsedMessage) -> str:
    """Recover the correlation id from a raw message the same way the payload
    builders do (body ``external_correlation_id`` → SB ``correlation_id`` →
    ``message_id``). Used to fail the placeholder for a malformed message whose
    payload could not be built."""
    body = dict(msg.body or {})
    return (
        str(body.get("external_correlation_id") or "").strip()
        or (msg.correlation_id or "").strip()
        or (msg.message_id or "").strip()
        or (f"sb-sequence-{msg.sequence_number}" if msg.sequence_number is not None else "")
    )


def _fail_placeholder_for_message(msg: ParsedMessage, *, error_code: str) -> None:
    """Fail the placeholder for a message whose payload could not be built."""
    correlation_id = _correlation_id_from_message(msg)
    if correlation_id:
        _fail_placeholder(correlation_id, error_code=error_code)


def _publish_drain_failure_event(
    cfg: ServiceBusConfig,
    *,
    correlation_id: str,
    request_id: str,
    error_code: str,
    error_message: str,
) -> bool:
    """Publish a terminal ``failed`` transition for a drain-time rejection.

    A message rejected BEFORE it bridges to a sibling job (malformed body, or a
    permanent 4xx submit rejection) never reaches ``publish_transitions`` — there
    is no bridge row to poll — so without this the completion topic would stay
    silent and a subscriber would wait forever. This emits the same
    ``blast.transition`` failure shape (``status=failed`` + ``error_code`` +
    sanitised ``error_message``) with an empty ``openapi_job_id`` (no job was
    created). Best-effort: a publish failure is logged and swallowed so it never
    changes the message-settlement decision (the placeholder is already failed
    and the message is being dead-lettered regardless). ``publish_event`` no-ops
    when no completion topic is configured.
    """
    if not correlation_id:
        return False
    try:
        from api.services.sanitise import sanitise

        event = _transition_event(
            correlation_id=correlation_id,
            openapi_job_id="",
            status=_STATUS_FAILED,
            attempt=1,
            error_code=error_code,
            error_message=(str(sanitise(error_message))[:_ERROR_MESSAGE_MAX_LEN] or None)
            if error_message
            else None,
            request_id=request_id,
        )
        durable, _delivered = _stage_response_event(cfg, event)
        return durable
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.warning(
            "drain failure response staging failed corr=%s: %s",
            correlation_id,
            type(exc).__name__,
        )
        return False


def _publish_duplicate_ack(
    cfg: ServiceBusConfig,
    existing: BridgeRecord,
    *,
    request_id: str,
) -> bool:
    """Re-publish the accepted ACK for an idempotent request redelivery."""
    durable, _delivered = _stage_response_event(
        cfg,
        _transition_event(
            correlation_id=existing.correlation_id,
            openapi_job_id=existing.openapi_job_id,
            status=_STATUS_QUEUED,
            attempt=1,
            request_id=request_id,
            event_id_scope=request_id,
        ),
    )
    return durable


def _publish_correlation_conflict(
    cfg: ServiceBusConfig,
    existing: BridgeRecord,
    *,
    request_id: str,
    request_fingerprint: str,
) -> bool:
    """Publish a sanitized terminal rejection for a reused correlation id."""
    durable, _delivered = _stage_response_event(
        cfg,
        _transition_event(
            correlation_id=existing.correlation_id,
            openapi_job_id="",
            status=_STATUS_FAILED,
            attempt=1,
            error_code="servicebus_correlation_conflict",
            error_message="external_correlation_id is already bound to a different request",
            request_id=request_id,
            event_id_scope=request_id or request_fingerprint,
        ),
    )
    return durable


def _detail_text(exc: HTTPException) -> str:
    """Coerce an HTTPException detail (str or ``{code,message}`` dict) to text."""
    detail = getattr(exc, "detail", "")
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or detail.get("code") or "")
    return str(detail or "")


def _dead_letter_action(
    msg: ParsedMessage,
    *,
    reason: str,
    description: str,
) -> MessageAction:
    """Attach a bounded broker-visible terminal reason to one disposition."""
    from api.services.sanitise import sanitise

    msg.settlement_reason = reason.strip()[:128] or "handler_rejected"
    msg.settlement_description = str(sanitise(description.strip()))[:1024]
    return MessageAction.DEAD_LETTER


def _transient_action(msg: ParsedMessage) -> MessageAction:
    """Schedule bounded redelivery without consuming broker delivery count."""
    return MessageAction.ABANDON if _retry_exhausted(msg) else MessageAction.RETRY


def _publish_jobs_cache_invalidate(reason: str) -> None:
    """Drop the api sidecar's jobs / message-flow caches cross-process.

    The drain runs in the worker sidecar and writes the durable jobstate row
    there, so it cannot reach the api process's in-process jobs-list /
    message-flow / external-jobs caches. Publishing the invalidation signal lets
    the api subscriber drop them so a queue-ingested job (or a placeholder
    status change) surfaces on the next poll instead of waiting out the cache
    TTL. Best-effort — never raises into the drain handler.
    """
    try:
        from api.services.blast.jobs_cache_signal import publish_jobs_cache_invalidate

        publish_jobs_cache_invalidate(reason)
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.debug("jobs cache invalidate publish skipped: %s", type(exc).__name__)


def _record_drain_request_event(
    stage: str,
    msg: ParsedMessage,
    cfg: ServiceBusConfig,
    *,
    payload: dict[str, Any] | None = None,
    openapi_job_id: str = "",
    action: str = "",
    error_code: str = "",
    ack_published: bool | None = None,
) -> None:
    """Record one drain decision using only bounded scalar request metadata."""
    values = payload or {}
    body = msg.body if isinstance(msg.body, dict) else {}
    taxid = values.get("taxid")
    inclusive = values.get("is_inclusive")
    record_service_bus_request_event(
        stage,
        correlation_id=str(
            values.get("external_correlation_id") or _correlation_id_from_message(msg)
        ),
        request_id=_extract_request_id(msg),
        message_id=str(msg.message_id or ""),
        queue=cfg.request_queue,
        openapi_job_id=openapi_job_id,
        program=str(values.get("program") or body.get("program") or ""),
        database=str(values.get("db") or body.get("db") or ""),
        taxid=taxid if isinstance(taxid, int) else None,
        is_inclusive=inclusive if isinstance(inclusive, bool) else None,
        action=action,
        error_code=error_code,
        delivery_count=msg.delivery_count,
        sequence_number=msg.sequence_number,
        ack_published=ack_published,
    )


def _stage_response_event(
    cfg: ServiceBusConfig,
    event: dict[str, Any],
    *,
    deliver_immediately: bool = True,
) -> tuple[bool, bool]:
    """Persist one producer response, then attempt immediate delivery."""
    event_id = str(event.get("event_id") or "")
    try:
        enqueue_response(event)
    except (ResponseOutboxPersistenceError, ValueError) as exc:
        LOGGER.error(
            "producer response outbox persist failed event_id=%s error=%s",
            event_id,
            type(exc).__name__,
        )
        return (False, False)
    if not cfg.completion_topic or not deliver_immediately:
        return (True, False)
    try:
        service_bus.publish_event(cfg, event)
    except Exception:
        LOGGER.warning(
            "producer response publish deferred to outbox event_id=%s",
            event_id,
        )
        return (True, False)
    try:
        mark_response_delivered(event_id)
    except Exception:
        LOGGER.warning(
            "producer response outbox confirm failed event_id=%s; may redeliver",
            event_id,
        )
    return (True, True)


def _flush_response_outbox(cfg: ServiceBusConfig) -> dict[str, int]:
    """Publish a bounded oldest-first pass of durable producer responses."""
    stats = {"scanned": 0, "delivered": 0, "errors": 0}
    if not cfg.completion_topic:
        note_outbox_flush(stats, attempted=False)
        return stats
    try:
        pending = list_pending_responses(limit=_OUTBOX_MAX_EVENTS)
    except Exception:
        LOGGER.warning("producer response outbox list failed", exc_info=True)
        stats["errors"] = 1
        note_outbox_flush(stats)
        return stats
    blocked_correlations: set[str] = set()
    for item in pending:
        stats["scanned"] += 1
        correlation_id = str(item.event.get("external_correlation_id") or "")
        if correlation_id and correlation_id in blocked_correlations:
            continue
        if item.next_attempt_at:
            try:
                next_attempt = datetime.fromisoformat(item.next_attempt_at.replace("Z", "+00:00"))
                if next_attempt.tzinfo is None:
                    next_attempt = next_attempt.replace(tzinfo=UTC)
                if datetime.now(UTC) < next_attempt:
                    if correlation_id:
                        blocked_correlations.add(correlation_id)
                    continue
            except ValueError:
                LOGGER.warning(
                    "producer response has invalid next_attempt_at event_id=%s",
                    item.event_id,
                )
                stats["errors"] += 1
                try:
                    defer_response(
                        item.event_id,
                        error_code="deferred_timestamp_corrupt",
                        retry_after_seconds=300,
                    )
                except Exception:
                    LOGGER.warning("producer response timestamp repair failed", exc_info=True)
                    break
                if correlation_id:
                    blocked_correlations.add(correlation_id)
                continue
        try:
            service_bus.publish_event(cfg, item.event)
            mark_response_delivered(item.event_id)
            stats["delivered"] += 1
        except service_bus.ServiceBusEventValidationError:
            LOGGER.warning(
                "producer response outbox poison event isolated event_id=%s",
                item.event_id,
            )
            stats["errors"] += 1
            # Preserve the stable event_id and state transition while dropping
            # optional/high-volume fields. The result_ref remains the durable
            # claim-check path, so the producer still receives a useful outcome.
            compact_event = {
                key: item.event[key]
                for key in (
                    "event",
                    "event_id",
                    "external_correlation_id",
                    "openapi_job_id",
                    "status",
                    "phase",
                    "error_code",
                    "ts",
                    "result_ref",
                    "request_id",
                    "attempt",
                )
                if key in item.event
            }
            try:
                service_bus.validate_completion_event(compact_event)
            except service_bus.ServiceBusEventValidationError:
                LOGGER.error(
                    "producer response cannot fit even after compaction event_id=%s",
                    item.event_id,
                )
                try:
                    defer_response(
                        item.event_id,
                        error_code="completion_event_irrecoverable",
                        retry_after_seconds=24 * 60 * 60,
                    )
                except Exception:
                    LOGGER.warning("irrecoverable response deferral persist failed", exc_info=True)
                break
            try:
                defer_response(
                    item.event_id,
                    error_code="completion_event_compacted",
                    retry_after_seconds=1,
                    replacement_event=compact_event,
                )
            except Exception:
                LOGGER.warning("producer response poison deferral persist failed", exc_info=True)
                break
            # Keep queued→running→terminal ordered for this request while
            # allowing unrelated producers to make progress. The failed row
            # remains durable and is retried on the next tick.
            if correlation_id:
                blocked_correlations.add(correlation_id)
        except Exception:
            LOGGER.warning(
                "producer response outbox flush paused event_id=%s",
                item.event_id,
            )
            stats["errors"] += 1
            try:
                defer_response(
                    item.event_id,
                    error_code="completion_publish_failed",
                    retry_after_seconds=30,
                )
            except Exception:
                LOGGER.warning("producer response retry deferral persist failed", exc_info=True)
            # A broker/auth/network failure is entity-wide. Stop this bounded
            # pass instead of multiplying one outage into N failed sends.
            break
    note_outbox_flush(stats)
    return stats


def _drain_handler(msg: ParsedMessage, cfg: ServiceBusConfig) -> MessageAction:
    body = dict(msg.body or {})
    if _is_v1_jobs_message(body):
        # Multi-token / tabular outfmt path: forward the producer's
        # ``blast_options`` to the sibling ``/v1/jobs`` (free-form options)
        # instead of the XML-locked ``/api/v1/elastic-blast/submit``.
        payload = _build_v1_jobs_payload(msg, cfg)
        submit = external_blast.submit_job_v1
    else:
        payload = _build_request_payload(msg, cfg)
        submit = external_blast.submit_job
    if payload is None:
        # Cannot ever succeed → dead-letter (do not loop forever). Fail the
        # send-time placeholder (if any) so it does not linger as ``queued``
        # forever even though the message is in the DLQ. The correlation id is
        # recovered from the raw body the same way the placeholder used it.
        response_durable = _publish_drain_failure_event(
            cfg,
            correlation_id=_correlation_id_from_message(msg),
            request_id=_extract_request_id(msg),
            error_code="servicebus_malformed_request",
            error_message="request message could not be parsed into a valid BLAST submit",
        )
        if not response_durable:
            return _transient_action(msg)
        _fail_placeholder_for_message(msg, error_code="servicebus_malformed_request")
        _publish_jobs_cache_invalidate("servicebus_drain_malformed")
        _record_drain_request_event(
            "rejected",
            msg,
            cfg,
            action=MessageAction.DEAD_LETTER,
            error_code="servicebus_malformed_request",
        )
        return _dead_letter_action(
            msg,
            reason="servicebus_malformed_request",
            description="request message failed the ElasticBLAST submit contract",
        )

    correlation_id = str(payload["external_correlation_id"])
    received_ts = _now_iso()
    request_id = _extract_request_id(msg)
    request_fingerprint = _request_fingerprint(payload)

    # Date-tiered layout: stamp the YYYY/MM/DD/ prefix ONCE here (not inside the
    # submit_job choke point) so the SAME value reaches both the sibling (which
    # writes results under it) AND the durable jobstate row below (which the
    # dashboard's analytics resolve through resolve_results_prefix). Computing
    # it once avoids a recompute drifting across a midnight boundary between the
    # submit and the row write. submit_job honours a caller-set prefix, so this
    # wins and its own injection is a no-op for this path.
    try:
        from api.services.storage.job_prefix import date_layout_enabled, dated_results_subdir

        if date_layout_enabled() and "results_prefix" not in payload:
            payload["results_prefix"] = dated_results_subdir()
    except Exception:
        LOGGER.debug("drain results_prefix stamp skipped corr=%s", correlation_id, exc_info=True)

    # Idempotency: at-least-once delivery means we may see this twice. With the
    # atomic-claim gate OFF (legacy) ANY existing bridge row dedups. With it ON
    # only a CONFIRMED row (one carrying an openapi_job_id) dedups here; a bare
    # in-flight reservation is handled by claim_bridge below, so two concurrent
    # drains of the same correlation id can never both submit.
    existing = get_bridge(correlation_id)
    if existing is not None and (existing.openapi_job_id or not _ATOMIC_CLAIM):
        if existing.request_fingerprint and existing.request_fingerprint != request_fingerprint:
            LOGGER.warning("service bus correlation conflict corr=%s", correlation_id)
            response_durable = _publish_correlation_conflict(
                cfg,
                existing,
                request_id=request_id,
                request_fingerprint=request_fingerprint,
            )
            if not response_durable:
                return _transient_action(msg)
            _record_drain_request_event(
                "correlation_conflict",
                msg,
                cfg,
                payload=payload,
                action=MessageAction.DEAD_LETTER,
                error_code="servicebus_correlation_conflict",
                ack_published=True,
            )
            return _dead_letter_action(
                msg,
                reason="servicebus_correlation_conflict",
                description="external_correlation_id is bound to a different request",
            )
        if not existing.request_fingerprint:
            LOGGER.warning(
                "service bus duplicate request corr=%s has legacy bridge without fingerprint",
                correlation_id,
            )
        if not _publish_duplicate_ack(cfg, existing, request_id=request_id):
            return _transient_action(msg)
        LOGGER.info("service bus duplicate request ACK replayed corr=%s", correlation_id)
        _record_drain_request_event(
            "retry_ack_replayed",
            msg,
            cfg,
            payload=payload,
            openapi_job_id=existing.openapi_job_id,
            action=MessageAction.COMPLETE,
            ack_published=True,
        )
        return MessageAction.COMPLETE

    # Atomic single-writer reservation (gate-on). The winner submits; a contended
    # fresh reservation means another worker is mid-submit, so defer (expiry-
    # preserving RETRY) and let that worker's single submit stand — this is what
    # makes the parallel / multi-worker drain safe against duplicate BLAST runs.
    # A stale reservation (a worker that crashed between claim and submit) is
    # stolen inside claim_bridge, so a contended claim never wedges the
    # correlation id forever.
    if _ATOMIC_CLAIM:
        claim_error = ""
        try:
            claim_won = claim_bridge(correlation_id, request_id, request_fingerprint)
        except SoftTimeLimitExceeded:
            # The drain pass ran out of its soft budget. Never convert that into
            # a "defer" — the handler must stop immediately so the pass can wind
            # down instead of doing more work past its limit.
            raise
        except Exception:
            # The reservation store IS the single-writer guard: while it is
            # unreachable we cannot tell whether another worker owns this id, so
            # deferring is the only safe move. Letting the exception escape would
            # reach ``_safe_drain_handler`` and ABANDON, burning one of the ~10
            # broker deliveries per outage tick until a perfectly valid request
            # dead-letters. Deferring instead keeps the expiry-preserving retry
            # budget intact (see the RETRY contract in ``_transient_action``).
            LOGGER.warning(
                "service bus claim store unavailable corr=%s — deferring",
                correlation_id,
                exc_info=True,
            )
            claim_won = False
            claim_error = "claim_unavailable"
        if not claim_won:
            if not claim_error:
                LOGGER.info(
                    "service bus claim contended corr=%s — deferring to the in-flight submit",
                    correlation_id,
                )
            action = _transient_action(msg)
            _record_drain_request_event(
                "deferred",
                msg,
                cfg,
                payload=payload,
                action=action,
                error_code=claim_error or "claim_contended",
            )
            return action

    # Close the final receive/build/claim → submit race. A lifecycle barrier can
    # be created after the handler's entry check; never let that message leave
    # the broker for execution while start/scale/stop/DB warmup admission has
    # just closed. Roll back only our unconfirmed claim so redelivery retries
    # after the lifecycle converges.
    pre_submit_admission = _execution_admission_for_drain(cfg)
    if not pre_submit_admission.get("allowed"):
        if _ATOMIC_CLAIM:
            release_bridge(correlation_id)
        reason = str(pre_submit_admission.get("reason") or "not_ready")
        action = _transient_action(msg)
        _record_drain_request_event(
            "deferred",
            msg,
            cfg,
            payload=payload,
            action=action,
            error_code=reason,
        )
        LOGGER.info(
            "servicebus message deferred at pre-submit admission reason=%s corr=%s",
            reason,
            correlation_id,
        )
        return action

    try:
        upstream = submit(payload, **_openapi_kwargs(cfg))
    except HTTPException as exc:
        # Distinguish a permanent rejection from a transient one. A 4xx (e.g.
        # the sibling 400s a bad option / unsupported field, or a 422 validation
        # error) will NEVER succeed on retry, so dead-letter it immediately
        # instead of abandoning — abandoning burns the whole delivery count
        # (~10 retries) re-POSTing a request the sibling already rejected, which
        # delays the rest of the queue and floods the logs. A 5xx (sibling
        # overloaded / mid-restart) or a 503 transport error IS transient, so
        # abandon it for redelivery. 408/429 are retryable 4xx exceptions.
        status = int(getattr(exc, "status_code", 0) or 0)
        permanent = 400 <= status < 500 and status not in (408, 429)
        retry_exhausted = not permanent and _retry_exhausted(msg)
        failure_code = (
            f"servicebus_submit_rejected_{status}"
            if permanent
            else f"openapi_http_{status or 'unknown'}"
        )
        LOGGER.warning(
            "service bus → OpenAPI submit %s corr=%s status=%s",
            "rejected (dead-letter)" if permanent else "failed (retry)",
            correlation_id,
            status,
        )
        if permanent:
            # Terminal rejection: turn the send-time placeholder into a failed
            # row instead of leaving it ``queued`` forever (the message is now
            # dead-lettered). A transient failure keeps the placeholder queued.
            response_durable = _publish_drain_failure_event(
                cfg,
                correlation_id=correlation_id,
                request_id=request_id,
                error_code=f"servicebus_submit_rejected_{status}",
                error_message=_detail_text(exc),
            )
            if response_durable:
                _fail_placeholder(
                    correlation_id,
                    error_code=f"servicebus_submit_rejected_{status}",
                )
                _publish_jobs_cache_invalidate("servicebus_drain_rejected")
            else:
                permanent = False
        elif retry_exhausted:
            terminal_code, terminal_message = _retry_terminal_error(msg)
            response_durable = _publish_drain_failure_event(
                cfg,
                correlation_id=correlation_id,
                request_id=request_id,
                error_code=terminal_code,
                error_message=terminal_message,
            )
            if response_durable:
                permanent = True
                failure_code = terminal_code
                _fail_placeholder(
                    correlation_id,
                    error_code=terminal_code,
                )
                _publish_jobs_cache_invalidate(terminal_code)
        if _ATOMIC_CLAIM:
            # Submit failed after we reserved the correlation id, so roll the
            # reservation back: a transient ABANDON can then re-claim + resubmit
            # on redelivery, and a permanent DEAD_LETTER leaves no phantom
            # ``claimed`` row behind.
            release_bridge(correlation_id)
        action = (
            MessageAction.DEAD_LETTER
            if permanent
            else (MessageAction.ABANDON if retry_exhausted else MessageAction.RETRY)
        )
        _record_drain_request_event(
            "rejected" if permanent else "retry_scheduled",
            msg,
            cfg,
            payload=payload,
            action=action,
            error_code=failure_code,
        )
        if action == MessageAction.DEAD_LETTER:
            return _dead_letter_action(
                msg,
                reason=failure_code,
                description=_detail_text(exc) or "OpenAPI submit rejected the request",
            )
        return action
    except Exception as exc:
        # Unknown/unexpected error is transient until the bounded scheduled
        # retry envelope expires. Admission-blocked messages never enter this
        # branch, so multi-hour DB update/warmup does not consume attempts.
        LOGGER.exception("service bus → OpenAPI submit failed corr=%s", correlation_id)
        if _ATOMIC_CLAIM:
            release_bridge(correlation_id)
        retry_exhausted = _retry_exhausted(msg)
        action = MessageAction.ABANDON if retry_exhausted else MessageAction.RETRY
        if retry_exhausted:
            terminal_code, terminal_message = _retry_terminal_error(msg)
            response_durable = _publish_drain_failure_event(
                cfg,
                correlation_id=correlation_id,
                request_id=request_id,
                error_code=terminal_code,
                error_message=terminal_message,
            )
            if response_durable:
                action = MessageAction.DEAD_LETTER
                _fail_placeholder(
                    correlation_id,
                    error_code=terminal_code,
                )
                _publish_jobs_cache_invalidate(terminal_code)
        _record_drain_request_event(
            "rejected" if action == MessageAction.DEAD_LETTER else "retry_scheduled",
            msg,
            cfg,
            payload=payload,
            action=action,
            error_code=(
                _retry_terminal_error(msg)[0]
                if action == MessageAction.DEAD_LETTER
                else type(exc).__name__
            ),
        )
        if action == MessageAction.DEAD_LETTER:
            return _dead_letter_action(
                msg,
                reason=_retry_terminal_error(msg)[0],
                description=_retry_terminal_error(msg)[1],
            )
        return action

    openapi_job_id = str(upstream.get("job_id") or "")
    upsert_bridge(
        BridgeRecord(
            correlation_id=correlation_id,
            openapi_job_id=openapi_job_id,
            last_status="",
            done=False,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
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
    # Supersede the send-time ``queued`` placeholder (keyed by correlation id):
    # the real OpenAPI-keyed row now carries the job, so soft-delete the
    # placeholder to avoid a duplicate row in the list. Best-effort — a stale
    # placeholder is reconciled later and never blocks the drain.
    _supersede_placeholder(correlation_id)
    # The durable row was just created in THIS (worker) process; drop the api
    # sidecar's jobs / message-flow caches cross-process so the job surfaces on
    # the next poll instead of waiting out the cache TTL.
    _publish_jobs_cache_invalidate("servicebus_drain_submitted")
    # Stage the initial queued response durably before immediate publish. If the
    # outbox write fails the confirmed bridge remains active with an empty
    # marker, so publish_transitions retries without re-submitting the job.
    queued_ack_published = False
    try:
        queued_durable, queued_ack_published = _stage_response_event(
            cfg,
            _transition_event(
                correlation_id=correlation_id,
                openapi_job_id=openapi_job_id,
                status=_STATUS_QUEUED,
                attempt=1,
                request_id=request_id,
                event_id_scope=request_id,
            ),
        )
        if queued_durable:
            mark_published(correlation_id, _STATUS_QUEUED)
    except Exception:
        LOGGER.warning("queued response staging failed corr=%s (bridge will retry)", correlation_id)
    _record_drain_request_event(
        "accepted",
        msg,
        cfg,
        payload=payload,
        openapi_job_id=openapi_job_id,
        action=MessageAction.COMPLETE,
        ack_published=queued_ack_published,
    )
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
        from api.services.blast.external_config import build_external_config_snapshot
        from api.services.blast.external_jobs import _sync_external_jobs_to_table
        from api.services.blast.servicebus_placeholder import placeholder_exists

        # control_plane = the dashboard send route wrote a send-time placeholder
        # for this correlation id (an external producer that enqueues straight to
        # the namespace cannot). external = no placeholder => the request entered
        # the queue from outside the control plane. Surfaced on the job row so the
        # UI can label "queue (dashboard)" vs "queue". The check runs BEFORE
        # ``_supersede_placeholder`` (called by the drain handler after this), so
        # the placeholder is still present here.
        queue_origin = "control_plane" if placeholder_exists(correlation_id) else "external"

        # Capture the submitted BLAST options as a config_snapshot so the job
        # detail can show outfmt / evalue / word_size / etc. The sibling /v1/jobs
        # record never echoes these back, so this is the ONLY durable source. The
        # XML path carries them under ``options``; the v1 path under
        # ``blast_options``.
        _submitted_options = payload.get("blast_options") or payload.get("options") or {}
        config_snapshot = build_external_config_snapshot(
            _submitted_options if isinstance(_submitted_options, dict) else {}
        )

        # Resolve the cluster context (sub/rg/cluster) so region + cluster-scoped
        # analytics populate immediately on the durable row instead of waiting
        # for the scope-backfill poll. Blanks fall through to that backfill.
        _sub, _rg, _cluster = _resolve_drain_cluster_context(cfg)

        ext_row = {
            "job_id": openapi_job_id,
            "status": _STATUS_QUEUED,
            "program": payload.get("program"),
            "db": payload.get("db"),
            "created_at": received_ts,
            "submission_source": "servicebus",
            "queue_origin": queue_origin,
            "external_correlation_id": correlation_id,
            "cluster_name": _cluster,
        }
        if _sub:
            ext_row["subscription_id"] = _sub
        if _rg:
            ext_row["resource_group"] = _rg
        if config_snapshot:
            ext_row["config_snapshot"] = config_snapshot
        # Capture the query identity (length + molecule) from the submitted FASTA
        # so the detail header shows them instead of "—" without an extra blob
        # read. Best-effort and durable on the row.
        try:
            from api.services.blast.external_query_meta import query_meta_from_fasta

            _query_meta = query_meta_from_fasta(payload.get("query_fasta"))
            if _query_meta:
                ext_row["query_meta"] = _query_meta
        except Exception:
            LOGGER.debug("drain query meta capture skipped corr=%s", correlation_id, exc_info=True)
        # Resolve + stamp the region durably in the worker so the jobs-list
        # projection reads it from the row instead of making an ARM call on the
        # request hot path. Best-effort (cached).
        try:
            from api.services.blast.external_config import resolve_cluster_region

            _region = resolve_cluster_region(_sub, _rg, _cluster)
            if _region:
                ext_row["region"] = _region
        except Exception:
            LOGGER.debug("drain region stamp skipped corr=%s", correlation_id, exc_info=True)
        # Date-tiered layout: the sibling wrote results under
        # results/<prefix><openapi_job_id>/, so record that exact prefix on the
        # durable row. Without it resolve_results_prefix(openapi_job_id) falls
        # back to the flat <openapi_job_id>/ and the dashboard's analytics
        # (list_parseable_result_blobs) find zero blobs for a date-tiered job.
        _date_prefix = str(payload.get("results_prefix") or "").strip()
        if _date_prefix:
            ext_row["results_prefix"] = f"{_date_prefix}{openapi_job_id}/"
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


def _openapi_ready_for_drain(cfg: ServiceBusConfig) -> bool:
    """True when the sibling OpenAPI plane is reachable + ready for a submit.

    Used ONLY when queue-arrival auto-start is enabled (charter §12a Rule 4
    default-OFF gate ``SERVICEBUS_QUEUE_AUTOSTART``). While an auto-started
    Stopped cluster warms up, its OpenAPI plane is unreachable, and draining
    then would ABANDON-loop every received message (delivery-count burn →
    premature dead-letter) before the cluster is ready. A False result defers
    the whole drain tick so the messages stay on the queue — which also keeps
    the auto-start trigger (pending depth) alive — until the plane is up.
    ``external_blast.ready`` is already short-TTL cached (≈5s) so this adds at
    most one cheap probe per tick. Never raises: any probe failure (including
    the expected ``openapi_unreachable`` while the cluster is down) returns
    False so an unreadable plane defers rather than crashes the tick.
    """
    try:
        payload = external_blast.ready(**_openapi_kwargs(cfg))
        return bool(payload.get("ready"))
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        return False


def _openapi_ready_for_transition_poll(openapi_kwargs: dict[str, str]) -> bool:
    """Return whether transition polling can reach a ready OpenAPI plane.

    A stopped AKS cluster cannot change sibling job status, so polling every
    active bridge only multiplies transport timeouts. The readiness endpoint
    has a five-second bound and short cache; deferring preserves every active
    bridge for the next tick after AKS starts. Never raises because this is a
    periodic availability gate, not a terminal job-state decision.
    """
    try:
        payload = external_blast.ready(**openapi_kwargs)
        return bool(payload.get("ready"))
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        return False


@shared_task(
    name="api.tasks.servicebus.drain_and_resubmit",
    soft_time_limit=45,
    time_limit=60,
)
@skip_tick_on_transient_infra
def drain_and_resubmit() -> dict[str, Any]:
    """Drain the request queue → bridge each message to the OpenAPI plane."""
    if not service_bus_enabled():
        return {"skipped": "disabled"}
    cfg = get_service_bus_config()
    return _drain_once(
        cfg,
        max_messages=_DRAIN_MAX_MESSAGES,
        max_wait_seconds=1,
        max_concurrency=_DRAIN_CONCURRENCY,
    )


def _drain_once(
    cfg: ServiceBusConfig,
    *,
    max_messages: int,
    max_wait_seconds: int,
    max_concurrency: int,
) -> dict[str, Any]:
    """Run one admission-gated, queue-scoped, bounded drain pass."""
    admission = _execution_admission_for_drain(cfg)
    if not admission.get("allowed"):
        reason = str(admission.get("reason") or "cluster_not_ready")
        if reason in {
            "aks_stop_in_progress",
            "cluster_stopped",
            "cluster_starting",
        }:
            try:
                from api.services.aks.queue_autostart import (
                    request_autostart_for_pending_queue,
                )

                request_autostart_for_pending_queue(
                    reason=f"servicebus_drain:{reason}"
                )
            except Exception:
                LOGGER.debug("queue-demand auto-start trigger failed", exc_info=True)
        LOGGER.info(
            "servicebus drain deferred queue=%s reason=%s action=%s target_nodes=%s",
            cfg.request_queue,
            reason,
            admission.get("lifecycle_action", ""),
            admission.get("target_node_count", ""),
        )
        return {
            "skipped": reason,
            "retry_after_seconds": int(admission.get("retry_after_seconds") or 10),
        }
    _reconcile_recovered_start_failure(admission)
    proceed, lock_token = _acquire_drain_lock(cfg.request_queue)
    if not proceed:
        # Another drain holds the single-flight lease — skip this overlapping
        # tick instead of racing it on the same queue. The held drain covers the
        # backlog; the next tick re-evaluates once the lease frees.
        LOGGER.debug(
            "servicebus drain tick skipped: single-flight lease held queue=%s",
            cfg.request_queue,
        )
        return {"skipped": "locked"}
    try:
        stats = service_bus.drain_requests(
            cfg,
            lambda m: _drain_handler(m, cfg),
            max_messages=max_messages,
            max_wait_seconds=max_wait_seconds,
            max_concurrency=max_concurrency,
        )
        # Observability (self-critique #6): one structured line per non-empty tick
        # so drain throughput / fan-out effectiveness is visible in App Insights
        # without parsing per-message logs. Silent on an idle tick (received==0)
        # to avoid flooding the log when the queue is empty.
        if stats.received:
            LOGGER.info(
                "servicebus drain tick received=%d completed=%d abandoned=%d "
                    "retried=%d dead_lettered=%d concurrency=%d",
                stats.received,
                stats.completed,
                stats.abandoned,
                    getattr(stats, "retried", 0),
                stats.dead_lettered,
                max_concurrency,
            )
        return {
            "received": stats.received,
            "completed": stats.completed,
            "abandoned": stats.abandoned,
            **(
                {"retried": int(getattr(stats, "retried", 0))}
                if getattr(stats, "retried", 0)
                else {}
            ),
            "dead_lettered": stats.dead_lettered,
            "concurrency": max_concurrency,
            **(
                {"budget_exhausted": True}
                if getattr(stats, "budget_exhausted", False)
                else {}
            ),
        }
    finally:
        _release_drain_lock(lock_token, cfg.request_queue)


def _publish_one_bridge(
    cfg: ServiceBusConfig,
    rec: BridgeRecord,
    openapi_kwargs: dict[str, str],
    *,
    pending_correlations: set[str] | None = None,
) -> tuple[int, int]:
    """Process one active bridge: poll sibling status, publish on change.

    Returns ``(published_delta, finished_delta)``. The expected transient cases
    (status poll failure, publish failure) are handled inline and return
    ``(0, 0)`` so the bridge is retried on the next tick. Anything unexpected
    (a tracking-store write raising) propagates to the caller, which isolates it
    so one bad bridge never aborts the whole tick.
    """
    if not rec.openapi_job_id:
        # Never bridged to a job id (drain crashed mid-flight). Give up once it
        # ages past the deadline, but first stage a terminal producer response.
        if _bridge_expired(rec.created_at):
            event = _transition_event(
                correlation_id=rec.correlation_id,
                openapi_job_id="",
                status=_STATUS_FAILED,
                attempt=1,
                error_code="bridge_unconfirmed_timeout",
                error_message="request could not be confirmed before the bridge deadline",
                request_id=rec.request_id,
            )
            durable, delivered = _stage_response_event(cfg, event)
            if not durable:
                return (0, 0)
            mark_done(rec.correlation_id, _STATUS_FAILED)
            return (int(delivered), 1)
        return (0, 0)
    if not rec.last_status:
        # Submit succeeded but the immediate queued outbox write failed. Recover
        # the producer acknowledgement before observing later running/terminal
        # states so every accepted request has a durable acceptance response.
        queued_event = _transition_event(
            correlation_id=rec.correlation_id,
            openapi_job_id=rec.openapi_job_id,
            status=_STATUS_QUEUED,
            attempt=1,
            request_id=rec.request_id,
            event_id_scope=rec.request_id,
        )
        durable, delivered = _stage_response_event(cfg, queued_event)
        if not durable:
            return (0, 0)
        mark_published(rec.correlation_id, _STATUS_QUEUED)
        return (int(delivered), 0)
    try:
        response_pending = (
            rec.correlation_id in pending_correlations
            if pending_correlations is not None
            else has_pending_response(rec.correlation_id)
        )
        if response_pending:
            # Preserve queued -> running -> terminal order even when the topic
            # is temporarily unavailable. The outbox flush publishes the
            # older response first; this bridge is polled on the next tick.
            return (0, 0)
    except Exception:
        LOGGER.warning(
            "producer response ordering check failed corr=%s",
            rec.correlation_id,
            exc_info=True,
        )
        return (0, 0)
    if _finish_lifecycle_interrupted_bridge(cfg, rec):
        return (1, 1)
    try:
        job = external_blast.get_job(rec.openapi_job_id, **openapi_kwargs)
    except Exception as exc:  # transient unless a recovered plane confirms 404
        if int(getattr(exc, "status_code", 0) or 0) == 404 and (
            _finish_lifecycle_interrupted_bridge(cfg, rec, confirmed_missing=True)
        ):
            return (1, 1)
        LOGGER.debug("status poll failed corr=%s", rec.correlation_id, exc_info=True)
        return (0, 0)
    status = _classify(str(job.get("status") or ""))
    if status == rec.last_status:
        # No transition since last publish. If the job has been non-terminal for
        # too long, give up and emit a timeout failure so the active set stays
        # bounded and the subscriber is not left hanging.
        if status not in _TERMINAL and _bridge_expired(rec.created_at):
            timeout_event = _transition_event(
                correlation_id=rec.correlation_id,
                openapi_job_id=rec.openapi_job_id,
                status=_STATUS_FAILED,
                attempt=1,
                error_code="bridge_timeout",
                error_message="job did not reach a terminal state before the bridge deadline",
                request_id=rec.request_id,
            )
            durable, delivered = _stage_response_event(cfg, timeout_event)
            if not durable:
                return (0, 0)
            _record_transition_trace(rec.openapi_job_id, _STATUS_FAILED)
            mark_done(rec.correlation_id, _STATUS_FAILED)
            return (int(delivered), 1)
        return (0, 0)
    # Reaching here means status != rec.last_status (the equal case returned
    # above), so this is always the first publish of THIS status for THIS bridge
    # — attempt is 1. A re-publish after a failed ``mark_published`` write also
    # lands here with status != last_status (the marker never advanced), so
    # attempt alone cannot distinguish an original from a retry; subscribers
    # dedupe on the stable ``event_id`` (sha256 of corr:status) instead. The
    # field is kept at 1 for schema stability — see ``_transition_event``.
    attempt = 1
    error_code: str | None = None
    error_message: str | None = None
    if status == _STATUS_FAILED:
        err = job.get("error") if isinstance(job.get("error"), dict) else {}
        error_code = str((err or {}).get("code") or "failed")
        error_message = _error_message_for_event(job) or None
        # A coarse/generic sibling error (``one or more BLAST jobs failed``, a
        # bare K8s CrashLoopBackOff, or none) names THAT the job failed, not
        # WHY. Recover the authoritative cluster-side blastn detail
        # (metadata/FAILURE.txt + BLAST_RUNTIME exit code) — the same enrichment
        # the dashboard detail view applies — so a completion-topic subscriber
        # sees an actionable cause. Best-effort + sanitised (charter §12).
        enriched = _enrich_failure_message_for_event(
            job, rec.openapi_job_id, error_message
        )
        if enriched:
            error_message = enriched
    # On a succeeded transition, attach the result-file download links so a
    # topic subscriber can pull the results directly (via the dashboard's
    # authenticated streaming gateway — never a SAS URL). Best-effort: if the
    # sibling has not listed files yet the list is empty and the subscriber
    # falls back to ``result_ref``.
    result_files: list[dict[str, Any]] | None = None
    if status == _STATUS_SUCCEEDED:
        try:
            result_files = _result_files_for_event(job, rec.openapi_job_id)
        except Exception:
            LOGGER.debug(
                "result-file link build failed corr=%s", rec.correlation_id, exc_info=True
            )
            result_files = []
        # Durably capture the file_id -> blob_path manifest (best-effort) so the
        # download route can serve results from Storage after the cluster
        # auto-stops. Independent of the event-link build above.
        _persist_result_manifest(rec.openapi_job_id, job)
    event = _transition_event(
        correlation_id=rec.correlation_id,
        openapi_job_id=rec.openapi_job_id,
        status=status,
        attempt=attempt,
        error_code=error_code,
        error_message=error_message,
        request_id=rec.request_id,
        result_files=result_files,
    )
    durable, delivered = _stage_response_event(cfg, event)
    if not durable:
        return (0, 0)
    _record_transition_trace(rec.openapi_job_id, status)
    if status in _TERMINAL:
        mark_done(rec.correlation_id, status)
        return (int(delivered), 1)
    mark_published(rec.correlation_id, status)
    return (int(delivered), 0)


def _finish_lifecycle_interrupted_bridge(
    cfg: ServiceBusConfig,
    rec: BridgeRecord,
    *,
    confirmed_missing: bool = False,
) -> bool:
    """Publish one bounded failure when a newer AKS lifecycle lost the job."""
    if not rec.openapi_job_id:
        return False
    subscription_id, resource_group, cluster_name = _resolve_drain_cluster_context(cfg)
    try:
        from api.services.aks.execution_admission import (
            lifecycle_barrier_interrupts_job,
        )

        barrier = lifecycle_barrier_interrupts_job(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            job_created_at=rec.created_at,
        )
        if barrier is None:
            return False
        # A start/scale barrier can keep OpenAPI unavailable throughout node
        # convergence and DB warmup. Offline is not proof that an accepted job
        # was lost; the recovered per-job poll must first return 404 (handled
        # by ``_publish_one_bridge``). Stop/delete intentionally remove the
        # execution plane, so their sustained outage is positive evidence.
        if barrier.action in {"start", "scale"} and not confirmed_missing:
            try:
                from api.services.state_repo import get_state_repo

                row = get_state_repo().get(rec.openapi_job_id)
                payload = getattr(row, "payload", None)
                payload = payload if isinstance(payload, dict) else {}
                error_code = str(
                    getattr(row, "error_code", "") or payload.get("error_code") or ""
                )
                local_interrupted = (
                    str(getattr(row, "status", "") or "").lower() == "failed"
                    and error_code == "cluster_lifecycle_interrupted"
                )
            except Exception:
                local_interrupted = False
            if not local_interrupted:
                return False
        barrier_at = datetime.fromisoformat(barrier.created_at.replace("Z", "+00:00"))
        if (datetime.now(UTC) - barrier_at).total_seconds() < _LIFECYCLE_INTERRUPTION_SECONDS:
            return False
        event = _transition_event(
            correlation_id=rec.correlation_id,
            openapi_job_id=rec.openapi_job_id,
            status=_STATUS_FAILED,
            attempt=1,
            error_code="cluster_lifecycle_interrupted",
            error_message=(
                f"AKS {barrier.action} interrupted the execution plane. Retry after "
                "the cluster and database warmup are ready."
            ),
            request_id=rec.request_id,
        )
        durable, _delivered = _stage_response_event(cfg, event)
        if not durable:
            return False
        _record_transition_trace(rec.openapi_job_id, _STATUS_FAILED)
        mark_done(rec.correlation_id, _STATUS_FAILED)
        return True
    except Exception:
        LOGGER.warning(
            "lifecycle interruption publish failed corr=%s",
            rec.correlation_id,
            exc_info=True,
        )
        return False


def _stage_dead_letter_response_and_backup(
    cfg: ServiceBusConfig,
    msg: ParsedMessage,
) -> bool:
    """Durably stage terminal response + audit evidence for one DLQ request."""
    correlation_id = _correlation_id_from_message(msg)
    request_id = _extract_request_id(msg)
    existing = get_bridge(correlation_id) if correlation_id else None
    if existing is not None and existing.openapi_job_id:
        # This DLQ entry is an at-least-once duplicate of a request that already
        # has an accepted execution. Re-emit accepted, never a false terminal
        # failure that would contradict the live bridge/job.
        if not _publish_duplicate_ack(cfg, existing, request_id=request_id):
            return False
        return bool(backup_dead_letter_message(
            {
                "ts": _now_iso(),
                "correlation_id": correlation_id,
                "request_id": request_id,
                "message_id": msg.message_id,
                "sequence_number": msg.sequence_number,
                "dead_letter_reason": msg.dead_letter_reason,
                "delivery_count": msg.delivery_count,
                "duplicate_of_openapi_job_id": existing.openapi_job_id,
                "body": msg.body,
            }
        ))
    raw_reason = str(msg.dead_letter_reason or "servicebus_dead_lettered")
    reason_key = raw_reason.strip().lower().replace(" ", "_")
    if "ttl" in reason_key or "expired" in reason_key:
        error_code = "servicebus_request_expired"
    elif "maxdelivery" in reason_key or "max_delivery" in reason_key:
        error_code = "servicebus_max_delivery_exceeded"
    else:
        error_code = "servicebus_dead_lettered"
    event = _transition_event(
        correlation_id=correlation_id,
        openapi_job_id="",
        status=_STATUS_FAILED,
        attempt=max(1, msg.retry_attempt + 1),
        error_code=error_code,
        error_message=f"request moved to the dead-letter queue: {raw_reason[:160]}",
        request_id=request_id,
    )
    durable, _delivered = _stage_response_event(
        cfg,
        event,
        deliver_immediately=False,
    )
    if not durable:
        return False
    backed_up = backup_dead_letter_message(
        {
            "ts": _now_iso(),
            "correlation_id": correlation_id,
            "request_id": request_id,
            "message_id": msg.message_id,
            "sequence_number": msg.sequence_number,
            "enqueued_time_utc": (
                msg.enqueued_time_utc.isoformat() if msg.enqueued_time_utc else None
            ),
            "dead_letter_reason": msg.dead_letter_reason,
            "dead_letter_error_description": msg.dead_letter_error_description,
            "delivery_count": msg.delivery_count,
            "body": msg.body,
        }
    )
    if not backed_up:
        return False
    _fail_placeholder(correlation_id, error_code=error_code)
    _publish_jobs_cache_invalidate("servicebus_dlq_response")
    return True


def stage_operator_purge_response_and_backup(
    cfg: ServiceBusConfig,
    msg: ParsedMessage,
) -> bool:
    """Durably fail one operator-purged request before removing it from queue."""
    correlation_id = _correlation_id_from_message(msg)
    request_id = _extract_request_id(msg)
    existing = get_bridge(correlation_id) if correlation_id else None
    if existing is not None and existing.openapi_job_id:
        if not _publish_duplicate_ack(cfg, existing, request_id=request_id):
            return False
        return bool(backup_dead_letter_message(
            {
                "ts": _now_iso(),
                "correlation_id": correlation_id,
                "request_id": request_id,
                "message_id": msg.message_id,
                "sequence_number": msg.sequence_number,
                "dead_letter_reason": "operator_purged_duplicate",
                "duplicate_of_openapi_job_id": existing.openapi_job_id,
                "body": msg.body,
            }
        ))
    event = _transition_event(
        correlation_id=correlation_id,
        openapi_job_id="",
        status=_STATUS_FAILED,
        attempt=max(1, msg.retry_attempt + 1),
        error_code="servicebus_operator_purged",
        error_message="request was cancelled by an operator before execution",
        request_id=request_id,
    )
    durable, _delivered = _stage_response_event(
        cfg,
        event,
        deliver_immediately=False,
    )
    if not durable:
        return False
    if not backup_dead_letter_message(
        {
            "ts": _now_iso(),
            "correlation_id": correlation_id,
            "request_id": request_id,
            "message_id": msg.message_id,
            "sequence_number": msg.sequence_number,
            "enqueued_time_utc": (
                msg.enqueued_time_utc.isoformat() if msg.enqueued_time_utc else None
            ),
            "dead_letter_reason": "operator_purged",
            "delivery_count": msg.delivery_count,
            "body": msg.body,
        }
    ):
        return False
    _fail_placeholder(correlation_id, error_code="servicebus_operator_purged")
    _publish_jobs_cache_invalidate("servicebus_operator_purged")
    return True


def _reconcile_dead_letter_responses(cfg: ServiceBusConfig) -> dict[str, int]:
    """Emit one terminal response and audit backup for every DLQ request."""

    def handle(msg: ParsedMessage) -> MessageAction:
        if not _stage_dead_letter_response_and_backup(cfg, msg):
            return MessageAction.ABANDON
        return MessageAction.COMPLETE

    stats = service_bus.drain_dead_letter_messages(
        cfg,
        handle,
        max_messages=_DLQ_RESPONSE_MAX_MESSAGES,
    )
    return {
        "received": stats.received,
        "completed": stats.completed,
        "abandoned": stats.abandoned,
    }


@shared_task(
    name="api.tasks.servicebus.reconcile_dead_letter_responses",
    soft_time_limit=90,
    time_limit=120,
)
@skip_tick_on_transient_infra
def reconcile_dead_letter_responses() -> dict[str, Any]:
    """Turn every terminal DLQ request into a durable producer response."""
    if not service_bus_enabled():
        return {"skipped": "disabled"}
    return _reconcile_dead_letter_responses(get_service_bus_config())


@shared_task(
    name="api.tasks.servicebus.emit_service_bus_health",
    soft_time_limit=45,
    time_limit=60,
)
@skip_tick_on_transient_infra
def emit_service_bus_health() -> dict[str, Any]:
    """Emit one payload-free queue/outbox/admission health snapshot."""
    if not service_bus_enabled():
        return {"skipped": "disabled"}
    cfg = get_service_bus_config()
    try:
        admission: dict[str, Any] | None = _execution_admission_for_drain(cfg)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        admission = None
        LOGGER.debug(
            "servicebus health admission probe unavailable: %s",
            type(exc).__name__,
        )
    snapshot = collect_service_bus_health(cfg, admission=admission)
    queue = cast(dict[str, Any], snapshot["queue"])
    outbox = cast(dict[str, Any], snapshot["outbox"])
    warnings = tuple(str(value) for value in snapshot["warnings"])
    record_service_bus_health_event(
        status=str(snapshot["status"]),
        warning_codes=",".join(warnings),
        queue_counts_available=bool(queue["counts_available"]),
        queue_counts_error=str(queue["counts_error"]),
        queue_active=cast(int | None, queue["active"]),
        queue_scheduled=cast(int | None, queue["scheduled"]),
        queue_dead_letter=cast(int | None, queue["dead_letter"]),
        queue_total=cast(int | None, queue["total"]),
        completion_configured=bool(snapshot["completion_configured"]),
        completion_kind=str(snapshot["completion_kind"]),
        completion_accessible=cast(bool | None, queue["completion_accessible"]),
        completion_error=str(queue["completion_error"]),
        completion_subscription_count=cast(int | None, queue["completion_subscription_count"]),
        completion_active=cast(int | None, queue["completion_active"]),
        completion_dead_letter=cast(int | None, queue["completion_dead_letter"]),
        outbox_available=bool(outbox["available"]),
        outbox_error=str(outbox["error"]),
        outbox_pending=cast(int | None, outbox["pending"]),
        outbox_pending_truncated=bool(outbox["pending_truncated"]),
        outbox_oldest_age_seconds=cast(int | None, outbox["oldest_age_seconds"]),
        outbox_last_attempt_at=str(outbox["last_attempt_at"]),
        outbox_last_success_at=str(outbox["last_success_at"]),
        outbox_last_error_at=str(outbox["last_error_at"]),
        outbox_last_scanned=int(outbox["last_scanned"]),
        outbox_last_delivered=int(outbox["last_delivered"]),
        outbox_last_errors=int(outbox["last_errors"]),
        outbox_deferred=cast(int | None, outbox.get("deferred")),
        outbox_poison=cast(int | None, outbox.get("poison")),
        admission_available=bool(snapshot["admission_available"]),
        admission_allowed=cast(bool | None, snapshot["admission_allowed"]),
        admission_reason=str(snapshot["admission_reason"]),
        request_policy_available=bool(queue.get("request_policy_available")),
        request_policy_error=str(queue.get("request_policy_error") or ""),
        request_ttl_seconds=cast(int | None, queue.get("request_ttl_seconds")),
        producer_request_ttl_seconds=int(snapshot.get("producer_request_ttl_seconds") or 0),
        request_dead_letter_on_expiration=cast(
            bool | None, queue.get("request_dead_letter_on_expiration")
        ),
        request_max_delivery_count=cast(int | None, queue.get("request_max_delivery_count")),
        completion_policy_available=bool(queue.get("completion_policy_available")),
        completion_min_ttl_seconds=cast(int | None, queue.get("completion_min_ttl_seconds")),
        completion_dead_letter_on_expiration=cast(
            bool | None, queue.get("completion_dead_letter_on_expiration")
        ),
        completion_max_delivery_count=cast(int | None, queue.get("completion_max_delivery_count")),
        admission_target_node_count=int(snapshot.get("admission_target_node_count") or 0),
        admission_ready_node_count=int(snapshot.get("admission_ready_node_count") or 0),
        admission_warmup_job_count=int(snapshot.get("admission_warmup_job_count") or 0),
        admission_failed_warmup_job_count=int(
            snapshot.get("admission_failed_warmup_job_count") or 0
        ),
        resident_consumer_enabled=bool(snapshot["resident_consumer_enabled"]),
        drain_concurrency=int(snapshot["drain_concurrency"]),
    )

    global _LAST_SERVICE_BUS_HEALTH_WARNING
    warning_key = ",".join(warnings)
    if warning_key != _LAST_SERVICE_BUS_HEALTH_WARNING:
        if warning_key:
            LOGGER.warning(
                "servicebus health warning codes=%s active=%s dlq=%s "
                "outbox_pending=%s completion_configured=%s admission_reason=%s",
                warning_key,
                queue["active"],
                queue["dead_letter"],
                outbox["pending"],
                snapshot["completion_configured"],
                snapshot["admission_reason"],
            )
        elif _LAST_SERVICE_BUS_HEALTH_WARNING:
            LOGGER.info("servicebus health recovered")
        _LAST_SERVICE_BUS_HEALTH_WARNING = warning_key
    return snapshot


@shared_task(
    name="api.tasks.servicebus.publish_transitions",
    soft_time_limit=50,
    time_limit=60,
)
@skip_tick_on_transient_infra
def publish_transitions() -> dict[str, Any]:
    """Poll sibling status for active bridges and emit one event per change."""
    if not service_bus_enabled():
        return {"skipped": "disabled"}
    cfg = get_service_bus_config()
    outbox = _flush_response_outbox(cfg)
    try:
        pending_correlations, pending_snapshot_complete = pending_response_correlations()
    except Exception:
        LOGGER.warning("producer response ordering snapshot failed", exc_info=True)
        return {
            "skipped": "outbox_ordering_unavailable",
            "published": outbox["delivered"],
            "errors": outbox["errors"] + 1,
        }
    if not pending_snapshot_complete:
        LOGGER.warning(
            "producer response ordering snapshot truncated; bridge polling deferred"
        )
        return {
            "skipped": "outbox_ordering_truncated",
            "published": outbox["delivered"],
            "errors": outbox["errors"] + 1,
        }
    # Fetch the work set BEFORE resolving the OpenAPI client kwargs. With zero
    # active bridges there is nothing to poll, and `_openapi_kwargs` reads the
    # configured cluster's `elb-openapi` Service IP from the Kubernetes API on
    # every tick (30 s). When that cluster is stopped or was recreated with a
    # new API-server FQDN, the read raises a `requests` ConnectionError that the
    # OpenTelemetry instrumentation auto-records as an App Insights
    # dependency-failure exception — flooding the telemetry with thousands of
    # identical traces for a no-op tick. Resolving lazily makes the idle path
    # touch nothing but the local tracking store.
    bridges, next_cursor = list_active_bridges_page(
        limit=_PUBLISH_MAX_ROWS,
        after_row_key=_transition_cursor(),
    )
    if bridges:
        _save_transition_cursor(next_cursor)
    if not bridges:
        return {
            "scanned": 0,
            "published": outbox["delivered"],
            "finished": 0,
            "errors": outbox["errors"],
        }
    openapi_kwargs = _openapi_kwargs(cfg)
    if not _openapi_ready_for_transition_poll(openapi_kwargs):
        finished = sum(
            1 for rec in bridges if _finish_lifecycle_interrupted_bridge(cfg, rec)
        )
        LOGGER.info(
            "servicebus publish tick deferred: OpenAPI plane not ready active_bridges=%d "
            "lifecycle_finished=%d",
            len(bridges),
            finished,
        )
        return {
            "skipped": "cluster_not_ready",
            "active_bridges": len(bridges),
            "finished": finished,
        }
    published = outbox["delivered"]
    finished = 0
    scanned = 0
    errors = outbox["errors"]
    for rec in bridges:
        scanned += 1
        try:
            p_delta, f_delta = _publish_one_bridge(
                cfg,
                rec,
                openapi_kwargs,
                pending_correlations=pending_correlations,
            )
        except Exception:
            # Partial-failure isolation: a tracking write (mark_published /
            # mark_done) or any unexpected error on ONE bridge must not abort
            # the whole tick and starve the remaining bridges — this mirrors the
            # per-item isolation in ``drain_requests`` and
            # ``reconcile_stale_jobs``. Any event already published is deduped by
            # ``event_id`` on the subscriber; the bridge marker advances on the
            # next tick (the beat re-runs every 30 s).
            LOGGER.warning(
                "publish_transitions: bridge failed corr=%s",
                getattr(rec, "correlation_id", ""),
                exc_info=True,
            )
            errors += 1
            continue
        published += p_delta
        finished += f_delta
    # Real-time status: a published transition (Queued→Running→Succeeded/Failed)
    # changed a jobstate row, so drop the api caches + wake the jobs-events SSE
    # clients cross-process. Without this the new status only surfaced on the
    # next dashboard poll (the "status changes late" lag); now it pushes the
    # instant the bridge advances. Best-effort, gated only by there being a real
    # change to announce.
    if published or finished:
        _publish_jobs_cache_invalidate("servicebus_transition")
    return {"scanned": scanned, "published": published, "finished": finished, "errors": errors}


def _dlq_predicate(
    cfg: ServiceBusConfig, total_dlq: int
) -> Callable[[ParsedMessage], bool]:
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
        return bool(enq.timestamp() <= cutoff)

    return predicate


@shared_task(name="api.tasks.servicebus.dlq_cleanup")
@skip_tick_on_transient_infra
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
        return _stage_dead_letter_response_and_backup(cfg, msg)

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

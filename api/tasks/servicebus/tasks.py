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
    env gate plus the saved config must both opt in. All three beat tasks also
    skip the current tick (returning ``{"skipped": "transient"}``) on a
    transient connectivity/DNS error from a top-level Table / Service Bus read,
    so a brief platform blip self-heals on the next tick instead of crashing
    with an exception Celery cannot pickle. The drain handler is
    idempotent on ``external_correlation_id`` (Service Bus is at-least-once);
    a duplicate completes the message without a second submit. All three tasks
    are BOUNDED per tick (drain/publish/cleanup caps) so a backlog drains over
    several ticks instead of spinning one tick forever. Transition events are
    emitted only on an actual status change (``last_status`` marker) so the
    topic does not flood. A caller-supplied ``request_id`` pass-through value on
    a request message is captured at drain time and echoed onto every published
    transition event (body + topic envelope) so a topic subscriber correlates on
    the same value the producer set. A succeeded transition event additionally
    carries ``result_files`` (per-file metadata + a dashboard ``download_url``
    for the authenticated streaming gateway — pointers only, never a SAS URL or
    result bytes; charter §9).
Validation: ``uv run pytest -q api/tests/test_servicebus_tasks.py``.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from celery import shared_task
from fastapi import HTTPException

from api.services import external_blast, service_bus
from api.services.service_bus import MessageAction, ParsedMessage
from api.services.service_bus_pref import (
    ServiceBusConfig,
    get_service_bus_config,
    service_bus_enabled,
)
from api.services.service_bus_tracking import (
    BridgeRecord,
    claim_bridge,
    get_bridge,
    list_active_bridges,
    mark_done,
    mark_published,
    release_bridge,
    upsert_bridge,
)
from api.tasks.servicebus.dlq_backup import backup_dead_letter_message
from api.tasks.transient import skip_tick_on_transient_infra

LOGGER = logging.getLogger(__name__)

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
    raw = os.environ.get("SERVICEBUS_DRAIN_CONCURRENCY", "1")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        LOGGER.warning(
            "invalid SERVICEBUS_DRAIN_CONCURRENCY=%r; defaulting to 1 (serial)", raw
        )
        value = 1
    return max(1, min(32, value))


_DRAIN_CONCURRENCY = _drain_concurrency_from_env()

# Atomic single-writer claim gate (charter §12a Rule 4, default-OFF). When ON the
# drain reserves each correlation id with an atomic insert BEFORE submitting, so
# a parallel / multi-worker drain can never submit the same request twice (the
# get_bridge → upsert_bridge read-modify-write is otherwise racy). OFF keeps the
# legacy "any existing bridge row dedups" behaviour unchanged. Pair this ON with
# SERVICEBUS_DRAIN_CONCURRENCY>1 — parallel submit is only safe with the claim.
_ATOMIC_CLAIM = os.environ.get("SERVICEBUS_ATOMIC_CLAIM", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

# Single-flight drain gate (charter §12a Rule 4, default-OFF). When ON, a drain
# tick takes a short-lived Redis lease before draining so two overlapping beat
# ticks (a tick that ran longer than the 10s interval) or two workers cannot
# drain the same queue at once. The atomic claim (#2) already prevents duplicate
# submits; this just removes the wasted receiver contention / lock churn / log
# noise of N workers racing the same queue. FAIL-OPEN: a Redis error never
# blocks a drain (the lease is an optimisation, not a correctness gate), so a
# broker blip degrades to the legacy every-tick drain instead of stalling.
_DRAIN_SINGLEFLIGHT = os.environ.get(
    "SERVICEBUS_DRAIN_SINGLEFLIGHT", ""
).strip().lower() in {"1", "true", "yes"}
_DRAIN_LOCK_KEY = "servicebus:drain:singleflight"


def _drain_lock_key(queue_name: str) -> str:
    """Queue-scoped lease key so distinct request queues never block each other."""
    return f"{_DRAIN_LOCK_KEY}:{queue_name}" if queue_name else _DRAIN_LOCK_KEY


def _drain_lock_ttl_from_env() -> int:
    """Lease TTL in seconds, floored at 10s, fail-safe on a bad value.

    Must exceed a normal tick's drain time so the holder finishes before it
    expires, but stay small enough that a crashed holder (which never runs the
    release) frees the lease quickly. The release is best-effort; the TTL is the
    backstop.
    """
    raw = os.environ.get("SERVICEBUS_DRAIN_LOCK_TTL_SECONDS", "120")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        LOGGER.warning(
            "invalid SERVICEBUS_DRAIN_LOCK_TTL_SECONDS=%r; defaulting to 120", raw
        )
        value = 120
    return max(10, value)


_DRAIN_LOCK_TTL = _drain_lock_ttl_from_env()
# Atomic compare-and-delete so a tick only releases a lease it still owns (never
# one a later tick re-acquired after this one's TTL expired).
_DRAIN_LOCK_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


def _acquire_drain_lock(queue_name: str = "") -> tuple[bool, str | None]:
    """Try to take the single-flight drain lease for ``queue_name``.

    Returns ``(proceed, token)``. ``proceed`` is False ONLY when another drain
    demonstrably holds the lease (skip this tick). It is True both when we won
    the lease (``token`` is the release handle) and when the gate is off or Redis
    is unreachable (``token`` is None → nothing to release, fail-open so a broker
    blip never stalls the drain).
    """
    if not _DRAIN_SINGLEFLIGHT:
        return (True, None)
    try:
        from api.services.redis_clients import get_broker_redis_client

        client = get_broker_redis_client(socket_timeout=2)
        token = uuid.uuid4().hex
        if client.set(_drain_lock_key(queue_name), token, nx=True, ex=_DRAIN_LOCK_TTL):
            return (True, token)
        return (False, None)
    except Exception:
        LOGGER.debug("drain lock acquire failed; proceeding without lease", exc_info=True)
        return (True, None)


def _release_drain_lock(token: str | None, queue_name: str = "") -> None:
    """Release the drain lease iff we still own it (best-effort, TTL backstop)."""
    if not token:
        return
    try:
        from api.services.redis_clients import get_broker_redis_client

        client = get_broker_redis_client(socket_timeout=2)
        client.eval(_DRAIN_LOCK_RELEASE_LUA, 1, _drain_lock_key(queue_name), token)
    except Exception:
        LOGGER.debug("drain lock release failed (will expire via TTL)", exc_info=True)
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
    Apps FQDN), NOT a Storage SAS — a subscriber downloads by calling it with a
    bearer token and the ``api`` sidecar streams the bytes (charter §9: never
    hand a SAS / direct Storage URL to a consumer). When the dashboard public
    URL cannot be resolved the metadata is still emitted but ``download_url`` is
    omitted so a subscriber can fall back to ``result_ref``.
    """
    from api.services.blast.external_job_projection import _external_result_files
    from api.services.control_plane_url import resolve_control_plane_url

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
        entry: dict[str, Any] = {
            "file_id": file_id,
            "name": item.get("name"),
            "format": item.get("format"),
            "size": item.get("size"),
        }
        if base:
            entry["download_url"] = (
                f"{base}/api/v1/elastic-blast/jobs/{openapi_job_id}/files/{file_id}"
            )
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
    request_id: str = "",
    result_files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a completion-topic ``blast.transition`` event with idempotency keys.

    Every event carries a stable ``event_id`` (sha256 digest of ``corr:status``)
    so a subscriber can dedupe an at-least-once re-delivery idempotently — this
    is the authoritative dedup key. ``attempt`` is an informational counter that
    in practice is always 1 (the publish loop cannot distinguish a first publish
    from a re-publish using the bridge marker alone, so it does not try; see
    ``publish_transitions``); it is kept in the schema for stability and for the
    explicit ``attempt=1`` timeout-failure event. ``result_ref`` points at the
    dashboard result API (pointers only — never result bytes; charter §9).
    ``result_files`` (succeeded events only) carries the per-file metadata plus a
    concrete ``download_url`` for the dashboard's authenticated streaming gateway
    so a subscriber can download results directly — still pointers, never bytes
    or SAS URLs. ``request_id`` is the caller-supplied pass-through value from the
    request queue message; it is echoed onto the event (and the topic envelope)
    only when the producer set one, so a subscriber correlates on the SAME value.
    It is NOT part of the ``event_id`` digest — it is constant per correlation
    id, so including it would not change dedup semantics.
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
    if result_files is not None:
        event["result_files"] = result_files
    if request_id:
        event["request_id"] = request_id
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
    from api.services.blast.submit_payload import (
        _caller_supplied_searchsp,
        canonical_submit_metadata,
        resolve_sharded_db_resource_profile,
        resolve_sharding_plan,
    )

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
    for key in (
        "outfmt",
        "word_size",
        "dust",
        "evalue",
        "max_target_seqs",
        "sharding_mode",
        "db_effective_search_space",
    ):
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
    # Server-derived sharding default (mirrors the direct /api/v1 submit path):
    # a memory-heavy DB like core_nt MUST run sharded, which the sibling only
    # does for a sharding-family resource_profile. A Service Bus producer that
    # omits the profile would otherwise get a non-sharded config that
    # elastic-blast rejects on the memory-fit check. Promote a missing/standard
    # profile; an explicit profile is preserved.
    payload["resource_profile"] = resolve_sharded_db_resource_profile(
        payload.get("db") or "", payload.get("resource_profile")
    )
    plan = resolve_sharding_plan(
        program=str(payload.get("program") or "blastn"),
        database=str(payload.get("db") or ""),
        options=payload.get("options"),
        caller_supplied_searchsp=_caller_supplied_searchsp(body),
        allow_servicebus_downgrade=True,
    )
    payload["options"] = plan.options
    payload.update(
        canonical_submit_metadata(
            payload,
            submission_source="servicebus",
            correlation_id=correlation_id,
        )
    )
    return payload


def _is_v1_jobs_message(body: dict[str, Any]) -> bool:
    """True when a message wants the free-form ``/v1/jobs`` (multi-token) path.

    The signal is a ``blast_options`` object (the sibling ``/v1/jobs`` shape).
    A message using the XML-locked ``options`` object (``ExternalBlastOptions``)
    keeps the existing ``/api/v1/elastic-blast/submit`` path. The two are
    mutually exclusive by key name, so detection is unambiguous and a producer
    explicitly opts into the tabular path by sending ``blast_options``.
    """
    return isinstance(body.get("blast_options"), dict)


def _build_v1_jobs_payload(
    msg: ParsedMessage, cfg: ServiceBusConfig
) -> dict[str, Any] | None:
    """Map a ``blast_options`` message to a validated ``/v1/jobs`` payload.

    Returns ``None`` when the message cannot ever succeed (malformed / missing
    required fields) so the caller dead-letters it instead of retrying forever.
    Mirrors ``_build_request_payload`` but validates against
    ``ExternalBlastV1Request`` (free-form ``outfmt`` + ``extra``) and stamps the
    server-derived metadata (wire ``submission_source=external_api`` for the
    sibling, sharded-DB ``resource_profile`` promotion, correlation id).
    """
    from api.routes.elastic_blast import ExternalBlastV1Request
    from api.services.blast.submit_payload import (
        canonical_submit_metadata,
        resolve_sharded_db_resource_profile,
        resolve_sharding_plan,
    )

    body = dict(msg.body or {})
    correlation_id = (
        str(body.get("external_correlation_id") or "").strip()
        or (msg.correlation_id or "").strip()
        or (msg.message_id or "").strip()
    )
    if not correlation_id:
        return None

    candidate: dict[str, Any] = {
        "query_fasta": body.get("query_fasta"),
        "db": body.get("db"),
        "program": body.get("program") or "blastn",
        "external_correlation_id": correlation_id,
    }
    if isinstance(body.get("blast_options"), dict):
        candidate["blast_options"] = body["blast_options"]
    for key in ("taxid", "is_inclusive", "priority", "batch_len", "idempotency_key"):
        if body.get(key) is not None:
            candidate[key] = body[key]

    try:
        request = ExternalBlastV1Request(**candidate)
    except Exception:
        LOGGER.warning("service bus v1 request validation failed corr=%s", correlation_id)
        return None

    payload = request.model_dump(exclude_none=True)
    # Server-derived sharding default (same as the XML path): a memory-heavy DB
    # like core_nt MUST run sharded, which the sibling only does for a
    # sharding-family resource_profile. Promote a missing/standard profile.
    payload["resource_profile"] = resolve_sharded_db_resource_profile(
        payload.get("db") or "", payload.get("resource_profile")
    )
    # Forward the dashboard's Web BLAST search-space oracle value to BLAST on
    # the free-form /v1/jobs path so an outfmt-7 Service Bus submit applies the
    # SAME calibrated -searchsp the dashboard New Search native path emits
    # (api/services/blast/config.py generate_config → "-searchsp <N>").
    #
    # Why this is needed (verified against the sibling):
    #   * The sibling /v1/jobs BlastOptions has only outfmt + extra (no
    #     structured searchsp field); raw flags in `extra` reach BLAST.
    #   * When no -searchsp / -dbsize is present, the sibling submit_job
    #     auto-injects a FIXED default -searchsp 32156241807668
    #     (docker-openapi/app/main.py). That value is core_nt's calibration, so
    #     it is correct ONLY for core_nt — a caller-supplied value, a snapshot
    #     drift, or a future per-database calibration never reaches BLAST.
    #   * The XML /api/v1/elastic-blast/submit path does NOT help here: the
    #     sibling external_submit handler drops db_effective_search_space and
    #     builds its own `extra` (word_size/dust only), then delegates to the
    #     same submit_job → it too relies on that fixed auto-inject. So this v1
    #     path is the first external surface that forwards the dashboard's
    #     computed per-database value.
    #
    # resolve_sharding_plan (allow_servicebus_downgrade=True → never blocks, only
    # degrades) resolves the per-database / drift-adjusted / caller-supplied
    # value; we forward it as a raw -searchsp flag in blast_options.extra. A
    # caller-pinned -searchsp / -dbsize is never overridden.
    # db_effective_search_space is our convenience field (mirrors the XML
    # request model), not a sibling wire field, so it is stripped before the
    # payload leaves. searchsp resolution must never fail a valid submit — on any
    # error we skip injection and let the sibling apply its own default.
    blast_options = payload.get("blast_options")
    if isinstance(blast_options, dict):
        caller_searchsp = blast_options.pop("db_effective_search_space", None)
        already = f"{blast_options.get('extra') or ''} {blast_options.get('outfmt') or ''}"
        if "-searchsp" not in already and "-dbsize" not in already:
            resolved_searchsp = None
            plan = None
            try:
                plan = resolve_sharding_plan(
                    program=str(payload.get("program") or "blastn"),
                    database=str(payload.get("db") or ""),
                    options={
                        "additional_options": str(blast_options.get("extra") or ""),
                        "db_effective_search_space": caller_searchsp,
                        "db_total_letters": body.get("db_total_letters"),
                        "db_total_sequences": body.get("db_total_sequences"),
                    },
                    caller_supplied_searchsp=(
                        caller_searchsp if isinstance(caller_searchsp, int) else None
                    ),
                    allow_servicebus_downgrade=True,
                )
                resolved_searchsp = plan.options.get("db_effective_search_space")
            except Exception as exc:  # never fail a valid submit over searchsp
                LOGGER.warning(
                    "service bus v1 searchsp resolution skipped corr=%s: %s",
                    correlation_id,
                    type(exc).__name__,
                )
                resolved_searchsp = None
            if resolved_searchsp:
                existing_extra = str(blast_options.get("extra") or "").strip()
                blast_options["extra"] = (
                    f"{existing_extra} -searchsp {int(resolved_searchsp)}".strip()
                )
                LOGGER.info(
                    "service bus v1 searchsp applied corr=%s db=%s searchsp=%s",
                    correlation_id,
                    payload.get("db"),
                    int(resolved_searchsp),
                )
            elif plan is not None and getattr(plan, "downgraded", False):
                # Calibration does not apply to this DB snapshot; we inject
                # nothing and the sibling falls back to its own default. Log so
                # an unexpected e-value drift is traceable to a known cause.
                LOGGER.info(
                    "service bus v1 searchsp parity downgraded corr=%s db=%s reason=%s",
                    correlation_id,
                    payload.get("db"),
                    getattr(plan, "downgrade_reason", None),
                )
        payload["blast_options"] = blast_options
    # Server-derived source + correlation id (a producer cannot spoof the
    # source). ``canonical_submit_metadata`` reads the just-promoted
    # resource_profile off ``payload`` and preserves it.
    payload.update(
        canonical_submit_metadata(
            payload,
            submission_source="servicebus",
            correlation_id=correlation_id,
        )
    )
    # The sibling ``/v1/jobs`` (``JobSubmitRequest``) only accepts
    # ``submission_source`` in {dashboard, external_api, terminal, system} and
    # rejects ``servicebus`` with HTTP 400 (the XML ``/api/v1/elastic-blast/
    # submit`` path silently rewrites it to ``external_api`` internally, so it
    # never hit this). Send the sibling-accepted value on the wire while the
    # dashboard's own tracking row stays ``servicebus`` — that row is written
    # separately by ``_persist_drain_row_and_trace`` and is not derived from
    # this payload field.
    payload["submission_source"] = "external_api"
    return payload


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
    )


def _fail_placeholder_for_message(msg: ParsedMessage, *, error_code: str) -> None:
    """Fail the placeholder for a message whose payload could not be built."""
    correlation_id = _correlation_id_from_message(msg)
    if correlation_id:
        _fail_placeholder(correlation_id, error_code=error_code)


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
        _fail_placeholder_for_message(msg, error_code="servicebus_malformed_request")
        _publish_jobs_cache_invalidate("servicebus_drain_malformed")
        return MessageAction.DEAD_LETTER

    correlation_id = str(payload["external_correlation_id"])
    received_ts = _now_iso()
    request_id = _extract_request_id(msg)

    # Idempotency: at-least-once delivery means we may see this twice. With the
    # atomic-claim gate OFF (legacy) ANY existing bridge row dedups. With it ON
    # only a CONFIRMED row (one carrying an openapi_job_id) dedups here; a bare
    # in-flight reservation is handled by claim_bridge below, so two concurrent
    # drains of the same correlation id can never both submit.
    existing = get_bridge(correlation_id)
    if existing is not None and (existing.openapi_job_id or not _ATOMIC_CLAIM):
        LOGGER.info("service bus duplicate request corr=%s (already bridged)", correlation_id)
        return MessageAction.COMPLETE

    # Atomic single-writer reservation (gate-on). The winner submits; a contended
    # fresh reservation means another worker is mid-submit, so defer (abandon) and
    # let that worker's single submit stand — this is what makes the parallel /
    # multi-worker drain safe against duplicate BLAST runs. A stale reservation
    # (a worker that crashed between claim and submit) is stolen inside
    # claim_bridge, so a contended claim never wedges the correlation id forever.
    if _ATOMIC_CLAIM and not claim_bridge(correlation_id, request_id):
        LOGGER.info(
            "service bus claim contended corr=%s — deferring to the in-flight submit",
            correlation_id,
        )
        return MessageAction.ABANDON

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
            _fail_placeholder(correlation_id, error_code=f"servicebus_submit_rejected_{status}")
            _publish_jobs_cache_invalidate("servicebus_drain_rejected")
        if _ATOMIC_CLAIM:
            # Submit failed after we reserved the correlation id, so roll the
            # reservation back: a transient ABANDON can then re-claim + resubmit
            # on redelivery, and a permanent DEAD_LETTER leaves no phantom
            # ``claimed`` row behind.
            release_bridge(correlation_id)
        return MessageAction.DEAD_LETTER if permanent else MessageAction.ABANDON
    except Exception:
        # Unknown/unexpected error — treat as transient (abandon → redelivery)
        # so a transient glitch never loses a submit.
        LOGGER.exception("service bus → OpenAPI submit failed corr=%s", correlation_id)
        if _ATOMIC_CLAIM:
            release_bridge(correlation_id)
        return MessageAction.ABANDON

    openapi_job_id = str(upstream.get("job_id") or "")
    upsert_bridge(
        BridgeRecord(
            correlation_id=correlation_id,
            openapi_job_id=openapi_job_id,
            last_status="",
            done=False,
            request_id=request_id,
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
                request_id=request_id,
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
@skip_tick_on_transient_infra
def drain_and_resubmit() -> dict[str, Any]:
    """Drain the request queue → bridge each message to the OpenAPI plane."""
    if not service_bus_enabled():
        return {"skipped": "disabled"}
    cfg = get_service_bus_config()
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
            max_messages=_DRAIN_MAX_MESSAGES,
            max_concurrency=_DRAIN_CONCURRENCY,
        )
        # Observability (self-critique #6): one structured line per non-empty tick
        # so drain throughput / fan-out effectiveness is visible in App Insights
        # without parsing per-message logs. Silent on an idle tick (received==0)
        # to avoid flooding the log when the queue is empty.
        if stats.received:
            LOGGER.info(
                "servicebus drain tick received=%d completed=%d abandoned=%d "
                "dead_lettered=%d concurrency=%d",
                stats.received,
                stats.completed,
                stats.abandoned,
                stats.dead_lettered,
                _DRAIN_CONCURRENCY,
            )
        return {
            "received": stats.received,
            "completed": stats.completed,
            "abandoned": stats.abandoned,
            "dead_lettered": stats.dead_lettered,
            "concurrency": _DRAIN_CONCURRENCY,
        }
    finally:
        _release_drain_lock(lock_token, cfg.request_queue)


def _publish_one_bridge(
    cfg: ServiceBusConfig,
    rec: BridgeRecord,
    openapi_kwargs: dict[str, str],
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
        # ages past the deadline so it cannot linger forever.
        if _bridge_expired(rec.created_at):
            mark_done(rec.correlation_id, _STATUS_FAILED)
            return (0, 1)
        return (0, 0)
    try:
        job = external_blast.get_job(rec.openapi_job_id, **openapi_kwargs)
    except Exception:  # transient; retry next tick
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
                request_id=rec.request_id,
            )
            try:
                service_bus.publish_event(cfg, timeout_event)
            except Exception:  # retry next tick (marker unchanged)
                LOGGER.warning("timeout publish failed corr=%s", rec.correlation_id)
                return (0, 0)
            _record_transition_trace(rec.openapi_job_id, _STATUS_FAILED)
            mark_done(rec.correlation_id, _STATUS_FAILED)
            return (1, 1)
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
    if status == _STATUS_FAILED:
        err = job.get("error") if isinstance(job.get("error"), dict) else {}
        error_code = str((err or {}).get("code") or "failed")
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
        request_id=rec.request_id,
        result_files=result_files,
    )
    try:
        service_bus.publish_event(cfg, event)
    except Exception:
        LOGGER.warning("transition publish failed corr=%s", rec.correlation_id)
        return (0, 0)
    _record_transition_trace(rec.openapi_job_id, status)
    if status in _TERMINAL:
        mark_done(rec.correlation_id, status)
        return (1, 1)
    mark_published(rec.correlation_id, status)
    return (1, 0)


@shared_task(name="api.tasks.servicebus.publish_transitions")
@skip_tick_on_transient_infra
def publish_transitions() -> dict[str, Any]:
    """Poll sibling status for active bridges and emit one event per change."""
    if not service_bus_enabled():
        return {"skipped": "disabled"}
    cfg = get_service_bus_config()
    # Fetch the work set BEFORE resolving the OpenAPI client kwargs. With zero
    # active bridges there is nothing to poll, and `_openapi_kwargs` reads the
    # configured cluster's `elb-openapi` Service IP from the Kubernetes API on
    # every tick (30 s). When that cluster is stopped or was recreated with a
    # new API-server FQDN, the read raises a `requests` ConnectionError that the
    # OpenTelemetry instrumentation auto-records as an App Insights
    # dependency-failure exception — flooding the telemetry with thousands of
    # identical traces for a no-op tick. Resolving lazily makes the idle path
    # touch nothing but the local tracking store.
    bridges = list_active_bridges(limit=_PUBLISH_MAX_ROWS)
    if not bridges:
        return {"scanned": 0, "published": 0, "finished": 0, "errors": 0}
    published = 0
    finished = 0
    scanned = 0
    errors = 0
    openapi_kwargs = _openapi_kwargs(cfg)
    for rec in bridges:
        scanned += 1
        try:
            p_delta, f_delta = _publish_one_bridge(cfg, rec, openapi_kwargs)
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
    return {"scanned": scanned, "published": published, "finished": finished, "errors": errors}


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

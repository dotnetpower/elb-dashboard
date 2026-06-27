"""`/api/aks/autostop` routes — opt-in idle auto-stop cost saver.

Responsibility: HTTP surface for reading, updating, and acting on
    per-cluster `AutoStopPreference` rows. Defers every decision to
    `auto_stop_evaluator`; never calls the AKS SDK directly.
Edit boundaries: Route validation and response shaping only. Storage
    lives in `api.services.auto_stop`; idle decisions live in
    `api.services.auto_stop_evaluator`; the Celery driver lives in
    `api.tasks.azure.idle_autostop`.
Key entry points: `get_autostop`, `put_autostop`, `extend_autostop`,
    `autostop_status`.
Risky contracts: Every non-health `/api/*` route must enforce
    `require_caller`.
Validation: `uv run pytest -q api/tests/test_aks_autostop_route.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth import (
    CallerIdentity,
    is_dev_bypass_caller,
    require_caller,
)
from api.services.auto_stop import (
    ALLOWED_IDLE_MINUTES,
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_IDLE_MINUTES,
    EXTEND_GRANT_MINUTES,
    AutoStopPreference,
    extend_auto_stop_preference,
    get_auto_stop_preference,
    normalise_preference,
    save_auto_stop_preference,
)
from api.services.auto_stop_evaluator import evaluate_cluster
from api.services.feature_events import record_feature_event
from api.services.sanitise import (
    redact_oid,
    sanitise,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# Hard cap on extend grants. 24h is what the storage layer allows for
# legitimate scenarios (paused weekend run), but the SPA "Extend" button
# is a quick-touch control — a misclick at 24h disables the cost-saver
# for an entire workday. 4h matches the longest idle bucket so a
# researcher who needs more time can simply re-press Extend.
MAX_EXTEND_MINUTES = 4 * 60

# Per-cluster status cache (two-tier).
#
# Why two tiers? The SPA polls /autostop/status every 60s for every
# visible cluster card. N clusters \u00d7 M browsers without a cache
# would mean N*M Table reads + ARM lookups per minute. A short TTL
# collapses the fan-in to one compute per cluster.
#
# L1 (in-process) collapses concurrent polls hitting the SAME uvicorn
# worker. Tiny TTL (``_STATUS_L1_TTL_SECONDS=2``) so a PUT on this
# worker is reflected on the very next read.
#
# L2 (Redis, ``autostop:status:<key>``) collapses concurrent polls
# hitting DIFFERENT uvicorn workers (charter pins ``minReplicas=1
# maxReplicas=1`` but the api sidecar runs 2 uvicorn workers per
# replica; a browser polling at 60s round-robins between them).
# ``_STATUS_L2_TTL_SECONDS=5`` matches the original single-tier TTL
# so cross-worker stale window stays in the same envelope. Critique
# #18: previously L1 was the only cache, so a PUT on worker-A was
# invisible to worker-B for up to 5 s and idempotent re-polls re-ran
# the entire compute on worker-B \u2014 wasted Table+ARM lookups.
#
# Both tiers cache identical JSON payloads. Redis unreachable degrades
# to L1-only; ``get_ops_redis_client`` short-timeout means we never
# stall the request waiting for Redis.
_STATUS_L1_TTL_SECONDS = 2.0
_STATUS_L2_TTL_SECONDS = 5
_STATUS_TTL_SECONDS = _STATUS_L1_TTL_SECONDS  # kept for tests that still import this name
_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STATUS_CACHE_LOCK = threading.Lock()
# Belt-and-braces cap on the L1 status cache. Key space is (sub, rg, cluster)
# so this stays tiny in practice; the cap bounds growth on a long-lived api
# sidecar that never recycles.
_STATUS_CACHE_MAX_ENTRIES = 64
_STATUS_REDIS_KEY_PREFIX = "autostop:status:"
# Per-key in-flight gate. Without this, cache-miss thunderstorms (M
# browsers polling the same cluster simultaneously) all run
# ``_compute_status`` in parallel. The gate lets the first caller
# compute and subsequent callers wait for the result.
#
# Cross-loop / cross-thread shareable via ``threading.Event``. The
# async route never calls ``gate.wait()`` directly — it wraps the
# wait in ``asyncio.to_thread`` (critique #9.4) so the event-loop
# thread is not blocked while a follower waits for the leader's
# compute. Threadpool slots are still consumed during the wait, but
# only briefly (TTL = 5 s) and only when the cache is empty.
_STATUS_INFLIGHT: dict[str, threading.Event] = {}
_STATUS_INFLIGHT_LOCK = threading.Lock()

# Reasons that indicate a transient backend issue (Table / ARM / evaluator
# blip). Caching these freezes the SPA banner on a stale "we don't know"
# answer for the full TTL. The happy path (active / idle_pending /
# disabled / cooldown / extended / power_state:*) is safe to cache —
# those depend only on state that changes on the order of minutes.
_NON_CACHEABLE_REASONS = frozenset(
    {
        "state_repo_unreachable",
        "evaluator_unavailable",
        "power_state_unknown",
        "history_scan_truncated",
    }
)


def _status_cache_key(subscription_id: str, resource_group: str, cluster_name: str) -> str:
    return f"{subscription_id}|{resource_group}|{cluster_name}"


def _status_cache_put(key: str, body: dict[str, Any]) -> None:
    """Insert into the L1 status cache, evicting the soonest-to-expire entry
    when above ``_STATUS_CACHE_MAX_ENTRIES``. Caller must NOT already hold
    ``_STATUS_CACHE_LOCK``.
    """
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE[key] = (time.monotonic(), body)
        if len(_STATUS_CACHE) > _STATUS_CACHE_MAX_ENTRIES:
            oldest = min(_STATUS_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _STATUS_CACHE.pop(oldest, None)


def _status_pending_queue_depth(power_state: str) -> int | None:
    """Cached Service Bus request-queue keep-alive depth for the status route.

    Mirrors the auto-stop beat driver's queue signal so the SPA countdown
    agrees with the beat decision (a Running cluster shows ``keep`` /
    ``sb_queue_pending:N`` instead of an idle countdown while queued work
    waits). Uses the shared :mod:`api.services.auto_stop_sb_signal` gate with
    its default short TTL so a 60s-per-cluster status poll fan-in collapses to
    at most one Service Bus admin call per window across every cluster and
    browser. Never raises — degrades to ``None`` (additive only).
    """
    try:
        from api.services.auto_stop_sb_signal import pending_queue_signal

        return pending_queue_signal(power_state)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("autostop status sb queue signal unavailable: %s", type(exc).__name__)
        return None


def _status_redis_key(cache_key: str) -> str:
    return f"{_STATUS_REDIS_KEY_PREFIX}{cache_key}"


def _status_redis_client() -> Any | None:
    """Return the ops Redis client or ``None`` if unreachable.

    Imported lazily so tests that never need Redis don't pay the import
    cost, and so the module loads in environments without redis-py.
    """
    try:
        from api.services.redis_clients import get_ops_redis_client

        return get_ops_redis_client(socket_timeout=0.5)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("autostop status redis unavailable: %s", type(exc).__name__)
        return None


def _status_redis_get(cache_key: str) -> dict[str, Any] | None:
    """Read a cached status body from Redis. Returns ``None`` on miss
    or any Redis error \u2014 the caller then falls through to compute."""
    client = _status_redis_client()
    if client is None:
        return None
    try:
        raw = client.get(_status_redis_key(cache_key))
    except Exception as exc:
        LOGGER.debug("autostop status redis get failed: %s", type(exc).__name__)
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        body = json.loads(raw)
        if isinstance(body, dict):
            return body
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    return None


def _status_redis_set(cache_key: str, body: dict[str, Any]) -> None:
    """Cache a status body in Redis for ``_STATUS_L2_TTL_SECONDS``.

    No-op on Redis error \u2014 L1 still serves the same worker; other
    workers will simply recompute on their next poll.
    """
    client = _status_redis_client()
    if client is None:
        return
    try:
        client.setex(
            _status_redis_key(cache_key),
            _STATUS_L2_TTL_SECONDS,
            json.dumps(body),
        )
    except Exception as exc:
        LOGGER.debug("autostop status redis setex failed: %s", type(exc).__name__)


def _status_redis_delete(cache_key: str) -> None:
    """Drop the cached body across all workers."""
    client = _status_redis_client()
    if client is None:
        return
    try:
        client.delete(_status_redis_key(cache_key))
    except Exception as exc:
        LOGGER.debug("autostop status redis del failed: %s", type(exc).__name__)


def _invalidate_status_cache(
    subscription_id: str, resource_group: str, cluster_name: str
) -> None:
    """Drop the cached status row for a cluster.

    Called from any route that mutates the underlying preference so the
    SPA sees a fresh verdict on the very next poll instead of waiting up
    to ``_STATUS_TTL_SECONDS``. Drops BOTH L1 (this worker) and L2
    (cross-worker) so a PUT propagates to siblings within one Redis
    round-trip (critique #18).
    """
    key = _status_cache_key(subscription_id, resource_group, cluster_name)
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE.pop(key, None)
    _status_redis_delete(key)


def _reset_status_cache() -> None:
    """Test hook \u2014 drop every cached row (both tiers) and any
    leftover in-flight gates."""
    with _STATUS_CACHE_LOCK:
        keys = list(_STATUS_CACHE.keys())
        _STATUS_CACHE.clear()
    for key in keys:
        _status_redis_delete(key)
    with _STATUS_INFLIGHT_LOCK:
        for evt in _STATUS_INFLIGHT.values():
            evt.set()
        _STATUS_INFLIGHT.clear()


class AutoStopPutBody(BaseModel):
    """Browser-supplied body for `PUT /api/aks/autostop`."""

    subscription_id: str = Field(..., min_length=1, max_length=64)
    resource_group: str = Field(..., min_length=1, max_length=90)
    cluster_name: str = Field(..., min_length=1, max_length=63)
    enabled: bool = False
    idle_minutes: int = Field(default=DEFAULT_IDLE_MINUTES, ge=1, le=24 * 60)


class AutoStopExtendBody(BaseModel):
    subscription_id: str = Field(..., min_length=1, max_length=64)
    resource_group: str = Field(..., min_length=1, max_length=90)
    cluster_name: str = Field(..., min_length=1, max_length=63)
    minutes: int = Field(default=EXTEND_GRANT_MINUTES, ge=1, le=MAX_EXTEND_MINUTES)


_PUBLIC_PREF_FIELDS = (
    "subscription_id",
    "resource_group",
    "cluster_name",
    "enabled",
    "idle_minutes",
    "cooldown_minutes",
    "last_stop_at",
    "last_stop_reason",
    "last_skip_at",
    "last_skip_reason",
    "extend_until",
    "updated_at",
)


def _pref_response(
    pref: AutoStopPreference | None,
    *,
    caller: CallerIdentity | None = None,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> dict[str, Any]:
    """Project a preference into a stable wire shape.

    Schema is unified across "row exists" / "row absent" — every key in
    the contract is always present so the SPA does not have to branch
    on ``exists``. ``owner_oid`` / ``tenant_id`` never appear.

    ``editable`` is True when the caller may modify the row through
    PUT/extend. Defaults to True for the "no row exists" shape so the
    SPA can render the toggle in its off state and let the first writer
    create the row. Defaults to True for rows the caller owns. False
    when the row is owned by a different real user.
    """
    if pref is None:
        return {
            "exists": False,
            "editable": True,
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": cluster_name,
            "enabled": False,
            "idle_minutes": DEFAULT_IDLE_MINUTES,
            "cooldown_minutes": DEFAULT_COOLDOWN_MINUTES,
            "allowed_idle_minutes": list(ALLOWED_IDLE_MINUTES),
            "last_stop_at": "",
            "last_stop_reason": "",
            "last_skip_at": "",
            "last_skip_reason": "",
            "extend_until": "",
            "updated_at": "",
        }
    raw = pref.to_dict()
    payload: dict[str, Any] = {key: raw.get(key, "") for key in _PUBLIC_PREF_FIELDS}
    payload["exists"] = True
    payload["allowed_idle_minutes"] = list(ALLOWED_IDLE_MINUTES)
    payload["editable"] = _caller_owns(pref, caller)
    return payload


def _caller_owns(pref: AutoStopPreference, caller: CallerIdentity | None) -> bool:
    """Return True when ``caller`` may modify ``pref``.

    Rules:
    * Anyone may modify a row with an empty ``owner_oid`` (legacy /
      first-writer-wins).
    * Owner matches → yes.
    * Dev-bypass identity → yes (local-only escape so a developer can
      always inspect/repair any row).
    * Same Azure tenant → yes (charter §14: the dashboard is
      single-tenant per deployment; every authenticated caller from
      that tenant has already cleared Azure RBAC on the AKS cluster
      itself, so refusing the auto-stop preference just because a
      colleague set it first is theatre, not safety. The PUT path
      logs the implicit ownership transfer so the audit trail still
      attributes the change to the right oid).
    """
    if pref is None or not pref.owner_oid:
        return True
    if caller is None:
        return False
    if pref.owner_oid == caller.object_id:
        return True
    if is_dev_bypass_caller(caller):
        return True
    pref_tenant = (pref.tenant_id or "").strip()
    caller_tenant = (caller.tenant_id or "").strip()
    if pref_tenant and caller_tenant and pref_tenant == caller_tenant:
        return True
    return False


def _redact_for_foreign_caller(
    pref: AutoStopPreference,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Return the "no preference" shape so a foreign caller sees the
    default off state instead of someone else's bookkeeping fields
    (when they last stopped, what reason, etc.). This is the read-side
    of the ownership guard."""
    return _pref_response(
        None,
        caller=None,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    ) | {"editable": False}


def _check_ownership(pref: AutoStopPreference | None, caller: CallerIdentity) -> None:
    """Refuse cross-owner mutations on a non-empty owner row.

    First-writer-wins: a pref with ``owner_oid == ""`` accepts any
    authenticated caller. Once a real signed-in user has set a
    preference, only that same oid (or the dev-bypass identity, locally)
    may modify it via the dashboard. This is the same pragmatic contract
    used by BLAST job rows — full RBAC against the cluster RG is the
    next-step hardening but is intentionally out of scope for the
    cost-saver shipment.
    """
    if _caller_owns(pref, caller):
        return
    # Critique #9.10 + audit P0 #1: never log the full caller object_id (PII /
    # correlation surface). `redact_oid` returns a stable 12-char sha256 prefix
    # so operators can still grep a single user's trail in App Insights without
    # exposing the GUID itself.
    pref_owner = (pref.owner_oid if pref else "") or ""
    LOGGER.warning(
        "autostop ownership refusal cluster=%s pref_owner=%s caller=%s",
        pref.cluster_name if pref else "?",
        redact_oid(pref_owner) or "?",
        redact_oid(caller.object_id) or "?",
    )
    raise HTTPException(
        status_code=403,
        detail="auto-stop preference is owned by another user",
    )


def _verify_cluster_inputs(subscription_id: str, resource_group: str, cluster_name: str) -> None:
    """Reject empty / obviously-bad scope without leaking a 500."""
    for label, value in (
        ("subscription_id", subscription_id),
        ("resource_group", resource_group),
        ("cluster_name", cluster_name),
    ):
        if not value or not value.strip():
            raise HTTPException(status_code=400, detail=f"missing {label}")


@router.get("/autostop")
def get_autostop(
    subscription_id: str = Query(..., min_length=1),
    resource_group: str = Query(..., min_length=1),
    cluster_name: str = Query(..., min_length=1),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the persisted auto-stop preference for a cluster.

    Returns the default (disabled) shape when no preference exists OR
    when the existing row is owned by a different real user — foreign
    callers see ``editable=false`` plus the "no row" shape so the SPA
    renders a read-only off state instead of leaking bookkeeping fields
    (when the cluster was last stopped, by what reason, etc.).
    """
    _verify_cluster_inputs(subscription_id, resource_group, cluster_name)
    try:
        pref = get_auto_stop_preference(subscription_id, resource_group, cluster_name)
    except Exception as exc:
        LOGGER.warning("get_autostop storage read failed: %s", type(exc).__name__)
        empty = _pref_response(
            None,
            caller=caller,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
        return {**empty, "degraded": True}
    if pref is not None and not _caller_owns(pref, caller):
        return _redact_for_foreign_caller(
            pref,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    return _pref_response(
        pref,
        caller=caller,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )


@router.put("/autostop")
def put_autostop(
    body: AutoStopPutBody = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Upsert the auto-stop preference for a cluster.

    Reject ``idle_minutes`` that is not one of `ALLOWED_IDLE_MINUTES` at
    the boundary so a buggy / outdated client gets a clear contract
    error instead of a silent clamp. ``enabled=False`` persists with
    `idle_minutes` intact so re-enabling later restores the user's
    previous bucket choice without re-asking.
    """
    if body.idle_minutes not in ALLOWED_IDLE_MINUTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"idle_minutes must be one of {list(ALLOWED_IDLE_MINUTES)}, "
                f"got {body.idle_minutes}"
            ),
        )
    existing = get_auto_stop_preference(
        body.subscription_id, body.resource_group, body.cluster_name
    )
    _check_ownership(existing, caller)
    # Audit-log a cross-oid same-tenant transfer so the trail still attributes
    # the change to the right oid even after ``_caller_owns`` allowed it.
    # Hashes only (never raw oids) -- matches the §12a logging contract.
    if (
        existing is not None
        and existing.owner_oid
        and existing.owner_oid != caller.object_id
        and not is_dev_bypass_caller(caller)
    ):
        LOGGER.info(
            "autostop ownership transfer cluster=%s prev_owner=%s new_owner=%s",
            existing.cluster_name,
            redact_oid(existing.owner_oid) or "?",
            redact_oid(caller.object_id) or "?",
        )
    payload = body.model_dump()
    payload["owner_oid"] = caller.object_id
    payload["tenant_id"] = caller.tenant_id
    try:
        pref = normalise_preference(payload)
    except ValueError as exc:
        # Audit P1 #7: sanitise + cap exception text.
        raise HTTPException(status_code=400, detail=sanitise(str(exc))[:200]) from exc
    # Preserve cooldown/extend/last-* state across updates — the user is
    # only changing the toggle / bucket choice; clobbering bookkeeping
    # would re-open a cluster that is mid-cooldown.
    if existing is not None:
        pref.cooldown_minutes = existing.cooldown_minutes
        pref.last_stop_at = existing.last_stop_at
        pref.last_stop_reason = existing.last_stop_reason
        pref.last_skip_at = existing.last_skip_at
        pref.last_skip_reason = existing.last_skip_reason
        pref.extend_until = existing.extend_until
        # ``created_at`` / ``last_started_at`` are idle-clock anchors the
        # evaluator reads; the PUT body never carries them, so without this
        # restore every toggle would reset ``created_at`` to now (and drop a
        # real ``last_started_at``). Carry them forward by default.
        pref.created_at = existing.created_at
        pref.last_started_at = existing.last_started_at
        # ``last_live_activity_at`` is the durable live-K8s-activity anchor
        # (same role as ``last_started_at`` for OpenAPI runs). The PUT body
        # never carries it, so restore it too — otherwise toggling the
        # setting would wipe the high-water mark and the idle deadline would
        # regress to ``created_at`` on the next tick, stopping a cluster that
        # finished a BLAST burst only moments ago.
        pref.last_live_activity_at = existing.last_live_activity_at
        # Idle-clock reset on a SHORTER window. Shrinking ``idle_minutes``
        # (e.g. 4h → 1h) recomputes ``deadline = last_activity + new_window``;
        # when the last activity predates the new (smaller) window the
        # evaluator would fire ``stop`` (or a warn banner) on the very next
        # tick — the cluster looks like it is "about to stop immediately"
        # right after the user lowered the limit. Treat a downward change as
        # a fresh intent signal: stamp ``last_started_at = now`` so the new,
        # shorter window is measured from the change moment. Raising the
        # window (1h → 4h) only extends the deadline, so it needs no reset.
        if existing.idle_minutes and pref.idle_minutes < existing.idle_minutes:
            pref.last_started_at = pref.updated_at
    saved = save_auto_stop_preference(pref)
    _invalidate_status_cache(
        body.subscription_id, body.resource_group, body.cluster_name
    )
    LOGGER.info(
        "autostop upsert cluster=%s enabled=%s idle_minutes=%s by=%s",
        saved.cluster_name,
        saved.enabled,
        saved.idle_minutes,
        redact_oid(caller.object_id),
    )
    # Audit who toggled / re-tuned auto-stop. Records both the new state and
    # the previous one so the App Insights customEvent answers "who turned it
    # off?" / "who shortened the idle window?" directly.
    prev_enabled = existing.enabled if existing is not None else None
    prev_idle = existing.idle_minutes if existing is not None else None
    record_feature_event(
        "autostop_config",
        status="updated",
        actor="user",
        actor_oid=caller.object_id,
        cluster=saved.cluster_name,
        resource_group=saved.resource_group,
        enabled=saved.enabled,
        idle_minutes=saved.idle_minutes,
        prev_enabled=prev_enabled,
        prev_idle_minutes=prev_idle,
    )
    return _pref_response(
        saved,
        caller=caller,
        subscription_id=body.subscription_id,
        resource_group=body.resource_group,
        cluster_name=body.cluster_name,
    )


@router.post("/autostop/extend")
def extend_autostop(
    body: AutoStopExtendBody = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Push the next-stop deadline out by `minutes` (capped at 4 h).

    Returns 404 if no preference exists — the SPA should not show the
    Extend button until the user has actually enabled auto-stop.
    """
    pref = get_auto_stop_preference(
        body.subscription_id, body.resource_group, body.cluster_name
    )
    if pref is None:
        raise HTTPException(status_code=404, detail="auto-stop preference not found")
    _check_ownership(pref, caller)
    extended = extend_auto_stop_preference(pref, minutes=body.minutes)
    _invalidate_status_cache(
        body.subscription_id, body.resource_group, body.cluster_name
    )
    LOGGER.info(
        "autostop extend cluster=%s minutes=%s until=%s by=%s",
        extended.cluster_name,
        body.minutes,
        extended.extend_until,
        redact_oid(caller.object_id),
    )
    return _pref_response(
        extended,
        caller=caller,
        subscription_id=body.subscription_id,
        resource_group=body.resource_group,
        cluster_name=body.cluster_name,
    )


@router.get("/autostop/status")
async def autostop_status(
    subscription_id: str = Query(..., min_length=1),
    resource_group: str = Query(..., min_length=1),
    cluster_name: str = Query(..., min_length=1),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the current evaluator verdict for the SPA banner.

    Output shape:
        {
          "enabled": bool,
          "idle_minutes": int,
          "editable": bool,
          "verdict": "stop" | "warn" | "keep" | "disabled",
          "reason": str,
          "next_stop_at": iso8601 or "",
          "seconds_until_stop": int,
          "active_job_count": int,
          "cluster_power_state": str,
          "last_stop_at": str,
          "last_skip_at": str,
          "extend_until": str,
        }

    "verdict" is the evaluator's verdict, or ``"disabled"`` when the
    preference exists with ``enabled=false`` (the SPA hides the banner
    in that case). When no preference row exists at all the response
    carries ``verdict="disabled"`` and ``exists=false``. Foreign-owned
    rows are reported as ``disabled`` to the non-owner so a researcher
    cannot infer another user's idle patterns from this surface.

    Concurrency: a per-cluster ``asyncio.Event`` collapses concurrent
    cache-miss requests to one underlying compute — without it, N
    browsers polling the same cluster all run ``_compute_status`` in
    parallel and produce a thundering herd on ARM + state-repo.
    Critique #9.4: the route is async so the singleflight wait does
    not block a uvicorn threadpool slot; the storage/evaluator calls
    are wrapped with ``asyncio.to_thread`` so the event-loop thread
    itself is never blocked.
    Degraded results (state-repo / ARM blip / evaluator failure) are
    NOT cached so the next poll re-attempts immediately.
    """
    _verify_cluster_inputs(subscription_id, resource_group, cluster_name)

    # Ownership check up-front using a cheap pref lookup — this read is
    # cheap (single Table get) and lets us short-circuit before paying
    # for the evaluator + ARM lookup.
    try:
        pref_peek = await asyncio.to_thread(
            get_auto_stop_preference, subscription_id, resource_group, cluster_name
        )
    except Exception as exc:
        LOGGER.debug("autostop_status pref peek failed: %s", type(exc).__name__)
        pref_peek = None
    if pref_peek is not None and not _caller_owns(pref_peek, caller):
        return _disabled_status_shape(subscription_id, resource_group, cluster_name)

    key = _status_cache_key(subscription_id, resource_group, cluster_name)
    now = time.monotonic()
    # L1 check (this worker only). Tiny TTL so a PUT on this worker is
    # reflected on the very next read.
    with _STATUS_CACHE_LOCK:
        cached = _STATUS_CACHE.get(key)
        if cached is not None and (now - cached[0]) < _STATUS_L1_TTL_SECONDS:
            return cached[1]
    # L2 check (shared across uvicorn workers via Redis). On miss we
    # fall through to singleflight + compute; on hit we backfill L1 so
    # rapid repeat polls on the same worker stop hitting Redis.
    redis_body = await asyncio.to_thread(_status_redis_get, key)
    if redis_body is not None:
        _status_cache_put(key, redis_body)
        return redis_body

    # Singleflight: at most one compute per key at a time. The gate is
    # a cross-thread ``threading.Event`` so it works regardless of which
    # event loop / worker process the caller is on. The wait is wrapped
    # in ``asyncio.to_thread`` so the event-loop thread is never blocked
    # (critique #9.4). Followers consume a threadpool slot during the
    # wait but never the loop slot.
    leader = False
    with _STATUS_INFLIGHT_LOCK:
        gate = _STATUS_INFLIGHT.get(key)
        if gate is None:
            gate = threading.Event()
            _STATUS_INFLIGHT[key] = gate
            leader = True
    if not leader:
        # `gate` is guaranteed non-None here — we only enter this branch
        # when we observed a non-None value above. Bind to a local with
        # an explicit type-narrow so static checkers + a runtime guard
        # cover the case where _reset_status_cache cleared the dict
        # between the check and the wait (critique #9.5 — no `assert`).
        wait_gate = gate
        if wait_gate is None:
            raise RuntimeError("singleflight gate vanished between check and wait")
        await asyncio.to_thread(wait_gate.wait, _STATUS_L2_TTL_SECONDS)
        with _STATUS_CACHE_LOCK:
            cached = _STATUS_CACHE.get(key)
            if cached is not None:
                return cached[1]
        # Leader may have populated L2 even if our L1 was cleared.
        redis_body = await asyncio.to_thread(_status_redis_get, key)
        if redis_body is not None:
            _status_cache_put(key, redis_body)
            return redis_body
        # Fall through to compute (leader produced a non-cacheable
        # degraded result, or timed out).

    try:
        body = await asyncio.to_thread(
            _compute_status,
            subscription_id,
            resource_group,
            cluster_name,
            caller,
            pref_peek,
        )
        reason = body.get("reason") or ""
        # Only cache stable, non-degraded answers. Caching a transient
        # "state_repo_unreachable" would freeze the SPA banner on a
        # stale "we don't know" for the full TTL.
        if reason not in _NON_CACHEABLE_REASONS:
            _status_cache_put(key, body)
            # Write-through to Redis so sibling workers see the same
            # body on their next poll. Fire-and-forget on Redis errors.
            await asyncio.to_thread(_status_redis_set, key, body)
        return body
    finally:
        if leader:
            with _STATUS_INFLIGHT_LOCK:
                stale = _STATUS_INFLIGHT.pop(key, None)
            # Critique #9.5: explicit guard, not `assert` (would be
            # stripped under ``python -O``).
            if stale is not None:
                stale.set()
            elif gate is not None:
                gate.set()


def _disabled_status_shape(
    subscription_id: str, resource_group: str, cluster_name: str
) -> dict[str, Any]:
    """Status response that says 'no preference for you, move along'.

    Used for both the genuine "no row" case AND the foreign-caller
    redaction so the two are indistinguishable from the SPA's
    perspective.
    """
    return {
        "exists": False,
        "editable": False,
        "enabled": False,
        "idle_minutes": DEFAULT_IDLE_MINUTES,
        "verdict": "disabled",
        "reason": "no_preference",
        "next_stop_at": "",
        "seconds_until_stop": 0,
        "active_job_count": 0,
        "cluster_power_state": "",
        "last_stop_at": "",
        "last_skip_at": "",
        "extend_until": "",
    }


def _compute_status(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    caller: CallerIdentity,
    pref_peek: AutoStopPreference | None = None,
) -> dict[str, Any]:
    """Body of `/autostop/status` minus the cache layer.

    Critique #9.7: when the route already loaded ``pref`` for the
    ownership check, pass it in via ``pref_peek`` to avoid a second
    Table read. Falls back to a fresh read when called without it
    (e.g. tests / direct callers).
    """
    if pref_peek is not None:
        pref: AutoStopPreference | None = pref_peek
    else:
        pref = get_auto_stop_preference(subscription_id, resource_group, cluster_name)
    if pref is None:
        return _disabled_status_shape(subscription_id, resource_group, cluster_name)

    # Power-state lookup is best-effort: if ARM is unreachable we still
    # return a useful verdict (the evaluator treats "" as unknown and
    # gates on the remaining signals). Never let an ARM blip 500 this
    # endpoint — the SPA polls it on a short cadence.
    power_state = ""
    try:
        from api.services import get_credential
        from api.services.cluster_health import get_cluster_health

        health = get_cluster_health(
            get_credential(), subscription_id, resource_group, cluster_name
        )
        power_state = health.get("power_state") or ""
    except Exception as exc:
        LOGGER.debug(
            "autostop_status power_state lookup failed cluster=%s: %s",
            cluster_name,
            exc,
        )

    try:
        from api.services.state_repo import get_state_repo

        # Probe live K8s ``app=blast`` activity for Running clusters so the
        # SPA countdown reflects OpenAPI-submitted runs (which never write a
        # dashboard jobstate row) and agrees with the auto-stop beat driver.
        # Degrades to (None, None) on any failure — additive protection only.
        live_active_jobs: int | None = None
        live_latest_activity = None
        if power_state == "Running":
            try:
                from api.services.auto_stop_live import probe_live_blast_activity

                probe = probe_live_blast_activity(pref)
                if probe is not None:
                    live_active_jobs, live_latest_activity = probe
                    # Persist the live high-water mark (durable, monotonic,
                    # advance-only) so the idle deadline the SPA shows here
                    # survives the probe going blind on a later poll — without
                    # this the "Stops in" countdown lurches on refresh and the
                    # beat tick can stop the cluster earlier than shown. The
                    # advance-only guard means steady state writes nothing.
                    if live_latest_activity is not None:
                        try:
                            from api.services.auto_stop import (
                                mark_auto_stop_live_activity,
                            )

                            mark_auto_stop_live_activity(
                                subscription_id,
                                resource_group,
                                cluster_name,
                                live_latest_activity,
                                known=pref,
                            )
                        except Exception as exc:
                            LOGGER.debug(
                                "autostop_status live anchor persist failed "
                                "cluster=%s: %s",
                                cluster_name,
                                exc,
                            )
            except Exception as exc:
                LOGGER.debug(
                    "autostop_status live blast probe failed cluster=%s: %s",
                    cluster_name,
                    exc,
                )

        decision = evaluate_cluster(
            pref,
            repo=get_state_repo(),
            power_state=power_state,
            live_active_jobs=live_active_jobs,
            live_latest_activity=live_latest_activity,
            pending_queue_depth=_status_pending_queue_depth(power_state),
        )
    except Exception as exc:
        LOGGER.warning("autostop_status evaluator failed cluster=%s: %s", cluster_name, exc)
        return {
            **_pref_response(
                pref,
                caller=caller,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
            ),
            "verdict": "keep",
            "reason": "evaluator_unavailable",
            "next_stop_at": "",
            "seconds_until_stop": 0,
            "active_job_count": 0,
            "cluster_power_state": power_state,
        }

    return {
        "exists": True,
        "editable": _caller_owns(pref, caller),
        "enabled": pref.enabled,
        "idle_minutes": pref.idle_minutes,
        "verdict": "disabled" if not pref.enabled else decision.verdict,
        "reason": decision.reason,
        "next_stop_at": decision.next_stop_at,
        "seconds_until_stop": decision.seconds_until_stop,
        "active_job_count": decision.active_job_count,
        "cluster_power_state": decision.cluster_power_state or power_state,
        "last_stop_at": pref.last_stop_at,
        "last_skip_at": pref.last_skip_at,
        "extend_until": pref.extend_until,
    }

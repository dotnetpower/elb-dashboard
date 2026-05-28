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
    DEFAULT_IDLE_MINUTES,
    EXTEND_GRANT_MINUTES,
    AutoStopPreference,
    extend_auto_stop_preference,
    get_auto_stop_preference,
    normalise_preference,
    save_auto_stop_preference,
)
from api.services.auto_stop_evaluator import evaluate_cluster

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# Hard cap on extend grants. 24h is what the storage layer allows for
# legitimate scenarios (paused weekend run), but the SPA "Extend" button
# is a quick-touch control — a misclick at 24h disables the cost-saver
# for an entire workday. 4h matches the longest idle bucket so a
# researcher who needs more time can simply re-press Extend.
MAX_EXTEND_MINUTES = 4 * 60

# Per-cluster status cache. The SPA polls /autostop/status every 60s for
# every visible cluster card; N clusters × M browsers without a cache
# means N*M Table reads + ARM lookups per minute. A short TTL collapses
# concurrent polls to one fan-out per cluster.
#
# MULTI-WORKER CAVEAT: the api sidecar runs uvicorn with 2 workers, so
# this cache is per-process. A PUT routed to worker-1 invalidates only
# worker-1's cache; worker-2 keeps serving its own cached row for up to
# ``_STATUS_TTL_SECONDS``. The TTL is therefore kept SHORT (5 s) so the
# worst-case cross-worker stale window is below human perception
# instead of the original 30 s. A future Redis-backed cache + pub/sub
# invalidation would close this fully; until then, 5 s is the
# pragmatic trade-off between SPA fan-in collapse and cross-worker
# coherence.
_STATUS_TTL_SECONDS = 5.0
_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STATUS_CACHE_LOCK = threading.Lock()
# Per-key in-flight gate. Without this, cache-miss thunderstorms (M
# browsers polling the same cluster simultaneously) all run
# ``_compute_status`` in parallel. The gate lets the first caller
# compute and subsequent callers wait for the result.
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


def _invalidate_status_cache(
    subscription_id: str, resource_group: str, cluster_name: str
) -> None:
    """Drop the cached status row for a cluster.

    Called from any route that mutates the underlying preference so the
    SPA sees a fresh verdict on the very next poll instead of waiting up
    to ``_STATUS_TTL_SECONDS``.
    """
    key = _status_cache_key(subscription_id, resource_group, cluster_name)
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE.pop(key, None)


def _reset_status_cache() -> None:
    """Test hook — drop every cached row and any leftover in-flight gates."""
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE.clear()
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
            "cooldown_minutes": 0,
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
    """
    if pref is None or not pref.owner_oid:
        return True
    if caller is None:
        return False
    if pref.owner_oid == caller.object_id:
        return True
    return is_dev_bypass_caller(caller)


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
    LOGGER.warning(
        "autostop ownership refusal cluster=%s pref_owner=%s caller=%s",
        pref.cluster_name if pref else "?",
        pref.owner_oid if pref else "?",
        caller.object_id,
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
    payload = body.model_dump()
    payload["owner_oid"] = caller.object_id
    payload["tenant_id"] = caller.tenant_id
    try:
        pref = normalise_preference(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    saved = save_auto_stop_preference(pref)
    _invalidate_status_cache(
        body.subscription_id, body.resource_group, body.cluster_name
    )
    LOGGER.info(
        "autostop upsert cluster=%s enabled=%s idle_minutes=%s by=%s",
        saved.cluster_name,
        saved.enabled,
        saved.idle_minutes,
        caller.object_id,
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
        caller.object_id,
    )
    return _pref_response(
        extended,
        caller=caller,
        subscription_id=body.subscription_id,
        resource_group=body.resource_group,
        cluster_name=body.cluster_name,
    )


@router.get("/autostop/status")
def autostop_status(
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

    Concurrency: a per-cluster ``threading.Event`` collapses concurrent
    cache-miss requests to one underlying compute — without it, N
    browsers polling the same cluster all run ``_compute_status`` in
    parallel and produce a thundering herd on ARM + state-repo.
    Degraded results (state-repo / ARM blip / evaluator failure) are
    NOT cached so the next poll re-attempts immediately.
    """
    _verify_cluster_inputs(subscription_id, resource_group, cluster_name)

    # Ownership check up-front using a cheap pref lookup — this read is
    # cheap (single Table get) and lets us short-circuit before paying
    # for the evaluator + ARM lookup.
    try:
        pref_peek = get_auto_stop_preference(subscription_id, resource_group, cluster_name)
    except Exception as exc:
        LOGGER.debug("autostop_status pref peek failed: %s", type(exc).__name__)
        pref_peek = None
    if pref_peek is not None and not _caller_owns(pref_peek, caller):
        return _disabled_status_shape(subscription_id, resource_group, cluster_name)

    key = _status_cache_key(subscription_id, resource_group, cluster_name)
    now = time.monotonic()
    with _STATUS_CACHE_LOCK:
        cached = _STATUS_CACHE.get(key)
        if cached is not None and (now - cached[0]) < _STATUS_TTL_SECONDS:
            return cached[1]

    # Singleflight: at most one compute per key at a time.
    with _STATUS_INFLIGHT_LOCK:
        gate = _STATUS_INFLIGHT.get(key)
        leader = gate is None
        if leader:
            gate = threading.Event()
            _STATUS_INFLIGHT[key] = gate
    if not leader:
        # Follower: wait for the leader, then read the cache it
        # populated (or fall through to compute if leader failed to
        # cache, e.g. degraded result).
        assert gate is not None
        gate.wait(timeout=_STATUS_TTL_SECONDS)
        with _STATUS_CACHE_LOCK:
            cached = _STATUS_CACHE.get(key)
            if cached is not None:
                return cached[1]
        # Fall through to compute (leader produced a non-cacheable
        # degraded result, or timed out).

    try:
        body = _compute_status(subscription_id, resource_group, cluster_name, caller)
        reason = body.get("reason") or ""
        # Only cache stable, non-degraded answers. Caching a transient
        # "state_repo_unreachable" would freeze the SPA banner on a
        # stale "we don't know" for the full TTL.
        if reason not in _NON_CACHEABLE_REASONS:
            with _STATUS_CACHE_LOCK:
                _STATUS_CACHE[key] = (time.monotonic(), body)
        return body
    finally:
        if leader:
            with _STATUS_INFLIGHT_LOCK:
                _STATUS_INFLIGHT.pop(key, None)
            assert gate is not None
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
) -> dict[str, Any]:
    """Body of `/autostop/status` minus the cache layer."""
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

        decision = evaluate_cluster(
            pref,
            repo=get_state_repo(),
            power_state=power_state,
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

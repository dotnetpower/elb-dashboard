"""Auto warmup reconciliation policy and readiness guards.

Responsibility: Auto warmup reconciliation policy and readiness guards
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `cluster_is_workload_ready`, `expected_warmup_node_count`,
`auto_warmup_ready_gate`, `autowarmup_inflight_key`, `autowarmup_inflight_redis`,
`autowarmup_inflight_acquire`, `autowarmup_inflight_release`,
`autowarmup_wait_elapsed_seconds`, `autowarmup_wait_clear`,
`autowarmup_circuit_state`, `autowarmup_circuit_reset`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

from api.services import monitoring
from api.services.auto_warmup import (
    AutoWarmupPreference,
    list_auto_warmup_preferences,
    mark_auto_warmup_ready_state,
)
from api.services.storage import data as storage_data

LOGGER = logging.getLogger(__name__)

_AUTOWARMUP_INFLIGHT_TTL_SECONDS = 8 * 60
_AUTOWARMUP_INFLIGHT_PREFIX = "autowarmup:inflight:"

# Node-wait bookkeeping: tracks when the gate first started waiting for all
# warmup nodes on a cluster, so the bounded-wait partial fallback (above) can
# measure elapsed time across beat ticks. Redis-backed (ephemeral) — losing it
# on a worker restart only resets the grace timer, which is safe.
_AUTOWARMUP_WAIT_SINCE_PREFIX = "autowarmup:waitsince:"
_AUTOWARMUP_WAIT_SINCE_TTL_SECONDS = 6 * 60 * 60

# Per-(cluster, db) circuit breaker for a warmup that keeps landing in the
# ``Failed`` state. Without this the reconcile force-releases + re-enqueues a
# permanently failing DB every single beat tick (config/network failure that no
# retry can fix), generating endless App Insights failures and churn. After
# ``_CIRCUIT_THRESHOLD`` consecutive Failed observations the circuit opens and
# the DB is skipped for ``_CIRCUIT_COOLDOWN_SECONDS`` before one probe retry.
_AUTOWARMUP_FAIL_PREFIX = "autowarmup:fail:"
_AUTOWARMUP_COOLDOWN_PREFIX = "autowarmup:cooldown:"
_CIRCUIT_THRESHOLD = max(1, int(os.environ.get("AUTOWARMUP_CIRCUIT_THRESHOLD", "5")))
_CIRCUIT_FAILURE_WINDOW_SECONDS = int(
    os.environ.get("AUTOWARMUP_CIRCUIT_FAILURE_WINDOW_SECONDS", "3600")
)
_CIRCUIT_COOLDOWN_SECONDS = int(os.environ.get("AUTOWARMUP_CIRCUIT_COOLDOWN_SECONDS", "1800"))

# Process-local TTL cache for the NCBI latest-dir lookup (see
# ``_latest_ncbi_source_version``).
_LATEST_VERSION_CACHE: dict[str, Any] = {}
_LATEST_VERSION_TTL_SECONDS = int(os.environ.get("AUTOWARMUP_LATEST_VERSION_TTL_SECONDS", "300"))


def cluster_is_workload_ready(cluster: dict[str, Any]) -> bool:
    return (
        cluster.get("provisioning_state") == "Succeeded"
        and cluster.get("power_state") == "Running"
        and int(cluster.get("node_count") or 0) > 0
    )


def expected_warmup_node_count(cluster: dict[str, Any], configured_num_nodes: int = 0) -> int:
    live = 0
    try:
        live = max(0, int(cluster.get("node_count") or 0))
    except (TypeError, ValueError):
        live = 0
    if configured_num_nodes > 0:
        # Cap the configured count by the cluster's live pool count. A stale or
        # oversized ``pref.num_nodes`` (e.g. the pool autoscaled down, or the
        # user edited the node count after the pref was saved) would otherwise
        # make ``ready_node_count >= expected_node_count`` impossible to satisfy
        # and block warmup for that cluster forever. When the live count is
        # unknown (0) we trust the configured value rather than collapse to 0.
        return min(configured_num_nodes, live) if live > 0 else configured_num_nodes
    return live


# Past this many seconds of waiting for *all* expected warmup nodes, the gate
# falls back to warming whatever subset is Ready (>=1 node) so a single node
# that never becomes Ready (quota, spot eviction, ImagePullBackOff, a stuck
# node-image upgrade) cannot block warmup for the whole cluster indefinitely.
_NODE_WAIT_GRACE_SECONDS = int(os.environ.get("AUTOWARMUP_NODE_WAIT_GRACE_SECONDS", "900"))


def auto_warmup_ready_gate(
    credential: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    cluster: dict[str, Any],
    configured_num_nodes: int = 0,
    waited_seconds: float = 0.0,
    grace_seconds: int = _NODE_WAIT_GRACE_SECONDS,
) -> dict[str, Any]:
    expected_node_count = expected_warmup_node_count(cluster, configured_num_nodes)
    if not cluster_is_workload_ready(cluster):
        return {
            "ready": False,
            "phase": "cluster_not_ready",
            "reason": "cluster is not Running/Succeeded yet",
            "expected_node_count": expected_node_count,
            "ready_node_count": 0,
        }
    if expected_node_count <= 0:
        return {
            "ready": True,
            "phase": "ready",
            "expected_node_count": expected_node_count,
            "ready_node_count": 0,
        }
    try:
        from api.services.k8s.monitoring import k8s_ready_warmup_node_names

        ready_nodes = k8s_ready_warmup_node_names(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
        )
    except Exception as exc:
        # Beat reconciler runs every 120 s; a sustained AKS outage would
        # otherwise emit a fresh WARNING per tick. Key by (cluster, exc
        # class) so a new failure class still surfaces; repeats drop to
        # DEBUG.
        from api.services.log_dedup import dedup_log_warning

        dedup_log_warning(
            LOGGER,
            ("auto_warmup_node_readiness", cluster_name, type(exc).__name__),
            "auto warmup node readiness lookup failed cluster=%s expected=%d: %s",
            cluster_name,
            expected_node_count,
            type(exc).__name__,
        )
        return {
            "ready": False,
            "phase": "waiting_for_warmup_nodes",
            "reason": "warmup node readiness is not available yet",
            "expected_node_count": expected_node_count,
            "ready_node_count": 0,
            "error": type(exc).__name__,
        }

    ready_node_count = len(ready_nodes)
    if ready_node_count < expected_node_count:
        # Bounded wait: once we have waited past the grace window and at least
        # one node is Ready, warm the ready subset instead of blocking forever
        # on a node that may never come up. ``expected_node_count`` is narrowed
        # to the ready set so the downstream warmup task (which re-checks
        # ``num_nodes``) does not itself defer the partial warm.
        if ready_node_count >= 1 and waited_seconds >= grace_seconds:
            LOGGER.warning(
                "auto warmup falling back to partial warm cluster=%s ready=%d expected=%d "
                "after waiting %ds",
                cluster_name,
                ready_node_count,
                expected_node_count,
                int(waited_seconds),
            )
            return {
                "ready": True,
                "phase": "ready_partial",
                "reason": "warming ready subset after node wait grace expired",
                "partial": True,
                "expected_node_count": ready_node_count,
                "requested_node_count": expected_node_count,
                "ready_node_count": ready_node_count,
                "ready_nodes": ready_nodes,
            }
        return {
            "ready": False,
            "phase": "waiting_for_warmup_nodes",
            "reason": "waiting for all warmup nodes",
            "expected_node_count": expected_node_count,
            "ready_node_count": ready_node_count,
            "ready_nodes": ready_nodes,
        }
    return {
        "ready": True,
        "phase": "ready",
        "reason": "all warmup nodes are Ready",
        "partial": False,
        "expected_node_count": expected_node_count,
        "ready_node_count": ready_node_count,
        "ready_nodes": ready_nodes,
    }


def autowarmup_inflight_key(
    subscription_id: str, resource_group: str, cluster_name: str, db_name: str
) -> str:
    return (
        f"{_AUTOWARMUP_INFLIGHT_PREFIX}{subscription_id}:{resource_group}:{cluster_name}:{db_name}"
    )


def autowarmup_inflight_redis() -> Any | None:
    try:
        from api.services.redis_clients import get_ops_redis_client

        return get_ops_redis_client(socket_timeout=1.5)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("auto warm inflight redis unavailable: %s", type(exc).__name__)
        return None


def autowarmup_inflight_acquire(
    subscription_id: str, resource_group: str, cluster_name: str, db_name: str
) -> bool:
    """Atomically claim an enqueue slot for one auto-warmup DB."""
    client = autowarmup_inflight_redis()
    if client is None:
        return True
    key = autowarmup_inflight_key(subscription_id, resource_group, cluster_name, db_name)
    try:
        return bool(client.set(key, "1", nx=True, ex=_AUTOWARMUP_INFLIGHT_TTL_SECONDS))
    except Exception as exc:
        LOGGER.debug("auto warm inflight set failed: %s", type(exc).__name__)
        return True


def autowarmup_inflight_release(
    subscription_id: str, resource_group: str, cluster_name: str, db_name: str
) -> None:
    """Release the enqueue slot claimed by ``autowarmup_inflight_acquire``.

    The acquire is a Redis ``SET NX EX`` whose only other release path is the
    TTL. A warmup task that completes, fails, or defers far sooner than the TTL
    must drop the key so the next beat reconcile can re-evaluate the database
    immediately instead of waiting out the full TTL — otherwise a failed warmup
    only retries once per TTL window and the dashboard reports the DB ``Stale``
    far longer than necessary. Best-effort: a missing key or a Redis hiccup is a
    no-op (the TTL is the backstop). The warmup task calls this from its
    ``finally`` when ``release_inflight_on_done`` is set (the auto-warmup path).
    """
    client = autowarmup_inflight_redis()
    if client is None:
        return
    key = autowarmup_inflight_key(subscription_id, resource_group, cluster_name, db_name)
    try:
        client.delete(key)
    except Exception as exc:
        LOGGER.debug("auto warm inflight release failed: %s", type(exc).__name__)


def autowarmup_wait_elapsed_seconds(
    subscription_id: str, resource_group: str, cluster_name: str
) -> float:
    """Return how long the gate has been waiting for all nodes on a cluster.

    Marks ``now`` on the first call (Redis ``SET NX EX``) and returns the
    elapsed seconds since that mark on subsequent calls. Returns ``0.0`` when
    Redis is unavailable (the bounded-wait fallback simply never triggers and we
    keep the strict all-nodes behaviour, which is the safe default).
    """
    client = autowarmup_inflight_redis()
    if client is None:
        return 0.0
    key = f"{_AUTOWARMUP_WAIT_SINCE_PREFIX}{subscription_id}:{resource_group}:{cluster_name}"
    now = int(time.time())
    try:
        client.set(key, str(now), nx=True, ex=_AUTOWARMUP_WAIT_SINCE_TTL_SECONDS)
        raw = client.get(key)
    except Exception as exc:
        LOGGER.debug("auto warm wait-since read failed: %s", type(exc).__name__)
        return 0.0
    if raw is None:
        return 0.0
    try:
        started = int(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, AttributeError):
        return 0.0
    return max(0.0, float(now - started))


def autowarmup_wait_clear(subscription_id: str, resource_group: str, cluster_name: str) -> None:
    """Clear the node-wait timestamp once the cluster is ready (or no longer
    needs warmup), so the next wait window starts fresh."""
    client = autowarmup_inflight_redis()
    if client is None:
        return
    key = f"{_AUTOWARMUP_WAIT_SINCE_PREFIX}{subscription_id}:{resource_group}:{cluster_name}"
    try:
        client.delete(key)
    except Exception as exc:
        LOGGER.debug("auto warm wait-since clear failed: %s", type(exc).__name__)


def autowarmup_circuit_state(
    subscription_id: str, resource_group: str, cluster_name: str, db_name: str
) -> dict[str, Any]:
    """Observe + advance the per-(cluster, db) warmup failure circuit breaker.

    Call once per reconcile tick for a DB whose warm state is ``Failed``. The
    helper increments a windowed failure counter and, once it crosses
    ``_CIRCUIT_THRESHOLD``, opens the circuit for ``_CIRCUIT_COOLDOWN_SECONDS``.
    Returns ``{"open": bool, "failures": int, "cooldown_seconds": int}``.

    When the circuit is open the reconcile must SKIP the (otherwise every-tick)
    force-release + re-enqueue for that DB — a permanently failing warmup
    (missing RBAC, network-blocked Storage, unschedulable pods) cannot be fixed
    by retrying, and hammering it every 120 s only floods telemetry. After the
    cooldown the circuit half-opens (one probe retry); a continued failure
    re-opens it. Degrades to ``open=False`` when Redis is unavailable (current
    every-tick behaviour, no worse than before).
    """
    client = autowarmup_inflight_redis()
    if client is None:
        return {"open": False, "failures": 0, "cooldown_seconds": 0}
    suffix = f"{subscription_id}:{resource_group}:{cluster_name}:{db_name}"
    fail_key = f"{_AUTOWARMUP_FAIL_PREFIX}{suffix}"
    cooldown_key = f"{_AUTOWARMUP_COOLDOWN_PREFIX}{suffix}"
    try:
        if client.get(cooldown_key) is not None:
            return {"open": True, "failures": _CIRCUIT_THRESHOLD, "cooldown_seconds": 0}
        failures = int(client.incr(fail_key))
        if failures == 1:
            client.expire(fail_key, _CIRCUIT_FAILURE_WINDOW_SECONDS)
        if failures >= _CIRCUIT_THRESHOLD:
            # Open the circuit and reset the counter so the post-cooldown probe
            # starts from a clean window.
            client.set(cooldown_key, str(int(time.time())), ex=_CIRCUIT_COOLDOWN_SECONDS)
            client.delete(fail_key)
            return {
                "open": True,
                "failures": failures,
                "cooldown_seconds": _CIRCUIT_COOLDOWN_SECONDS,
            }
        return {"open": False, "failures": failures, "cooldown_seconds": 0}
    except Exception as exc:
        LOGGER.debug("auto warm circuit state failed: %s", type(exc).__name__)
        return {"open": False, "failures": 0, "cooldown_seconds": 0}


def autowarmup_circuit_reset(
    subscription_id: str, resource_group: str, cluster_name: str, db_name: str
) -> None:
    """Clear the failure counter + cooldown for a DB that is no longer Failed
    (warm succeeded, generation changed, or the DB was removed)."""
    client = autowarmup_inflight_redis()
    if client is None:
        return
    suffix = f"{subscription_id}:{resource_group}:{cluster_name}:{db_name}"
    try:
        client.delete(f"{_AUTOWARMUP_FAIL_PREFIX}{suffix}")
        client.delete(f"{_AUTOWARMUP_COOLDOWN_PREFIX}{suffix}")
    except Exception as exc:
        LOGGER.debug("auto warm circuit reset failed: %s", type(exc).__name__)


def warmup_status_by_db(databases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in databases:
        name = str(item.get("name") or "")
        if name:
            out[name] = item
    return out


def _latest_ncbi_source_version() -> str:
    # Short process-local TTL cache: the reconcile calls this once per
    # configured preference, and a slow NCBI ``latest-dir`` lookup would
    # otherwise sit on the beat-tick critical path N times per run (issue #20).
    # The latest-dir changes at most daily, so a 5-minute cache is safe and
    # collapses N per-tick HTTP calls into one.
    now = time.monotonic()
    cached = _LATEST_VERSION_CACHE.get("value")
    cached_at = _LATEST_VERSION_CACHE.get("at", 0.0)
    if cached is not None and (now - float(cached_at)) < _LATEST_VERSION_TTL_SECONDS:
        return str(cached)
    try:
        from api.routes.storage.common import _resolve_latest_dir

        value = _resolve_latest_dir()
        _LATEST_VERSION_CACHE["value"] = value
        _LATEST_VERSION_CACHE["at"] = now
        return value
    except Exception as exc:
        LOGGER.warning("auto warm NCBI latest-dir lookup failed: %s", type(exc).__name__)
        return ""


InflightAcquire = Callable[[str, str, str, str], bool]
SendTask = Callable[..., Any]


def _seed_auto_warmup_job_state(
    *,
    job_id: str,
    pref: AutoWarmupPreference,
    db_name: str,
    machine_type: str,
    num_nodes: int,
    program: str,
    expected_node_count: int,
    force_rewarm: bool = False,
    require_all_warmup_nodes: bool = True,
    admission_token: str = "",
) -> bool:
    """Create the `JobState` row for an auto-warmup task before enqueue.

    The `warmup_database` task writes phase checkpoints via
    `state_repo.update()`, which is `get_entity` + patch under the hood.
    Without a pre-seeded row the first two checkpoints raise
    `ResourceNotFoundError` (caught as `KeyError`) and surface as red 404
    Dependency failures in App Insights, while the SPA loses progress
    visibility for the auto-warmup job. Returns True when the row exists
    (created or already present); False on unexpected create failure so
    the caller can still enqueue rather than block reconciliation.
    """

    from datetime import UTC, datetime

    from api.services.state.job_state import JobState
    from api.services.state_repo import get_state_repo

    now = datetime.now(UTC).isoformat(timespec="seconds")
    payload = {
        "subscription_id": pref.subscription_id,
        "resource_group": pref.resource_group,
        "storage_account": pref.storage_account,
        "database_name": db_name,
        "db": db_name,
        "cluster_name": pref.cluster_name,
        "machine_type": machine_type,
        "num_nodes": num_nodes,
        "acr_resource_group": pref.acr_resource_group,
        "acr_name": pref.acr_name,
        "program": program,
        "caller_oid": pref.owner_oid,
        "require_all_warmup_nodes": require_all_warmup_nodes,
        "auto_warmup": True,
        "expected_node_count": expected_node_count,
        "force_rewarm": force_rewarm,
        "execution_admission_token": admission_token,
    }
    state = JobState(
        job_id=job_id,
        type="warmup",
        status="queued",
        phase="queued",
        owner_oid=pref.owner_oid or None,
        tenant_id=None,
        created_at=now,
        updated_at=now,
        payload=payload,
        db=db_name,
        program=program,
        subscription_id=pref.subscription_id,
        resource_group=pref.resource_group,
        cluster_name=pref.cluster_name,
        storage_account=pref.storage_account,
    )
    try:
        get_state_repo().create(state)
        return True
    except Exception as exc:
        LOGGER.warning(
            "auto warm JobState seed failed job_id=%s db=%s: %s",
            job_id,
            db_name,
            type(exc).__name__,
        )
        return False


def _attach_auto_warmup_task_id(*, job_id: str, task_id: str) -> None:
    """Best-effort attach of the Celery `task_id` to a freshly enqueued auto-warmup job."""

    if not task_id:
        return
    try:
        from api.services.state_repo import get_state_repo

        get_state_repo().update(job_id, task_id=task_id)
    except Exception as exc:
        LOGGER.warning(
            "auto warm task_id attach failed job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )


def _mark_auto_warmup_enqueue_failed(*, job_id: str, error_code: str) -> None:
    """Best-effort terminalise a seeded row whose task was never enqueued."""
    try:
        from api.services.state_repo import get_state_repo

        get_state_repo().update(
            job_id,
            status="failed",
            phase="enqueue_failed",
            error_code=error_code,
        )
    except Exception as exc:
        LOGGER.warning(
            "auto warm enqueue failure marker failed job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )


def reconcile_auto_warmup_preferences(
    *,
    credential: Any,
    send_task: SendTask,
    preference: dict[str, Any] | None = None,
    force: bool = False,
    limit: int = 100,
    admission_token: str = "",
    inflight_acquire: InflightAcquire = autowarmup_inflight_acquire,
) -> dict[str, Any]:
    """Reconcile Auto warm preferences and enqueue strict warmup tasks."""

    if preference is not None:
        prefs = [AutoWarmupPreference.from_dict(preference)]
    else:
        try:
            prefs = list_auto_warmup_preferences(limit=max(1, min(int(limit or 100), 500)))
        except Exception as exc:
            LOGGER.warning("auto warm preferences list failed: %s", type(exc).__name__)
            return {"status": "list_failed", "error": type(exc).__name__, "reconciled": []}

    reconciled: list[dict[str, Any]] = []
    # Per-tick (sub, rg) memoisation of the ARM cluster list. The beat
    # reconcile can hold several preferences in the same resource group, and
    # `list_aks_clusters` is one ARM `managedClusters.list` round trip each.
    # Reusing the list within a single tick removes the duplicate ARM reads
    # (an App Insights hunt showed ~3.3k managedClusters calls / 4h) without
    # any staleness risk — every preference in one tick sees the same instant
    # snapshot, which is exactly what an un-memoised loop would also see modulo
    # ARM-side caching. Scoped to this call so it never outlives the tick.
    _cluster_list_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def _clusters_for(subscription_id: str, resource_group: str) -> list[dict[str, Any]]:
        cache_key = (subscription_id, resource_group)
        cached = _cluster_list_cache.get(cache_key)
        if cached is not None:
            return cached
        fetched = monitoring.list_aks_clusters(credential, subscription_id, resource_group)
        _cluster_list_cache[cache_key] = fetched
        return fetched

    for pref in prefs:
        result: dict[str, Any] = {
            "cluster_name": pref.cluster_name,
            "databases": pref.databases,
            "enqueued": [],
            "skipped": [],
        }
        try:
            if not pref.enabled or not pref.databases:
                result["status"] = "disabled"
                reconciled.append(result)
                continue
            if not pref.subscription_id or not pref.resource_group or not pref.cluster_name:
                result["status"] = "invalid"
                reconciled.append(result)
                continue

            # A one-shot ``force=True`` reconcile enqueued right after
            # ``begin_start`` fires before the blastpool nodes register Ready,
            # so it is dropped at the readiness gate below and the forced
            # re-warm intent is lost. ``start_aks`` therefore also persists
            # ``force_rewarm_pending`` on the preference; honour it here so the
            # recurring beat reconcile keeps forcing the re-warm across ticks
            # until the cluster is workload-ready, then clears it once the
            # warmup is actually enqueued (see ``clear_force_pending`` below).
            effective_force = bool(force) or bool(pref.force_rewarm_pending)
            barrier_token = admission_token.strip()
            if not barrier_token:
                try:
                    from api.services.aks.execution_admission import (
                        get_lifecycle_barrier,
                    )

                    barrier = get_lifecycle_barrier(
                        pref.subscription_id, pref.resource_group, pref.cluster_name
                    )
                    if barrier is not None and barrier.action in {"start", "scale"}:
                        barrier_token = barrier.token
                except Exception:
                    LOGGER.debug(
                        "execution admission barrier lookup skipped cluster=%s",
                        pref.cluster_name,
                        exc_info=True,
                    )

            clusters = _clusters_for(pref.subscription_id, pref.resource_group)
            cluster = next(
                (item for item in clusters if item.get("name") == pref.cluster_name), None
            )
            # Bounded node-wait: measure how long the gate has been waiting for
            # all nodes on this cluster so it can fall back to a partial warm of
            # the ready subset once the grace window expires (issue #3/#5).
            waited_seconds = autowarmup_wait_elapsed_seconds(
                pref.subscription_id, pref.resource_group, pref.cluster_name
            )
            ready_gate = auto_warmup_ready_gate(
                credential,
                subscription_id=pref.subscription_id,
                resource_group=pref.resource_group,
                cluster_name=pref.cluster_name,
                cluster=cluster or {},
                configured_num_nodes=pref.num_nodes,
                waited_seconds=waited_seconds,
            )
            if not ready_gate["ready"]:
                # Only the active node-wait should accumulate elapsed time. A
                # cluster that is Stopped/not-Running resets the timer so the
                # grace window measures node readiness, not power-off time.
                if ready_gate.get("phase") != "waiting_for_warmup_nodes":
                    autowarmup_wait_clear(
                        pref.subscription_id, pref.resource_group, pref.cluster_name
                    )
                mark_auto_warmup_ready_state(pref, ready=False)
                result.update({k: v for k, v in ready_gate.items() if k != "ready"})
                if ready_gate.get("phase") == "waiting_for_warmup_nodes":
                    LOGGER.info(
                        "auto warmup waiting for all warmup nodes cluster=%s expected=%s ready=%s",
                        pref.cluster_name,
                        ready_gate.get("expected_node_count"),
                        ready_gate.get("ready_node_count"),
                    )
                    result["status"] = "waiting_for_warmup_nodes"
                    result["skipped"].append(
                        {
                            "reason": "waiting_for_all_warmup_nodes",
                            "expected_node_count": ready_gate.get("expected_node_count", 0),
                            "ready_node_count": ready_gate.get("ready_node_count", 0),
                        }
                    )
                else:
                    result["status"] = "not_ready"
                reconciled.append(result)
                continue

            # Gate satisfied (full or partial). Clear the node-wait timer so a
            # later stop/start measures a fresh grace window.
            autowarmup_wait_clear(pref.subscription_id, pref.resource_group, pref.cluster_name)
            partial_warm = bool(ready_gate.get("partial"))
            if partial_warm:
                result["partial_warm"] = True

            warm_status = warmup_status_by_db(
                monitoring.k8s_warmup_status(
                    credential,
                    pref.subscription_id,
                    pref.resource_group,
                    pref.cluster_name,
                ).get("databases", [])
            )
            try:
                downloaded_by_name = {
                    str(item.get("name")): item
                    for item in storage_data.list_databases(credential, pref.storage_account)
                    if item.get("name")
                }
            except Exception as exc:
                LOGGER.warning("auto warm database listing failed: %s", type(exc).__name__)
                downloaded_by_name = {db_name: {"name": db_name} for db_name in pref.databases}
            latest_source_version = (
                _latest_ncbi_source_version()
                if any(item.get("source_version") for item in downloaded_by_name.values())
                else ""
            )

            for db_name in pref.databases:
                db_meta = downloaded_by_name.get(db_name)
                if db_meta is None:
                    result["skipped"].append({"db": db_name, "reason": "not_downloaded"})
                    continue
                db_source_version = str(db_meta.get("source_version") or "")
                if (
                    latest_source_version
                    and db_source_version
                    and db_source_version != latest_source_version
                ):
                    # An NCBI snapshot newer than the downloaded generation
                    # exists. Auto-warmup intentionally does NOT auto-download a
                    # new generation — that is an explicit, user-initiated
                    # prepare-db update (it can move hundreds of GB). Record the
                    # drift as an informational signal, then FALL THROUGH so the
                    # warm-state logic below still (re)warms the CURRENTLY
                    # downloaded generation. Skipping outright used to strand the
                    # DB cold/Stale forever every time NCBI rolled a daily
                    # snapshot: node invalidation (stop/start, node rotation)
                    # flips the warm Jobs Stale, but the early skip meant the
                    # reconcile never re-warmed them. Warming the current
                    # generation keeps searches working; the operator updates to
                    # the new generation on their own schedule.
                    result.setdefault("update_available", []).append(
                        {
                            "db": db_name,
                            "source_version": db_source_version,
                            "latest_version": latest_source_version,
                        }
                    )
                warm_meta = warm_status.get(db_name) or {}
                warm_generation = str(warm_meta.get("source_version") or "")
                warm_generations = {
                    str(item) for item in warm_meta.get("source_versions", []) or [] if str(item)
                }
                if db_source_version and warm_generation != db_source_version:
                    warm_meta = {}
                elif len(warm_generations) > 1 or str(warm_meta.get("status") or "") == "Stale":
                    warm_meta = {}
                warm_state = str(warm_meta.get("status") or "")
                # A lingering "Ready"/"Loading" warmup Job normally means the DB
                # is already warm and is skipped. After an `az aks stop`/`start`
                # the node RAM page cache is always cold, yet on a `node_disk`
                # cluster the Managed OS disk keeps VMSS instance names stable,
                # so the pre-stop Jobs are NOT flagged Stale and the DB still
                # reports "Ready". `start_aks` enqueues this reconcile with
                # `force=True` precisely to re-warm in that case, so a forced
                # pass must not skip — it re-enqueues with `force_rewarm` so the
                # warmup task replaces the stale Jobs (download is skipped on
                # node_disk, only the vmtouch re-runs).
                #
                # `force_rewarm` must fire whenever same-generation warmup Jobs
                # are still present (`warm_meta` truthy after the generation /
                # Stale resets above), because `k8s_ensure_job_manifests` skips
                # any Job name that already exists. There are two triggers:
                #   * `force` — the post stop/start re-warm of a still-"Ready"
                #     (or "Loading") DB on node_disk.
                #   * `warm_state == "Failed"` — a prior warmup left Failed Jobs
                #     pinned to LIVE nodes. On node_disk their names are stable,
                #     so the node-staleness sweep keeps them and ensure would
                #     skip recreating forever (the DB stays "Failed" and the
                #     reconcile busy-loops). Force-releasing clears them so the
                #     retry actually re-runs. (Ephemeral self-heals via node
                #     rotation; node_disk needs this explicit release.)
                forced_rewarm = bool(warm_meta) and (effective_force or warm_state == "Failed")
                if warm_state in {"Ready", "Loading"} and not effective_force:
                    # The DB is warm (or warming) with a matching generation —
                    # a healthy observation clears any previous failure circuit.
                    autowarmup_circuit_reset(
                        pref.subscription_id,
                        pref.resource_group,
                        pref.cluster_name,
                        db_name,
                    )
                    result["skipped"].append({"db": db_name, "reason": warm_state})
                    continue
                # Circuit breaker (issue #8): a warmup that keeps landing in the
                # ``Failed`` state cannot be fixed by retrying (missing RBAC,
                # network-blocked Storage, unschedulable pods). Without this the
                # reconcile force-releases + re-enqueues it every single beat
                # tick, flooding telemetry. After N consecutive Failed
                # observations open the circuit and skip the DB for a cooldown.
                if warm_state == "Failed":
                    circuit = autowarmup_circuit_state(
                        pref.subscription_id,
                        pref.resource_group,
                        pref.cluster_name,
                        db_name,
                    )
                    if circuit["open"]:
                        LOGGER.warning(
                            "auto warmup circuit OPEN cluster=%s db=%s failures=%s; "
                            "skipping re-enqueue for cooldown",
                            pref.cluster_name,
                            db_name,
                            circuit.get("failures"),
                        )
                        result["skipped"].append(
                            {
                                "db": db_name,
                                "reason": "circuit_open",
                                "failures": circuit.get("failures", 0),
                                "cooldown_seconds": circuit.get("cooldown_seconds", 0),
                            }
                        )
                        continue
                else:
                    # Any non-Failed warm state for a DB we are about to (re)warm
                    # means the prior failure streak is broken — reset the
                    # circuit so a future failure starts a fresh window.
                    autowarmup_circuit_reset(
                        pref.subscription_id,
                        pref.resource_group,
                        pref.cluster_name,
                        db_name,
                    )
                if not inflight_acquire(
                    pref.subscription_id,
                    pref.resource_group,
                    pref.cluster_name,
                    db_name,
                ):
                    result["skipped"].append({"db": db_name, "reason": "inflight"})
                    continue
                job_id = (
                    f"auto-warmup-{pref.cluster_name}-{db_name}-"
                    f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
                )
                machine_type = pref.machine_type or str((cluster or {}).get("node_sku") or "")
                num_nodes = int(ready_gate.get("expected_node_count") or 0)
                program = pref.programs.get(db_name, "blastn")
                seeded = _seed_auto_warmup_job_state(
                    job_id=job_id,
                    pref=pref,
                    db_name=db_name,
                    machine_type=machine_type,
                    num_nodes=num_nodes,
                    program=program,
                    expected_node_count=num_nodes,
                    force_rewarm=forced_rewarm,
                    require_all_warmup_nodes=not partial_warm,
                    admission_token=barrier_token,
                )
                if barrier_token and not seeded:
                    autowarmup_inflight_release(
                        pref.subscription_id,
                        pref.resource_group,
                        pref.cluster_name,
                        db_name,
                    )
                    raise RuntimeError("post-lifecycle warmup JobState could not be persisted")
                if barrier_token:
                    # Persist the correlation BEFORE enqueueing the side effect.
                    # A durable-write failure prevents send_task via the outer
                    # exception path, keeping request messages safely queued.
                    from api.services.aks.execution_admission import (
                        record_barrier_warmup_jobs,
                    )

                    correlated = record_barrier_warmup_jobs(
                        token=barrier_token,
                        subscription_id=pref.subscription_id,
                        resource_group=pref.resource_group,
                        cluster_name=pref.cluster_name,
                        jobs={db_name: job_id},
                    )
                    if not correlated:
                        _mark_auto_warmup_enqueue_failed(
                            job_id=job_id,
                            error_code="execution_admission_superseded",
                        )
                        autowarmup_inflight_release(
                            pref.subscription_id,
                            pref.resource_group,
                            pref.cluster_name,
                            db_name,
                        )
                        raise RuntimeError(
                            "post-lifecycle warmup barrier was superseded before enqueue"
                        )
                try:
                    task = send_task(
                        "api.tasks.storage.warmup_database",
                        kwargs={
                            "job_id": job_id,
                            "subscription_id": pref.subscription_id,
                            "resource_group": pref.resource_group,
                            "storage_account": pref.storage_account,
                            # The storage account may live in a different RG
                            # than the AKS cluster. Forwarding the preference's
                            # storage_resource_group is required so the warmup
                            # task's RBAC ensure (ARM lookup) targets the right
                            # RG. Omitting it falls back to the cluster RG and
                            # produces a ResourceNotFound that silently skips
                            # the role assignment.
                            "storage_resource_group": pref.storage_resource_group,
                            "database_name": db_name,
                            "cluster_name": pref.cluster_name,
                            "machine_type": machine_type,
                            "num_nodes": num_nodes,
                            "acr_resource_group": pref.acr_resource_group,
                            "acr_name": pref.acr_name,
                            "program": program,
                            "caller_oid": pref.owner_oid,
                            # Strict (all-nodes) warmup by default. After the gate's
                            # bounded-wait fallback fires (``partial_warm``) we warm
                            # whatever subset is Ready, so the task must NOT defer on
                            # a missing node — pass False to let it warm the subset.
                            "require_all_warmup_nodes": not partial_warm,
                            # When a forced (post stop/start) reconcile re-enqueues a
                            # DB that still reports "Ready", the warmup task must drop
                            # the pre-stop Jobs before it recreates them — otherwise
                            # `k8s_ensure_job_manifests` sees the existing names and
                            # no-ops, leaving the RAM cache cold on node_disk.
                            "force_rewarm": forced_rewarm,
                            # The reconcile claimed the in-flight slot via
                            # ``inflight_acquire`` (Redis SET NX EX). Tell the task
                            # to drop that key in its ``finally`` so a deferred or
                            # failed warmup is retried on the next beat tick instead
                            # of waiting out the full TTL.
                            "release_inflight_on_done": True,
                        },
                        queue="storage",
                    )
                except Exception:
                    if barrier_token:
                        from api.services.aks.execution_admission import (
                            clear_barrier_warmup_job,
                        )

                        clear_barrier_warmup_job(
                            token=barrier_token,
                            subscription_id=pref.subscription_id,
                            resource_group=pref.resource_group,
                            cluster_name=pref.cluster_name,
                            database=db_name,
                        )
                    _mark_auto_warmup_enqueue_failed(
                        job_id=job_id,
                        error_code="warmup_enqueue_failed",
                    )
                    autowarmup_inflight_release(
                        pref.subscription_id,
                        pref.resource_group,
                        pref.cluster_name,
                        db_name,
                    )
                    raise
                _attach_auto_warmup_task_id(job_id=job_id, task_id=getattr(task, "id", ""))
                result["enqueued"].append({"db": db_name, "task_id": task.id})

            mark_auto_warmup_ready_state(
                pref,
                ready=True,
                triggered=bool(result["enqueued"]),
                # Only drop the forced re-warm intent once at least one warmup
                # was actually enqueued. If every DB was skipped this tick (a
                # prior warmup still in-flight, or the DB not yet downloaded),
                # keep the flag so the next tick retries the force instead of
                # silently losing it during the skip window.
                clear_force_pending=bool(result["enqueued"]),
            )
            result["status"] = "triggered" if result["enqueued"] else "ready_noop"
        except Exception as exc:
            LOGGER.warning(
                "auto warm reconcile failed cluster=%s: %s",
                pref.cluster_name,
                type(exc).__name__,
            )
            result["status"] = "failed"
            # Sanitise + cap: the reconcile result is surfaced to the SPA and
            # logs; a raw ARM/Kubernetes exception string can carry subscription
            # IDs, resource paths, or SAS-like tokens.
            from api.services.sanitise import sanitise

            result["error"] = sanitise(str(exc))[:300]
        reconciled.append(result)

    return {"status": "completed", "clusters": reconciled}

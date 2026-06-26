"""Periodic AKS idle auto-stop driver.

Responsibility: Walk every persisted `AutoStopPreference`, ask the
    evaluator whether each cluster is idle, and (when ``verdict == "stop"``)
    enqueue `stop_aks` after a second in-task evaluation closes the race
    window between "decide" and "act". Records the outcome of each tick
    (stopped / skipped + reason) on the preference for the SPA's
    "last evaluation" line.
Edit boundaries: Driver only — no SDK calls, no decision logic. The
    "should we stop?" gate lives in `auto_stop_evaluator`. The actual
    stop ARM call lives in `api.tasks.azure.stop_aks`.
Key entry points: `evaluate_idle_clusters` (Celery beat task
    ``api.tasks.azure.evaluate_idle_clusters``),
    `auto_stop_aks` (per-cluster Celery task
    ``api.tasks.azure.auto_stop_aks``).
Risky contracts: Task names are referenced by `api/celery_app.py` beat
    schedule and SPA audit filters. Renaming requires coordinated edits.
Validation: `uv run pytest -q api/tests/test_auto_stop_task.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from api.services.auto_stop import (
    AutoStopPreference,
    get_auto_stop_preference,
    list_auto_stop_preferences,
    mark_auto_stop_event,
    mark_auto_stop_live_activity,
)
from api.services.auto_stop_evaluator import evaluate_cluster
from api.services.auto_stop_live import probe_live_blast_activity
from api.services.feature_events import record_feature_event

LOGGER = logging.getLogger(__name__)


def _live_blast_signal(
    pref: AutoStopPreference, power_state: str
) -> tuple[int | None, Any]:
    """Best-effort live K8s ``app=blast`` activity for the evaluator.

    Returns ``(live_active_jobs, live_latest_activity)`` ready to splat into
    `evaluate_cluster`. Only probes when ARM reports ``power_state ==
    "Running"`` (a stopped cluster has no API server to query) and always
    degrades to ``(None, None)`` on any failure so an unreachable K8s API
    can never strand a cluster running forever — the live signal is additive
    protection only.

    Side effect: when the probe reports a fresh ``live_latest_activity``, the
    high-water mark is persisted via ``mark_auto_stop_live_activity`` (durable,
    monotonic, advance-only). This is what keeps the idle deadline from
    regressing on the *next* tick when the probe goes blind (K8s blip / the
    finished run's Job/Pods garbage-collected) and stops the cluster earlier
    than the SPA countdown last showed. The persist is best-effort — a write
    failure never fails the tick.
    """
    if power_state != "Running":
        return None, None
    probe = probe_live_blast_activity(pref)
    if probe is None:
        return None, None
    live_active_jobs, live_latest_activity = probe
    if live_latest_activity is not None:
        try:
            mark_auto_stop_live_activity(
                pref.subscription_id,
                pref.resource_group,
                pref.cluster_name,
                live_latest_activity,
                known=pref,
            )
        except Exception as exc:  # best-effort bookkeeping — never fail the tick
            LOGGER.debug(
                "auto_stop live anchor persist failed cluster=%s: %s",
                pref.cluster_name,
                exc,
            )
    return live_active_jobs, live_latest_activity


def _sb_pending_signal(power_state: str) -> int | None:
    """Best-effort Service Bus request-queue depth for the evaluator.

    Returns the active (deliverable) message count, or ``None`` when the
    signal is unavailable/disabled so the evaluator degrades to the
    job-count decision. Only meaningful for a ``Running`` cluster (a stopped
    cluster is already kept by the power-state gate, and auto-START on queue
    arrival is intentionally out of scope here -- this only prevents a stop
    while work waits). Gated by ``AKS_AUTOSTOP_RESPECT_SB_QUEUE`` (default
    on) so the behaviour can be disabled without a redeploy. Never raises --
    any failure degrades to ``None``.

    Delegates to the shared :mod:`api.services.auto_stop_sb_signal` gate with
    ``ttl_seconds=0`` (cache bypassed): the beat tick is a low-frequency
    caller and the act-path stop decision must read the live queue, so the
    short status-poll cache (used by the status route) is intentionally not
    applied here.
    """
    from api.services.auto_stop_sb_signal import pending_queue_signal

    return pending_queue_signal(power_state, ttl_seconds=0.0)


def _power_state(pref: AutoStopPreference) -> str:
    """Best-effort ARM ``power_state`` lookup for a single cluster.

    Failure is non-fatal — the evaluator treats `""` as "unknown" and
    still applies the rest of the guards. This keeps the auto-stop beat
    healthy when ARM is rate-limited.

    Used by ``auto_stop_aks`` (per-cluster path). The beat fan-out
    path uses ``_batch_power_states`` instead so 100 clusters in the
    same RG share one ARM list call.
    """
    try:
        from api.services import get_credential
        from api.services.cluster_health import get_cluster_health

        health = get_cluster_health(
            get_credential(),
            pref.subscription_id,
            pref.resource_group,
            pref.cluster_name,
        )
        return health.get("power_state") or ""
    except Exception as exc:
        LOGGER.debug("auto_stop power_state lookup failed for %s: %s", pref.cluster_name, exc)
        return ""


def _provisioning_state(pref: AutoStopPreference) -> str:
    """Best-effort ARM ``provisioning_state`` lookup for a single cluster.

    Used by ``auto_stop_aks`` so the evaluator can refuse to stop a
    cluster whose start LRO is still in progress (AKS reports
    ``power_state == "Running"`` the instant a start begins while
    ``provisioning_state`` stays ``Starting``). Failure is non-fatal —
    ``""`` lets the evaluator degrade open. Shares the 90s
    ``cluster_health`` cache with ``_power_state`` so this adds no extra
    ARM round-trip in the common path.
    """
    try:
        from api.services import get_credential
        from api.services.cluster_health import get_cluster_health

        health = get_cluster_health(
            get_credential(),
            pref.subscription_id,
            pref.resource_group,
            pref.cluster_name,
        )
        return health.get("provisioning_state") or ""
    except Exception as exc:
        LOGGER.debug(
            "auto_stop provisioning_state lookup failed for %s: %s", pref.cluster_name, exc
        )
        return ""


def _batch_power_states(
    prefs: list[AutoStopPreference],
) -> tuple[dict[tuple[str, str, str], str], dict[str, Any]]:
    """Resolve power_state for every pref using one ARM `list_by_resource_group`
    per ``(subscription_id, resource_group)`` group.

    The per-cluster ``_power_state`` helper makes one ARM ``managed_clusters.get``
    call per cluster; that's fine for ad-hoc calls but the beat fan-out
    used to issue N calls per tick (N clusters in 1 RG = N calls every
    5 min). With the batch path 1 RG = 1 call regardless of cluster
    count.

    Returns ``(power_state_map, batch_summary)`` where ``batch_summary``
    surfaces RG-level failures so the beat task can count them toward
    its visible ``errors`` total (critique #15: a silent ARM RBAC failure
    means the auto-stop never fires for the whole RG, but the previous
    debug-level log left no breadcrumb).
    """
    out: dict[tuple[str, str, str], str] = {}
    batch_summary: dict[str, Any] = {
        "rg_groups": 0,
        "rg_failed": 0,
        "failed_rgs": [],
    }
    if not prefs:
        return out, batch_summary
    # Group by (sub, rg)
    grouped: dict[tuple[str, str], list[str]] = {}
    for pref in prefs:
        grouped.setdefault(
            (pref.subscription_id, pref.resource_group), []
        ).append(pref.cluster_name)
    batch_summary["rg_groups"] = len(grouped)
    for (sub, rg), names in grouped.items():
        try:
            # Critique #9.3: import + credential acquisition must be
            # inside the try so a transient token refresh failure (or a
            # missing managed identity in dev) does not abort the whole
            # beat tick — only that one RG group is skipped.
            from api.services import get_credential
            from api.services.azure_clients import aks_client

            cred = get_credential()
            client = aks_client(cred, sub)
            for cluster in client.managed_clusters.list_by_resource_group(rg):
                name = getattr(cluster, "name", "") or ""
                if not name or name not in names:
                    continue
                ps = ""
                state = getattr(cluster, "power_state", None)
                if state is not None:
                    ps = getattr(state, "code", "") or ""
                out[(sub, rg, name)] = ps
        except Exception as exc:
            # Critique #15: an RG-wide ARM failure means auto-stop is
            # silently broken for that group until ARM recovers. Log at
            # WARNING (not DEBUG) and surface in the beat task summary
            # so the operator notices in App Insights / the audit log.
            LOGGER.warning(
                "auto_stop batch power_state list failed sub=%s rg=%s: %s (%s)",
                sub,
                rg,
                type(exc).__name__,
                exc,
            )
            batch_summary["rg_failed"] += 1
            # Cap the surfaced list at 20 to bound the summary size.
            if len(batch_summary["failed_rgs"]) < 20:
                batch_summary["failed_rgs"].append(f"{sub}:{rg}")
            continue
    return out, batch_summary


@shared_task(
    name="api.tasks.azure.auto_stop_aks",
    bind=True,
    max_retries=0,
)
def auto_stop_aks(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Re-verify the idle decision then call `stop_aks`.

    The beat driver already evaluated the cluster, but state can change
    between "decide" and "act" — a user might submit a BLAST job during
    the Celery queueing window. Re-running `evaluate_cluster` here is
    the second of two checks demanded by the design (closes the
    decide-vs-act race window).

    Retry policy: this task does NOT autoretry. The inner ``stop_aks``
    already has ``autoretry_for=(Exception,) max_retries=3``; an outer
    retry would multiply that into up to 9 ARM stop attempts on a
    cluster that may already be transitioning. If ``stop_aks`` itself
    exhausts its retries, the next beat tick re-evaluates and decides
    fresh — that is the right cadence for a cost-saver.
    """
    from api.services.state_repo import get_state_repo
    from api.tasks.azure import stop_aks

    pref = get_auto_stop_preference(subscription_id, resource_group, cluster_name)
    if pref is None or not pref.enabled:
        LOGGER.info(
            "auto_stop_aks abort cluster=%s reason=preference_missing_or_disabled",
            cluster_name,
        )
        return {
            "cluster_name": cluster_name,
            "action": "skip",
            "reason": "preference_missing_or_disabled",
        }

    power_state = _power_state(pref)
    live_active_jobs, live_latest_activity = _live_blast_signal(pref, power_state)
    decision = evaluate_cluster(
        pref,
        repo=get_state_repo(),
        power_state=power_state,
        # Refuse to stop a cluster whose start LRO is still in progress —
        # AKS flips ``power_state`` to ``Running`` before
        # ``provisioning_state`` settles, and stopping mid-start is
        # rejected by ARM with ``OperationNotAllowed`` ("in progress start
        # managed cluster"), which would surface as a Celery task ERROR.
        provisioning_state=_provisioning_state(pref),
        # The beat driver preflight-stamps ``last_stop_at`` before
        # enqueueing this task (its double-enqueue guard). Without
        # ``ignore_cooldown`` the re-evaluation would always see that
        # fresh stamp, return ``keep / cooldown``, and late-skip the
        # stop forever. Cooldown is a beat-decide / SPA concern; the act
        # task re-confirms only the decide-vs-act race gates (enabled,
        # active jobs, extend, power_state).
        ignore_cooldown=True,
        # Re-probe live K8s ``app=blast`` activity here so an OpenAPI run
        # that started during the beat-decide → act queueing window blocks
        # the stop. This closes the decide-vs-act race for OpenAPI jobs the
        # same way the state_repo re-read does for dashboard jobs.
        live_active_jobs=live_active_jobs,
        live_latest_activity=live_latest_activity,
        # Keep the cluster alive while the Service Bus request queue still
        # holds undrained work, closing the same decide-vs-act race for
        # queued-but-not-yet-bridged submissions.
        pending_queue_depth=_sb_pending_signal(power_state),
    )
    if decision.verdict != "stop":
        LOGGER.info(
            "auto_stop_aks late-skip cluster=%s reason=%s",
            cluster_name,
            decision.reason,
        )
        # Clear the beat driver's preflight ``last_stop_at`` stamp: it was
        # written before this task was enqueued (double-enqueue guard), but we
        # are NOT stopping the cluster, so leaving it would falsely trip the
        # cooldown gate and hide the SPA countdown for the whole cooldown
        # window even though the cluster is Running.
        mark_auto_stop_event(
            pref,
            stopped=False,
            reason=f"late_skip:{decision.reason}",
            clear_preflight_stop=True,
        )
        return {
            "cluster_name": cluster_name,
            "action": "skip",
            "reason": decision.reason,
        }

    LOGGER.info(
        "auto_stop_aks invoking stop_aks cluster=%s reason=%s",
        cluster_name,
        decision.reason,
    )
    # We invoke the existing `stop_aks` task body directly (not `.delay`)
    # so the auto-stop task itself becomes the unit of work the dashboard
    # surfaces in audit — there is no point fanning out to another Celery
    # message just to make the same SDK call.
    result = stop_aks.run(  # type: ignore[attr-defined]
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    mark_auto_stop_event(pref, stopped=True, reason=decision.reason)
    record_feature_event(
        "cluster_lifecycle",
        status="completed",
        action="stop",
        actor="system:auto-stop",
        cluster=cluster_name,
        resource_group=resource_group,
        reason=decision.reason,
    )
    LOGGER.info("auto_stop_aks completed cluster=%s reason=%s", cluster_name, decision.reason)
    return {
        "cluster_name": cluster_name,
        "action": "stop",
        "reason": decision.reason,
        "stop_result": result,
    }


@shared_task(
    name="api.tasks.azure.evaluate_idle_clusters",
    bind=True,
    max_retries=0,
)
def evaluate_idle_clusters(self: Any) -> dict[str, Any]:
    """Beat-driven idle scan across every persisted preference.

    Side effects: enqueues `auto_stop_aks` for each cluster the evaluator
    marks as ``stop``. Records a skip note on every preference whose
    evaluator returns ``keep`` for a non-trivial reason (so the SPA can
    show "last evaluation: skipped — active_jobs:2"). Pure no-op when no
    preferences exist.
    """
    from api.services.state_repo import get_state_repo

    summary: dict[str, Any] = {
        "evaluated": 0,
        "queued_stops": 0,
        "queued_starts": 0,
        "kept_running": 0,
        "warnings": 0,
        "errors": 0,
        # Critique #15: surface RG-level batch failures so an operator
        # can spot when auto-stop has been silently broken for a whole
        # resource group (typical cause: ARM RBAC missing for the
        # platform managed identity on that subscription).
        "power_state_rg_groups": 0,
        "power_state_rg_failed": 0,
        "power_state_failed_rgs": [],
    }
    try:
        prefs = list_auto_stop_preferences(limit=500)
    except Exception as exc:
        LOGGER.warning("evaluate_idle_clusters list failed: %s", exc)
        summary["errors"] += 1
        return summary

    repo = get_state_repo()
    enabled_prefs = [p for p in prefs if p.enabled]
    # Resolve all power_state values in one ARM call per (sub, rg) — without
    # batching, a 100-cluster fleet would issue 100 ARM `get` calls per
    # 5-min tick and trip ARM throttling on busy subscriptions.
    power_state_map, batch_summary = _batch_power_states(enabled_prefs)
    summary["power_state_rg_groups"] = batch_summary["rg_groups"]
    summary["power_state_rg_failed"] = batch_summary["rg_failed"]
    summary["power_state_failed_rgs"] = batch_summary["failed_rgs"]
    # An RG-level failure means we lost the power-state signal for
    # every cluster in that RG, so count it toward the visible error
    # total (the evaluator will keep them running, which is safe
    # behaviour but represents lost cost savings).
    summary["errors"] += batch_summary["rg_failed"]
    # Queue-arrival auto-start state (default-OFF). The request queue is a single
    # deployment-wide entity, so its depth is probed at most ONCE per tick and
    # only when a Stopped cluster is actually seen (lazy).
    from api.services.aks.queue_autostart import (
        acquire_autostart_lease,
        queue_autostart_enabled,
        release_autostart_lease,
        should_autostart,
    )

    autostart_on = queue_autostart_enabled()
    autostart_pending: int | None = None
    autostart_probed = False
    for pref in enabled_prefs:
        summary["evaluated"] += 1
        try:
            cluster_power_state = power_state_map.get(
                (pref.subscription_id, pref.resource_group, pref.cluster_name),
                "",
            )
            # Probe live K8s ``app=blast`` activity only for Running
            # candidates (a stopped cluster has no API server). This catches
            # OpenAPI-submitted BLAST runs that never write a dashboard
            # jobstate row; the probe degrades to (None, None) on any K8s
            # failure so it can only ever ADD protection, never force a stop.
            live_active_jobs, live_latest_activity = _live_blast_signal(
                pref, cluster_power_state
            )
            decision = evaluate_cluster(
                pref,
                repo=repo,
                power_state=cluster_power_state,
                live_active_jobs=live_active_jobs,
                live_latest_activity=live_latest_activity,
                pending_queue_depth=_sb_pending_signal(cluster_power_state),
            )
        except Exception as exc:
            LOGGER.warning(
                "evaluate_idle_clusters cluster=%s eval failed: %s", pref.cluster_name, exc
            )
            summary["errors"] += 1
            continue

        if decision.verdict == "stop":
            # Stamp ``last_stop_at`` BEFORE enqueueing so a subsequent
            # beat tick (which can race when a tick takes longer than
            # the beat interval) sees the cooldown gate as already
            # tripped and refuses to enqueue a second stop for the same
            # cluster. The post-stop ``mark_auto_stop_event`` below
            # refreshes the timestamp once the stop actually completes.
            #
            # If the enqueue itself fails (broker down, queue full) we
            # MUST roll the stamp back \u2014 otherwise the preference
            # carries a fake "stopped 30 min ago" mark, the cooldown
            # gate keeps subsequent ticks from re-enqueueing, and the
            # cluster runs idle indefinitely without ever being stopped.
            previous_last_stop_at = pref.last_stop_at
            previous_last_stop_reason = pref.last_stop_reason
            try:
                mark_auto_stop_event(pref, stopped=True, reason=f"enqueued:{decision.reason}")
            except Exception as exc:
                LOGGER.warning(
                    "evaluate_idle_clusters preflight stamp failed cluster=%s: %s",
                    pref.cluster_name,
                    exc,
                )
            try:
                auto_stop_aks.delay(  # type: ignore[attr-defined]
                    subscription_id=pref.subscription_id,
                    resource_group=pref.resource_group,
                    cluster_name=pref.cluster_name,
                )
                summary["queued_stops"] += 1
            except Exception as exc:
                LOGGER.warning(
                    "evaluate_idle_clusters enqueue failed cluster=%s: %s",
                    pref.cluster_name,
                    exc,
                )
                summary["errors"] += 1
                # Roll back the cooldown stamp so the next beat tick
                # re-evaluates this cluster fresh instead of waiting
                # the full cooldown window on a fake stop.
                #
                # Critique #12: re-fetch the latest persisted row before
                # writing back. Using ``AutoStopPreference.from_dict(
                # pref.to_dict())`` from the stale beat-tick snapshot
                # would silently revert a concurrent user toggle (e.g.
                # the user disabled the feature between the beat snapshot
                # and the rollback). Restore ONLY the bookkeeping
                # fields (``last_stop_at`` / ``last_stop_reason``) onto
                # the freshly-read row so user-owned fields are
                # preserved.
                try:
                    from api.services.auto_stop import save_auto_stop_preference

                    fresh = get_auto_stop_preference(
                        pref.subscription_id,
                        pref.resource_group,
                        pref.cluster_name,
                    )
                    if fresh is None:
                        # User deleted the row in the meantime —
                        # nothing to roll back to. Skip.
                        pass
                    else:
                        fresh.last_stop_at = previous_last_stop_at
                        fresh.last_stop_reason = previous_last_stop_reason
                        save_auto_stop_preference(fresh)
                except Exception as rb_exc:
                    LOGGER.warning(
                        "evaluate_idle_clusters rollback failed cluster=%s: %s",
                        pref.cluster_name,
                        rb_exc,
                    )
        elif decision.verdict == "warn":
            summary["warnings"] += 1
            # Only record the transition INTO warn — repeating the same
            # write every 5 min during the warn window spams the Table
            # and the SPA "last evaluation" footer with no new signal.
            if not (pref.last_skip_reason or "").startswith("warn:"):
                mark_auto_stop_event(pref, stopped=False, reason=f"warn:{decision.reason}")
        else:
            summary["kept_running"] += 1
            # Only record interesting skip reasons — leaving "active" out
            # of the audit avoids spamming a row every 5 min for an
            # actively-used cluster. Also avoid re-writing the same
            # reason twice in a row (cooldown / extended / repeat warn
            # are all noisy without it).
            if decision.reason not in {"active", "disabled"} and (
                pref.last_skip_reason or ""
            ) != decision.reason:
                mark_auto_stop_event(pref, stopped=False, reason=decision.reason)

        # Queue-arrival auto-START (default-OFF; the deliberate inverse of idle
        # auto-stop). A cluster the evaluator KEPT because it is Stopped becomes
        # a start candidate when the deployment-wide request queue still holds
        # undrained work. ``start_aks`` is idempotent (a Running/Starting cluster
        # is a no-op) and stamps ``last_started_at`` so the next idle tick grants
        # a full grace, so this can never start-storm. The cooldown lease
        # (fail-closed) is the single-flight guard across overlapping ticks.
        if autostart_on and cluster_power_state == "Stopped":
            if not autostart_probed:
                from api.services.auto_stop_sb_signal import read_request_queue_depth

                autostart_pending = read_request_queue_depth()
                autostart_probed = True
            if should_autostart(cluster_power_state, autostart_pending) and acquire_autostart_lease(
                pref.subscription_id, pref.resource_group, pref.cluster_name
            ):
                try:
                    from api.tasks.azure.lifecycle import start_aks

                    start_aks.delay(  # type: ignore[attr-defined]
                        subscription_id=pref.subscription_id,
                        resource_group=pref.resource_group,
                        cluster_name=pref.cluster_name,
                    )
                    summary["queued_starts"] += 1
                    LOGGER.info(
                        "queue_autostart queued start cluster=%s pending=%s",
                        pref.cluster_name,
                        autostart_pending,
                    )
                except Exception as exc:
                    # Roll back the cooldown lease so the next tick can retry the
                    # start immediately — the reservation was taken on the
                    # assumption the enqueue would succeed, and no start was
                    # actually queued.
                    release_autostart_lease(
                        pref.subscription_id, pref.resource_group, pref.cluster_name
                    )
                    LOGGER.warning(
                        "queue_autostart enqueue failed cluster=%s: %s",
                        pref.cluster_name,
                        exc,
                    )
                    summary["errors"] += 1
    return summary


__all__ = [
    "_batch_power_states",
    "_live_blast_signal",
    "_sb_pending_signal",
    "auto_stop_aks",
    "evaluate_idle_clusters",
]

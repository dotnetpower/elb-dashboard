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
)
from api.services.auto_stop_evaluator import evaluate_cluster

LOGGER = logging.getLogger(__name__)


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


def _batch_power_states(
    prefs: list[AutoStopPreference],
) -> dict[tuple[str, str, str], str]:
    """Resolve power_state for every pref using one ARM `list_by_resource_group`
    per ``(subscription_id, resource_group)`` group.

    The per-cluster ``_power_state`` helper makes one ARM ``managed_clusters.get``
    call per cluster; that's fine for ad-hoc calls but the beat fan-out
    used to issue N calls per tick (N clusters in 1 RG = N calls every
    5 min). With the batch path 1 RG = 1 call regardless of cluster
    count.

    Failures are non-fatal — clusters whose ARM result we could not
    fetch get ``""`` (unknown), and the evaluator's "stop requires
    Running" guard takes it from there.
    """
    out: dict[tuple[str, str, str], str] = {}
    if not prefs:
        return out
    # Group by (sub, rg)
    grouped: dict[tuple[str, str], list[str]] = {}
    for pref in prefs:
        grouped.setdefault(
            (pref.subscription_id, pref.resource_group), []
        ).append(pref.cluster_name)
    try:
        from api.services import get_credential
        from api.services.azure_clients import aks_client
    except Exception as exc:
        LOGGER.warning("auto_stop batch power_state imports failed: %s", exc)
        return out
    cred = get_credential()
    for (sub, rg), names in grouped.items():
        try:
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
            LOGGER.debug(
                "auto_stop batch power_state list failed sub=%s rg=%s: %s",
                sub,
                rg,
                exc,
            )
            continue
    return out


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

    decision = evaluate_cluster(
        pref,
        repo=get_state_repo(),
        power_state=_power_state(pref),
    )
    if decision.verdict != "stop":
        LOGGER.info(
            "auto_stop_aks late-skip cluster=%s reason=%s",
            cluster_name,
            decision.reason,
        )
        mark_auto_stop_event(pref, stopped=False, reason=f"late_skip:{decision.reason}")
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
        "kept_running": 0,
        "warnings": 0,
        "errors": 0,
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
    power_state_map = _batch_power_states(enabled_prefs)
    for pref in enabled_prefs:
        summary["evaluated"] += 1
        try:
            decision = evaluate_cluster(
                pref,
                repo=repo,
                power_state=power_state_map.get(
                    (pref.subscription_id, pref.resource_group, pref.cluster_name),
                    "",
                ),
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
                try:
                    rollback = AutoStopPreference.from_dict(pref.to_dict())
                    rollback.last_stop_at = previous_last_stop_at
                    rollback.last_stop_reason = previous_last_stop_reason
                    from api.services.auto_stop import save_auto_stop_preference

                    save_auto_stop_preference(rollback)
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
    return summary


__all__ = ["auto_stop_aks", "evaluate_idle_clusters"]

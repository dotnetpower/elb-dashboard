"""Idle-detection logic for AKS auto-stop.

Responsibility: Pure decision function — given a `AutoStopPreference`, the
    current time, and an injected `state_repo`, return whether the cluster
    should be stopped, kept running, or whether the SPA should show a
    pre-stop banner.
Edit boundaries: No SDK calls, no audit writes, no Celery. The Celery
    driver (`api/tasks/azure/idle_autostop.py`) calls `evaluate_cluster()`
    twice — once in beat scheduling, once again inside the auto-stop task
    body just before invoking `stop_aks` — to close the race window
    between "decide" and "act".
Key entry points: `evaluate_cluster`, `IdleDecision`, `ACTIVE_JOB_STATUSES`,
    `ACTIVE_JOB_TYPES`.
Risky contracts: `ACTIVE_JOB_STATUSES` mirrors
    `JobStateRepository.list_active` — if that set ever changes, sync
    here so the "no active jobs" gate stays accurate.
Validation: `uv run pytest -q api/tests/test_auto_stop_evaluator.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from api.services.auto_stop import (
    AutoStopPreference,
    is_extended,
    is_in_cooldown,
)

ACTIVE_JOB_STATUSES = frozenset({"queued", "pending", "running", "reducing"})
"""Job statuses that count as "cluster in use" for the auto-stop gate.

Mirrors ``JobStateRepository.list_active``. Keep in sync.
"""

ACTIVE_JOB_TYPES = ("blast", "warmup", "prepare_db", "shard", "oracle")
"""Job ``type`` values that block an auto-stop.

A submitted-but-not-yet-K8s-visible BLAST run is the most common false
negative; covering ``warmup`` / ``prepare_db`` / ``shard`` / ``oracle``
also stops a database admin action from being killed mid-flight.
"""

Verdict = Literal["stop", "keep", "warn"]


class StateRepoProtocol(Protocol):
    """Subset of `JobStateRepository` the evaluator depends on."""

    def list_for_scope(
        self,
        *,
        subscription_id: str = "",
        resource_group: str = "",
        cluster_name: str = "",
        limit: int = 50,
        include_payload: bool = True,
    ) -> list[Any]:
        ...


@dataclass
class IdleDecision:
    """Outcome of one evaluator tick.

    Attributes:
        verdict: ``stop`` → enqueue a stop now;
            ``warn`` → show the SPA countdown banner (≤ half the idle window
            remains);
            ``keep`` → do nothing.
        reason: Human-readable code/phrase. Used in audit + SPA tooltip.
        next_stop_at: ISO 8601 (UTC) deadline at which the cluster
            *would* be stopped if nothing changes. Empty when verdict is
            ``keep`` for a non-idle reason (e.g. extended, disabled).
        seconds_until_stop: Convenience seconds-to-deadline, or 0 when
            ``next_stop_at`` is empty / past.
        active_job_count: How many active jobs were observed (for SPA
            tooltip).
        cluster_power_state: Pass-through of the most recent ARM
            ``power_state``. The evaluator does NOT call ARM itself; the
            driver injects this via `power_state`.
    """

    verdict: Verdict
    reason: str
    next_stop_at: str = ""
    seconds_until_stop: int = 0
    active_job_count: int = 0
    cluster_power_state: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "next_stop_at": self.next_stop_at,
            "seconds_until_stop": self.seconds_until_stop,
            "active_job_count": self.active_job_count,
            "cluster_power_state": self.cluster_power_state,
        }


def _scan_cluster_jobs(
    repo: StateRepoProtocol,
    pref: AutoStopPreference,
    *,
    limit: int = 200,
) -> tuple[int, datetime | None, bool, bool]:
    """Single Table query → (active_count, latest_activity_ts, ok, truncated).

    NOTE on truncation: Azure Tables returns rows in PartitionKey then
    RowKey order, NOT timestamp order. ``limit`` here is the in-process
    cap on rows we examine to compute both counters. When we hit the
    cap we DO NOT know whether the row carrying the true latest
    timestamp lies beyond the window — refusing to stop in that case
    is the safe default. The active counter is still safe (active jobs
    are by definition recent and rarely number > 200).

    Returns:
        active_count: jobs in ACTIVE_JOB_STATUSES whose type ∈ ACTIVE_JOB_TYPES.
        latest_activity_ts: most recent ``updated_at`` / ``created_at`` across
            ALL non-deleted rows in scope (terminal jobs included — they
            seed the idle clock).
        ok: False when the Table query raised — caller must fail safe.
        truncated: True when the scan hit ``limit`` — the latest
            timestamp may be stale. Upstream uses this to refuse stop.
    """
    try:
        rows = list(
            repo.list_for_scope(
                subscription_id=pref.subscription_id,
                resource_group=pref.resource_group,
                cluster_name=pref.cluster_name,
                limit=limit,
                include_payload=False,
            )
        )
    except Exception:
        return 0, None, False, False

    allowed_types = frozenset(ACTIVE_JOB_TYPES)
    active = 0
    latest: datetime | None = None
    for row in rows:
        row_type = (getattr(row, "type", "") or "")
        row_status = (getattr(row, "status", "") or "")
        if row_type in allowed_types and row_status in ACTIVE_JOB_STATUSES:
            active += 1
        for field in ("updated_at", "created_at"):
            raw = getattr(row, field, "") or ""
            if not raw:
                continue
            try:
                text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
                ts = datetime.fromisoformat(text)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                continue
            if latest is None or ts > latest:
                latest = ts
    return active, latest, True, len(rows) >= limit


def _format_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    """Best-effort ISO 8601 → aware UTC datetime, or None when unparseable."""
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def evaluate_cluster(
    pref: AutoStopPreference,
    *,
    repo: StateRepoProtocol,
    now: datetime | None = None,
    power_state: str = "",
    provisioning_state: str = "",
    ignore_cooldown: bool = False,
    live_active_jobs: int | None = None,
    live_latest_activity: datetime | None = None,
) -> IdleDecision:
    """Decide whether an AKS cluster should be auto-stopped.

    Args:
        pref: Persisted user preference. ``enabled=False`` short-circuits
            with ``keep / disabled``.
        repo: State repository used to count active jobs and read recent
            activity timestamps. Injected so unit tests do not touch Azure.
        now: Override "current time" for deterministic tests. Defaults to
            `datetime.now(UTC)`.
        power_state: Latest ARM ``power_state`` (``Running`` / ``Stopped``
            / ``Starting`` / …). The evaluator does not fetch it itself;
            the driver supplies it from `cluster_health.get_cluster_health`.
        provisioning_state: Latest ARM ``provisioning_state`` (``Succeeded``
            / ``Starting`` / ``Stopping`` / ``Updating`` / …). AKS flips
            ``power_state`` to ``Running`` the moment a start LRO begins
            while ``provisioning_state`` stays transitional, so the
            evaluator refuses to stop unless this is ``Succeeded`` (or
            unknown/empty when ARM is unreachable). The driver supplies
            it from `cluster_health.get_cluster_health`.
        ignore_cooldown: When True, skip the cooldown gate. The act task
            (`auto_stop_aks`) MUST set this: the beat driver stamps
            ``last_stop_at`` as a preflight double-enqueue guard *before*
            enqueueing the act task, so the act task's re-evaluation would
            otherwise always trip its own freshly-written cooldown and
            late-skip the very stop it was enqueued to perform (a
            permanent livelock — the cluster never stops). The beat
            ``decide`` pass and the SPA countdown keep cooldown enforced
            (default False) so a real recent stop still blocks a re-stop.
        live_active_jobs: Live ElasticBLAST ``app=blast`` job count observed
            directly on the Kubernetes cluster (via
            `auto_stop_live.probe_live_blast_activity`), or ``None`` when the
            K8s probe could not run. This is the key fix for
            OpenAPI-submitted runs that never write a dashboard jobstate
            row: it is ADDED to the Table-derived active count, so a live
            run keeps the cluster alive even though the state repo is empty.
            ``None`` (the default) means "not probed / unavailable" and is
            ignored — the probe only ever ADDS protection, it can never
            force a stop, so an unreachable K8s API can never strand a
            cluster running forever.
        live_latest_activity: Most recent live ``app=blast`` start/finish
            timestamp, or ``None``. Folded into the idle-clock anchor exactly
            like ``last_started_at`` so a just-finished live burst still gets
            the full ``idle_minutes`` grace before a stop. Never advances the
            deadline beyond a real observed activity time, so it cannot push
            the stop indefinitely.

    Returns:
        `IdleDecision` describing the outcome.
    """
    current = now or datetime.now(UTC)

    if not pref.enabled:
        return IdleDecision(
            verdict="keep",
            reason="disabled",
            cluster_power_state=power_state,
        )

    # Already not-Running clusters are not stop candidates. The
    # ``cluster_health`` gate gives us "" when ARM is unreachable; in that
    # case we degrade open (let the active-job count be the gate) so a
    # transient ARM blip cannot strand the cluster running forever — but
    # we also won't proactively stop it without ARM confirmation. So
    # treat "" as "unknown" and require the rest of the gates to pass.
    if power_state and power_state != "Running":
        return IdleDecision(
            verdict="keep",
            reason=f"power_state:{power_state}",
            cluster_power_state=power_state,
        )

    # AKS reports ``power_state.code == "Running"`` the instant a start LRO
    # begins, while ``provisioning_state`` stays ``Starting`` until the
    # control plane settles (~5 min). Stopping a cluster mid-transition is
    # rejected by ARM with ``OperationNotAllowed`` ("in progress start
    # managed cluster"), surfacing as a Celery task ERROR. Treat any
    # non-steady provisioning state as keep so we never attempt a stop
    # against a transitional cluster. Empty string = unknown (ARM
    # unreachable) → degrade open and let the remaining gates decide.
    if provisioning_state and provisioning_state.strip().lower() != "succeeded":
        return IdleDecision(
            verdict="keep",
            reason=f"provisioning:{provisioning_state}",
            cluster_power_state=power_state,
        )

    if not ignore_cooldown and is_in_cooldown(pref, now=current):
        return IdleDecision(
            verdict="keep",
            reason="cooldown",
            cluster_power_state=power_state,
        )

    if is_extended(pref, now=current):
        return IdleDecision(
            verdict="keep",
            reason="extended",
            cluster_power_state=power_state,
        )

    active_count, latest, ok, truncated = _scan_cluster_jobs(repo, pref)
    if not ok:
        # Table unreachable — fail safe (never stop without a quorum read).
        return IdleDecision(
            verdict="keep",
            reason="state_repo_unreachable",
            cluster_power_state=power_state,
        )
    # Fold the live K8s ``app=blast`` count into the Table-derived count.
    # ``live_active_jobs`` is ``None`` when the probe could not run (K8s
    # unreachable) — in that case we silently degrade to the state_repo
    # signal alone. A negative value is treated as 0 defensively.
    extra_active = live_active_jobs if (live_active_jobs and live_active_jobs > 0) else 0
    total_active = active_count + extra_active
    if total_active > 0:
        return IdleDecision(
            verdict="keep",
            reason=f"active_jobs:{total_active}",
            active_job_count=total_active,
            cluster_power_state=power_state,
        )
    if truncated:
        # Cluster has > scan window of history rows; the latest activity
        # timestamp we computed may be stale (Azure Tables does not
        # return rows in timestamp order). Refusing to stop in this
        # corner case avoids killing a busy cluster whose recent activity
        # row sorts beyond our scan window.
        return IdleDecision(
            verdict="keep",
            reason="history_scan_truncated",
            cluster_power_state=power_state,
        )

    idle_window = timedelta(minutes=max(1, int(pref.idle_minutes)))

    # A cluster START resets the idle clock: fold ``last_started_at`` into
    # the activity anchor so the user gets a full ``idle_minutes`` grace
    # after every start, even when the last observed job predates that
    # window. ``last_started_at`` only advances on real starts (never on
    # warn ticks), so — like ``created_at`` — it is a drift-free anchor
    # that cannot push the deadline indefinitely. This is the core fix for
    # "started the cluster but it stopped again within one beat tick".
    started = _parse_iso(pref.last_started_at)
    if started is not None and (latest is None or started > latest):
        latest = started

    # A recent live ``app=blast`` start/finish resets the idle clock too, so
    # a cluster that just finished an OpenAPI-submitted burst (active now 0,
    # no dashboard jobstate row) still gets the full idle grace instead of
    # being stopped on the next tick. Like ``last_started_at`` this only ever
    # moves the anchor to a real observed activity time, never into the
    # future, so it cannot defer the stop indefinitely.
    if live_latest_activity is not None and (latest is None or live_latest_activity > latest):
        latest = live_latest_activity

    if latest is None:
        # No jobs ever observed on this cluster. Anchor the idle clock to
        # the preference's ``created_at`` so a freshly-enabled cluster
        # still gets at least ``idle_minutes`` before the first stop.
        #
        # Critique #9.2: ``updated_at`` USED to be the anchor, but
        # ``mark_auto_stop_event`` writes ``updated_at`` AND
        # ``last_skip_at`` on every warn tick. Anchoring on those means
        # the 60-min idle clock keeps getting pushed forward by warn
        # ticks themselves — the cluster ends up stopping at 90-120 min
        # instead of the configured 60 min, costing 30-50% of the
        # cost-saver's value. ``created_at`` is stamped exactly once on
        # first save (``normalise_preference``) and is never touched by
        # warn ticks, so the deadline stays anchored where the user set
        # it. Legacy rows that pre-date the ``created_at`` field fall
        # back to the old behaviour until they next get re-saved.
        raw = pref.created_at or pref.updated_at
        anchor: datetime
        try:
            anchor_text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
            anchor = datetime.fromisoformat(anchor_text) if anchor_text else current
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            anchor = current
        latest = anchor

    deadline = latest + idle_window
    seconds_left = int((deadline - current).total_seconds())

    if seconds_left <= 0:
        # SAFETY: never emit ``stop`` without an ARM-confirmed
        # ``power_state == "Running"``. When ARM was unreachable
        # (``power_state == ""``) the cluster might already be
        # deleted/stopping/mid-provision — issuing another stop just
        # generates noise (or worse, races with provision). The
        # short-circuit higher up already returns ``keep`` for
        # non-Running ARM responses; this guard catches the unknown
        # case so we degrade open without acting.
        if power_state != "Running":
            return IdleDecision(
                verdict="keep",
                reason="power_state_unknown",
                next_stop_at="",
                seconds_until_stop=0,
                active_job_count=0,
                cluster_power_state=power_state,
            )
        return IdleDecision(
            verdict="stop",
            reason=f"idle:{int(idle_window.total_seconds() // 60)}m",
            next_stop_at=_format_iso(current),
            seconds_until_stop=0,
            active_job_count=0,
            cluster_power_state=power_state,
        )

    # Pre-stop banner threshold: surface the countdown in the SPA once
    # less than ~15 min (or the last quarter of the idle window,
    # whichever is smaller) remains. Capping at 15 min keeps the banner
    # from sitting on screen for half an hour on the 60-min default.
    idle_seconds = int(idle_window.total_seconds())
    warn_threshold = max(60, min(15 * 60, idle_seconds // 4))
    verdict: Verdict = "warn" if seconds_left <= warn_threshold else "keep"
    return IdleDecision(
        verdict=verdict,
        reason="idle_pending" if verdict == "warn" else "active",
        next_stop_at=_format_iso(deadline),
        seconds_until_stop=seconds_left,
        active_job_count=0,
        cluster_power_state=power_state,
    )


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "ACTIVE_JOB_TYPES",
    "IdleDecision",
    "StateRepoProtocol",
    "Verdict",
    "evaluate_cluster",
]

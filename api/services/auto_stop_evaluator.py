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
    here so the "no active jobs" gate stays accurate. A jobstate row in an
    active status older than `_ACTIVE_ROW_STALE_SECONDS` is treated as a
    crashed-worker zombie and dropped from the active count, so a stuck row
    can no longer pin the cluster alive forever.
Validation: `uv run pytest -q api/tests/test_auto_stop_evaluator.py`.
"""

from __future__ import annotations

import os
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

Matching is **prefix-aware for the ``prepare_db`` family** (see
:func:`_row_type_blocks_autostop`): the live row types are ``prepare_db_aks`` /
``prepare_db_cancel`` / ``prepare_db_delete`` (the bare ``prepare_db`` only
appears for the legacy server-side sync mode), and an in-flight
``prepare_db_aks`` download genuinely uses the AKS cluster, so it must keep an
otherwise-idle cluster alive. The synchronous ``prepare_db_cancel`` /
``prepare_db_delete`` rows are now born terminal at the source, so prefix
matching does not falsely pin a cluster on those. The ``_ACTIVE_ROW_STALE_SECONDS``
zombie cap still backstops a crashed ``prepare_db_aks`` row.
"""


def _row_type_blocks_autostop(row_type: str) -> bool:
    """True when a row of ``row_type`` should count as "cluster in use".

    Exact membership in :data:`ACTIVE_JOB_TYPES`, plus a prefix match for the
    ``prepare_db`` family (``prepare_db_aks`` / ``prepare_db_cancel`` /
    ``prepare_db_delete``) so a sub-typed download is not silently ignored.
    """
    if row_type in ACTIVE_JOB_TYPES:
        return True
    return row_type.startswith("prepare_db")


# A worker-lost row must eventually stop pinning the cluster, but the timeout
# must outlive that task family's legitimate execution envelope. Warmup and
# ordinary work use 2 h; prepare-db/shard/oracle use 6 h because AKS prepare-db
# has a per-task hard limit of roughly 4 h 45 m. Rows with no parseable
# timestamp still fail safe and count as active.
_ACTIVE_ROW_STALE_SECONDS = int(os.environ.get("AKS_AUTOSTOP_ACTIVE_ROW_STALE_SECONDS", "7200"))
_LONG_DBOPS_ACTIVE_ROW_STALE_SECONDS = int(
    os.environ.get("STALE_DBOPS_PREPARE_DB_SECONDS", "21600")
)


def _active_row_stale_seconds(row_type: str) -> int:
    """Return the execution-envelope-aware zombie threshold for a row type."""

    if row_type in {"prepare_db", "prepare_db_aks", "shard", "oracle"}:
        return _LONG_DBOPS_ACTIVE_ROW_STALE_SECONDS
    return _ACTIVE_ROW_STALE_SECONDS


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
            *would* be stopped if nothing changes. For an active Extend
            grant (``reason == "extended"``) this is the grant expiry —
            the earliest the cluster can stop while the grant holds — so
            the SPA renders a live countdown. Empty when verdict is
            ``keep`` for a non-idle reason with no projected time
            (e.g. disabled, active job, cooldown, degraded read).
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
    now: datetime | None = None,
    stale_after_seconds: int | None = None,
) -> tuple[int, datetime | None, bool]:
    """Single Table query → (active_count, latest_activity_ts, ok).

    Ordering guarantee: ``repo.list_for_scope`` routes through
    ``StateRepository._list_recent_sorted``, which reads the full filtered
    set (up to the repo hard cap) and returns the genuinely most-recent
    ``limit`` rows sorted by ``created_at`` descending. So even when the
    cluster has more than ``limit`` historical rows, the rows we examine
    here ARE the newest ones and ``latest_activity_ts`` is a reliable idle
    anchor — there is no "true latest timestamp beyond the window" hazard
    that would require refusing to stop. (Historically this helper also
    returned a ``truncated`` flag and the evaluator kept the cluster alive
    whenever the scan was full; that guard predated the sorted read and
    permanently disabled auto-stop for any busy cluster — see the dropped
    ``history_scan_truncated`` verdict.)

    Zombie age-out: a row in an active status whose most-recent timestamp
    is older than ``stale_after_seconds`` (default `_ACTIVE_ROW_STALE_SECONDS`)
    is treated as a crashed / ``worker_lost`` leftover and is NOT counted
    as active — otherwise one stuck row pins the cluster alive forever and
    suppresses the SPA auto-stop countdown. The row's timestamp still seeds
    the idle-clock ``latest`` anchor. Rows with no parseable timestamp fail
    safe (still counted active).

    Returns:
        active_count: jobs in ACTIVE_JOB_STATUSES whose type ∈ ACTIVE_JOB_TYPES
            AND whose latest timestamp is within the staleness window.
        latest_activity_ts: most recent ``updated_at`` / ``created_at`` across
            ALL non-deleted rows in scope (terminal jobs included — they
            seed the idle clock).
        ok: False when the Table query raised — caller must fail safe.
    """
    current = now or datetime.now(UTC)
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
        return 0, None, False

    active = 0
    latest: datetime | None = None
    for row in rows:
        row_type = (getattr(row, "type", "") or "")
        row_status = (getattr(row, "status", "") or "")
        row_latest: datetime | None = None
        for field in ("updated_at", "created_at"):
            raw = getattr(row, field, "") or ""
            if not raw:
                continue
            ts = _parse_iso(raw)
            if ts is None:
                continue
            if row_latest is None or ts > row_latest:
                row_latest = ts
            if latest is None or ts > latest:
                latest = ts
        if _row_type_blocks_autostop(row_type) and row_status in ACTIVE_JOB_STATUSES:
            # A row with no parseable timestamp fails safe (counted). A
            # fresh active row counts. A row untouched for longer than the
            # cap is a zombie (crashed worker) and is dropped so it cannot
            # keep the cluster alive forever.
            cap = (
                stale_after_seconds
                if stale_after_seconds is not None
                else _active_row_stale_seconds(row_type)
            )
            if row_latest is None or (current - row_latest).total_seconds() <= cap:
                active += 1
    return active, latest, True


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
    pending_queue_depth: int | None = None,
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
        live_active_jobs: Live BLAST / DB warmup / prepare-db workload count
            observed directly on the Kubernetes cluster (via
            `auto_stop_live.probe_live_blast_activity`), or ``None`` when the
            K8s probe could not run. This is the key fix for
            OpenAPI-submitted runs that never write a dashboard jobstate
            row: it is ADDED to the Table-derived active count, so a live
            run keeps the cluster alive even though the state repo is empty.
            ``None`` (the default) means "not probed / unavailable" and is
            ignored — the probe only ever ADDS protection, it can never
            force a stop, so an unreachable K8s API can never strand a
            cluster running forever.
        live_latest_activity: Most recent live workload observation timestamp,
            or ``None``. Folded into the idle-clock anchor exactly
            like ``last_started_at`` so a just-finished live burst still gets
            the full ``idle_minutes`` grace before a stop. Never advances the
            deadline beyond a real observed activity time, so it cannot push
            the stop indefinitely.
        pending_queue_depth: Active (deliverable) message count in the
            Service Bus request queue, or ``None`` when unavailable/disabled.
            Pending requests are work the cluster must stay up for even
            before the drain bridges them to ``app=blast`` Jobs, so a
            non-zero value keeps the cluster alive (``reason``
            ``sb_queue_pending:{N}``). Without this a Running cluster can be
            stopped in the gap between drained jobs while messages still
            wait, after which the drain hits ``ConnectTimeout`` and the
            backlog strands (DLQ risk). ``None`` is ignored (additive
            protection only); dead-lettered/scheduled messages are excluded
            upstream so a poison backlog cannot keep the cluster up forever.
            Auto-START of an already-Stopped cluster on queue arrival is
            intentionally out of scope -- this only prevents a stop.

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
        # Surface the grant expiry as the projected stop time so the SPA
        # renders a live countdown that reflects the user's Extend press
        # ("Extend 30 min" → "Stops in 29:59"). ``extend_until`` is the
        # earliest the cluster can be stopped while the grant is active —
        # the first tick after it passes re-evaluates the idle clock. Before
        # this, the extended verdict carried ``next_stop_at=""`` and the SPA
        # hid the countdown, leaving only the muted "paused by Extend" note,
        # so a successful Extend looked like a no-op (no visible time added).
        extend_deadline = _parse_iso(pref.extend_until)
        if extend_deadline is not None:
            return IdleDecision(
                verdict="keep",
                reason="extended",
                next_stop_at=_format_iso(extend_deadline),
                seconds_until_stop=max(
                    0, int((extend_deadline - current).total_seconds())
                ),
                cluster_power_state=power_state,
            )
        return IdleDecision(
            verdict="keep",
            reason="extended",
            cluster_power_state=power_state,
        )

    active_count, latest, ok = _scan_cluster_jobs(repo, pref, now=current)
    if not ok:
        # Table unreachable — fail safe (never stop without a quorum read).
        return IdleDecision(
            verdict="keep",
            reason="state_repo_unreachable",
            cluster_power_state=power_state,
        )
    # Fold the live K8s workload count into the Table-derived count.
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
    # Service Bus request queue carries work the cluster must stay up for,
    # even when nothing has bridged to an ``app=blast`` Job yet. Without this
    # a Running cluster can be stopped in the gap between drained jobs while
    # messages still wait in the queue -> the drain then hits ConnectTimeout
    # and the backlog strands (DLQ risk). ``None`` / non-positive = signal
    # unavailable -> ignore and fall through to the idle-anchor decision.
    pending = pending_queue_depth if (pending_queue_depth and pending_queue_depth > 0) else 0
    if pending > 0:
        return IdleDecision(
            verdict="keep",
            reason=f"sb_queue_pending:{pending}",
            active_job_count=pending,
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

    # Durable, monotonic live-activity anchor (chiefly OpenAPI-submitted runs
    # that never write a dashboard jobstate row). Folded in exactly like
    # ``last_started_at`` so a transient live-probe miss — the cluster API
    # server blinking, or a finished run's Job/Pods being garbage-collected —
    # can no longer pull the idle deadline earlier than the last real observed
    # activity. ``mark_auto_stop_live_activity`` only ever advances this field
    # to a real past timestamp, never into the future, so it cannot defer the
    # stop indefinitely. This is the key fix for "the SPA showed a long
    # 'Stops in' countdown but the cluster stopped suddenly": the live probe's
    # high-water mark is now persisted, so it survives the probe going blind.
    live_anchor = _parse_iso(pref.last_live_activity_at)
    if live_anchor is not None and (latest is None or live_anchor > latest):
        latest = live_anchor

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
    "_active_row_stale_seconds",
    "_row_type_blocks_autostop",
    "evaluate_cluster",
]

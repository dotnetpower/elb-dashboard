"""Tests for `api.services.auto_stop_evaluator`.

Responsibility: Cover every verdict branch of `evaluate_cluster` with a
    fake state repository: disabled / stopped power / cooldown / extended /
    active jobs / state-repo failure / idle pending (warn) / idle (stop).
Edit boundaries: Pure unit tests. The evaluator must not touch Azure
    SDK or storage backends — the fake repo + override-`now` cover
    everything.
Key entry points: see per-test docstrings.
Risky contracts: Verdict / reason strings are part of the SPA banner
    contract (frontend `useAutoStopStatus.ts`); rename in lockstep.
Validation: `uv run pytest -q api/tests/test_auto_stop_evaluator.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from api.services.auto_stop import AutoStopPreference
from api.services.auto_stop_evaluator import IdleDecision, evaluate_cluster


@dataclass
class _FakeJob:
    type: str
    status: str
    updated_at: str = ""
    created_at: str = ""


class _FakeRepo:
    def __init__(self, jobs: list[_FakeJob] | None = None, *, raise_on_list: bool = False):
        self._jobs = list(jobs or [])
        self._raise = raise_on_list

    def list_for_scope(
        self,
        *,
        subscription_id: str = "",
        resource_group: str = "",
        cluster_name: str = "",
        limit: int = 50,
        include_payload: bool = True,
    ) -> list[_FakeJob]:
        if self._raise:
            raise RuntimeError("table unreachable")
        return list(self._jobs)


def _pref(**overrides: object) -> AutoStopPreference:
    p = AutoStopPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        enabled=True,
        idle_minutes=60,
        cooldown_minutes=30,
    )
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


_NOW = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)


def test_disabled_short_circuits() -> None:
    decision = evaluate_cluster(
        _pref(enabled=False),
        repo=_FakeRepo(),
        now=_NOW,
        power_state="Running",
    )
    assert decision.verdict == "keep"
    assert decision.reason == "disabled"


def test_stopped_power_state_keeps_cluster() -> None:
    decision = evaluate_cluster(
        _pref(), repo=_FakeRepo(), now=_NOW, power_state="Stopped"
    )
    assert decision.verdict == "keep"
    assert decision.reason.startswith("power_state:")


def test_cooldown_keeps_cluster() -> None:
    pref = _pref(
        last_stop_at=(_NOW - timedelta(minutes=5)).isoformat(timespec="seconds"),
    )
    decision = evaluate_cluster(pref, repo=_FakeRepo(), now=_NOW, power_state="Running")
    assert decision.verdict == "keep"
    assert decision.reason == "cooldown"


def test_ignore_cooldown_bypasses_cooldown_gate() -> None:
    """The act task passes ``ignore_cooldown=True`` so the beat driver's
    own preflight ``last_stop_at`` stamp cannot make the act task skip
    the very stop it was enqueued to perform (the self-livelock fix).

    With a fresh ``last_stop_at`` (inside the cooldown window) and an idle
    cluster, the default path keeps (cooldown) while ``ignore_cooldown``
    proceeds to ``stop``.
    """
    pref = _pref(
        last_stop_at=(_NOW - timedelta(minutes=1)).isoformat(timespec="seconds"),
        created_at=(_NOW - timedelta(hours=4)).isoformat(timespec="seconds"),
    )
    # Default: cooldown wins.
    blocked = evaluate_cluster(pref, repo=_FakeRepo([]), now=_NOW, power_state="Running")
    assert blocked.verdict == "keep"
    assert blocked.reason == "cooldown"
    # Act task: cooldown bypassed, idle cluster stops.
    proceed = evaluate_cluster(
        pref,
        repo=_FakeRepo([]),
        now=_NOW,
        power_state="Running",
        ignore_cooldown=True,
    )
    assert proceed.verdict == "stop"
    assert proceed.reason.startswith("idle:")


def test_extend_overrides_idle() -> None:
    pref = _pref(
        extend_until=(_NOW + timedelta(minutes=10)).isoformat(timespec="seconds"),
    )
    decision = evaluate_cluster(pref, repo=_FakeRepo(), now=_NOW, power_state="Running")
    assert decision.verdict == "keep"
    assert decision.reason == "extended"


def test_active_jobs_block_stop() -> None:
    jobs = [
        _FakeJob(type="blast", status="running"),
        _FakeJob(type="warmup", status="queued"),
        _FakeJob(type="blast", status="completed"),  # not active
    ]
    decision = evaluate_cluster(
        _pref(), repo=_FakeRepo(jobs), now=_NOW, power_state="Running"
    )
    # Single query: 2 active rows (running blast + queued warmup), the
    # completed blast does not count.
    assert decision.verdict == "keep"
    assert decision.reason == "active_jobs:2"
    assert decision.active_job_count == 2


def test_stale_active_row_does_not_block_stop() -> None:
    """A jobstate row stuck in an active status longer than the staleness
    cap is a crashed / ``worker_lost`` zombie and must NOT keep the cluster
    alive — otherwise auto-stop never fires and the SPA loses its countdown
    (the real-world `auto-warmup` row stuck ``running`` for 10 h).
    """
    stale_ts = (_NOW - timedelta(hours=10)).isoformat(timespec="seconds")
    jobs = [_FakeJob(type="warmup", status="running", updated_at=stale_ts)]
    decision = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="Running",
    )
    # Zombie dropped → not active; idle clock anchored at the stale
    # timestamp (10 h ago) → deadline long past → stop, NOT active_jobs.
    assert decision.verdict == "stop"
    assert decision.reason.startswith("idle:")
    assert decision.active_job_count == 0


def test_fresh_active_row_still_blocks_stop() -> None:
    """A genuinely-running active row (recent timestamp) still keeps the
    cluster alive — the staleness age-out must only drop zombies."""
    fresh_ts = (_NOW - timedelta(minutes=5)).isoformat(timespec="seconds")
    jobs = [_FakeJob(type="warmup", status="running", updated_at=fresh_ts)]
    decision = evaluate_cluster(
        _pref(), repo=_FakeRepo(jobs), now=_NOW, power_state="Running"
    )
    assert decision.verdict == "keep"
    assert decision.reason == "active_jobs:1"
    assert decision.active_job_count == 1


def test_prepare_db_aks_row_blocks_stop_via_prefix_match() -> None:
    """The live prepare-db row types are sub-typed (``prepare_db_aks`` /
    ``prepare_db_cancel`` / ``prepare_db_delete``), not the bare ``prepare_db``
    in ``ACTIVE_JOB_TYPES``. An in-flight AKS-fanout download genuinely uses the
    cluster, so prefix matching must keep an otherwise-idle cluster alive."""
    fresh_ts = (_NOW - timedelta(minutes=5)).isoformat(timespec="seconds")
    jobs = [_FakeJob(type="prepare_db_aks", status="running", updated_at=fresh_ts)]
    decision = evaluate_cluster(
        _pref(), repo=_FakeRepo(jobs), now=_NOW, power_state="Running"
    )
    assert decision.verdict == "keep"
    assert decision.reason == "active_jobs:1"
    assert decision.active_job_count == 1


def test_row_type_blocks_autostop_prefix_helper() -> None:
    """Unit-level guard on the prefix matcher: exact members and the
    ``prepare_db`` family count; an unrelated type does not."""
    from api.services.auto_stop_evaluator import _row_type_blocks_autostop

    assert _row_type_blocks_autostop("blast")
    assert _row_type_blocks_autostop("warmup")
    assert _row_type_blocks_autostop("prepare_db")
    assert _row_type_blocks_autostop("prepare_db_aks")
    assert _row_type_blocks_autostop("prepare_db_cancel")
    assert not _row_type_blocks_autostop("openapi_proxy_exec")
    assert not _row_type_blocks_autostop("")


def test_active_row_without_timestamp_fails_safe() -> None:
    """A row with no parseable timestamp cannot be aged out — it still
    counts as active so a brand-new submission (no timestamp yet) is never
    dropped."""
    jobs = [_FakeJob(type="blast", status="running")]
    decision = evaluate_cluster(
        _pref(), repo=_FakeRepo(jobs), now=_NOW, power_state="Running"
    )
    assert decision.verdict == "keep"
    assert decision.reason == "active_jobs:1"


def test_stale_threshold_default_is_two_hours() -> None:
    """The default staleness cap must sit above Celery's 1 h hard task
    time limit (so a live task is never aged out) yet low enough to clear
    a real zombie promptly. Lock the 2 h boundary: a row 1 h55 m stale
    still counts; 2 h05 m does not. Regression guard for the 4 h → 2 h
    default change (the 4 h default left the cluster alive ~2 extra hours).
    """
    just_under = _FakeJob(
        type="warmup",
        status="running",
        updated_at=(_NOW - timedelta(minutes=115)).isoformat(timespec="seconds"),
    )
    under = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo([just_under]),
        now=_NOW,
        power_state="Running",
    )
    assert under.reason == "active_jobs:1"

    just_over = _FakeJob(
        type="warmup",
        status="running",
        updated_at=(_NOW - timedelta(minutes=125)).isoformat(timespec="seconds"),
    )
    over = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo([just_over]),
        now=_NOW,
        power_state="Running",
    )
    # Aged out → not active; idle clock anchored ~2 h ago → deadline passed.
    assert over.verdict == "stop"
    assert over.active_job_count == 0


def test_active_jobs_count_uses_single_query() -> None:
    """Regression: the evaluator must not issue one query per job_type."""

    class _CountingRepo(_FakeRepo):
        def __init__(self, jobs):
            super().__init__(jobs)
            self.calls = 0

        def list_for_scope(self, **kwargs):
            self.calls += 1
            return super().list_for_scope(**kwargs)

    repo = _CountingRepo(
        [
            _FakeJob(type="blast", status="running"),
            _FakeJob(type="warmup", status="queued"),
        ]
    )
    evaluate_cluster(_pref(), repo=repo, now=_NOW, power_state="Running")
    # Exactly one Table query per evaluation tick — previously this loop
    # issued one per ACTIVE_JOB_TYPE (5) + one for latest activity = 6.
    assert repo.calls == 1


def test_state_repo_failure_keeps_cluster_safe() -> None:
    decision = evaluate_cluster(
        _pref(),
        repo=_FakeRepo(raise_on_list=True),
        now=_NOW,
        power_state="Running",
    )
    assert decision.verdict == "keep"
    assert decision.reason == "state_repo_unreachable"


def test_recent_activity_within_window_keeps_running() -> None:
    """A completed job 30 min ago with `idle_minutes=60` → 30 min left, keep."""
    jobs = [
        _FakeJob(
            type="blast",
            status="completed",
            updated_at=(_NOW - timedelta(minutes=30)).isoformat(timespec="seconds"),
        ),
    ]
    decision = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="Running",
    )
    # 30 min left > warn threshold (15 min) → keep, not warn.
    assert decision.verdict == "keep"
    assert decision.reason == "active"
    assert decision.seconds_until_stop > 0
    assert decision.next_stop_at != ""


def test_pending_window_returns_warn() -> None:
    """Less than warn-threshold left → warn (SPA shows banner)."""
    jobs = [
        _FakeJob(
            type="blast",
            status="completed",
            updated_at=(_NOW - timedelta(minutes=50)).isoformat(timespec="seconds"),
        ),
    ]
    decision = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="Running",
    )
    # 10 min left ≤ 15 min warn threshold → warn.
    assert decision.verdict == "warn"
    assert decision.reason == "idle_pending"
    assert decision.seconds_until_stop > 0


def test_deadline_in_past_returns_stop() -> None:
    """No recent activity, deadline passed → stop."""
    jobs = [
        _FakeJob(
            type="blast",
            status="completed",
            updated_at=(_NOW - timedelta(minutes=120)).isoformat(timespec="seconds"),
        ),
    ]
    decision = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="Running",
    )
    assert decision.verdict == "stop"
    assert decision.reason.startswith("idle:")


def test_idle_deadline_with_unknown_power_state_keeps_safe() -> None:
    """ARM unreachable + deadline passed → keep + reason=power_state_unknown.

    Without ARM confirmation we cannot tell if the cluster is already
    deleted / stopping / mid-provision, so emitting a stop here would
    generate noise at best and race with a different operation at worst.
    """
    jobs = [
        _FakeJob(
            type="blast",
            status="completed",
            updated_at=(_NOW - timedelta(minutes=120)).isoformat(timespec="seconds"),
        ),
    ]
    decision = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="",  # ARM unreachable
    )
    assert decision.verdict == "keep"
    assert decision.reason == "power_state_unknown"


def test_no_jobs_observed_anchors_on_updated_at() -> None:
    """Fresh pref with no jobs → idle clock starts from pref.updated_at."""
    pref = _pref(
        idle_minutes=15,
        updated_at=(_NOW - timedelta(minutes=20)).isoformat(timespec="seconds"),
    )
    decision = evaluate_cluster(pref, repo=_FakeRepo([]), now=_NOW, power_state="Running")
    # 20 min since updated_at, window 15 min → stop.
    assert decision.verdict == "stop"


def test_truncated_scan_refuses_to_stop() -> None:
    """When the cluster's history exceeds the scan window, ``latest`` may
    be stale (Azure Tables is not timestamp-ordered). Refusing to stop
    in that corner case avoids killing a busy cluster whose recent
    activity row sorts beyond our scan window."""
    # Fill the scan window with terminal rows from long ago; the latest
    # of those is far enough in the past that the deadline has passed,
    # which would normally trigger `stop`. The truncation guard must
    # downgrade that to `keep`.
    jobs = [
        _FakeJob(
            type="blast",
            status="completed",
            updated_at=(_NOW - timedelta(hours=12)).isoformat(timespec="seconds"),
        )
        for _ in range(200)  # matches default scan limit
    ]
    decision = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="Running",
    )
    assert decision.verdict == "keep"
    assert decision.reason == "history_scan_truncated"


def test_recent_start_resets_idle_clock() -> None:
    """A cluster start within the idle window keeps the cluster running
    even when the last observed job predates the window.

    This is the core fix for "started the cluster but it stopped again
    within one beat tick": ``last_started_at`` is folded into the idle
    anchor so every start grants a full ``idle_minutes`` grace.
    """
    jobs = [
        _FakeJob(
            type="blast",
            status="completed",
            # Last job ran 2 h ago — well past a 15-min idle window.
            updated_at=(_NOW - timedelta(minutes=120)).isoformat(timespec="seconds"),
        ),
    ]
    pref = _pref(
        idle_minutes=15,
        # User started the cluster 2 min ago.
        last_started_at=(_NOW - timedelta(minutes=2)).isoformat(timespec="seconds"),
    )
    decision = evaluate_cluster(pref, repo=_FakeRepo(jobs), now=_NOW, power_state="Running")
    # Anchor = start (2 min ago) + 15 min window = 13 min left → keep.
    assert decision.verdict == "keep"
    assert decision.reason == "active"
    assert decision.seconds_until_stop > 0


def test_stale_start_does_not_prevent_stop() -> None:
    """``last_started_at`` only pushes the deadline forward — a start that
    is itself older than the idle window must not block a stop."""
    pref = _pref(
        idle_minutes=15,
        # Started 2 h ago, no jobs since → idle clock long expired.
        last_started_at=(_NOW - timedelta(minutes=120)).isoformat(timespec="seconds"),
    )
    decision = evaluate_cluster(pref, repo=_FakeRepo([]), now=_NOW, power_state="Running")
    assert decision.verdict == "stop"
    assert decision.reason.startswith("idle:")


def test_recent_start_anchors_when_no_jobs_observed() -> None:
    """A start with no job history at all still grants a full grace,
    overriding the ``created_at`` fallback anchor."""
    pref = _pref(
        idle_minutes=15,
        created_at=(_NOW - timedelta(hours=4)).isoformat(timespec="seconds"),
        last_started_at=(_NOW - timedelta(minutes=1)).isoformat(timespec="seconds"),
    )
    decision = evaluate_cluster(pref, repo=_FakeRepo([]), now=_NOW, power_state="Running")
    assert decision.verdict == "keep"
    assert decision.seconds_until_stop > 0


def test_transitional_provisioning_state_keeps_cluster() -> None:
    """AKS reports ``power_state == "Running"`` the instant a start LRO
    begins while ``provisioning_state`` stays ``Starting``. The evaluator
    must refuse to stop a transitional cluster (stopping mid-start is
    rejected by ARM with ``OperationNotAllowed``)."""
    pref = _pref(
        idle_minutes=15,
        updated_at=(_NOW - timedelta(minutes=120)).isoformat(timespec="seconds"),
    )
    decision = evaluate_cluster(
        pref,
        repo=_FakeRepo([]),
        now=_NOW,
        power_state="Running",
        provisioning_state="Starting",
        ignore_cooldown=True,
    )
    assert decision.verdict == "keep"
    assert decision.reason == "provisioning:Starting"


def test_succeeded_provisioning_state_allows_stop() -> None:
    """A steady ``Succeeded`` provisioning state does not block a stop."""
    pref = _pref(
        idle_minutes=15,
        updated_at=(_NOW - timedelta(minutes=120)).isoformat(timespec="seconds"),
    )
    decision = evaluate_cluster(
        pref,
        repo=_FakeRepo([]),
        now=_NOW,
        power_state="Running",
        provisioning_state="Succeeded",
        ignore_cooldown=True,
    )
    assert decision.verdict == "stop"
    assert decision.reason.startswith("idle:")


def test_idle_decision_to_dict_shape() -> None:
    payload = IdleDecision(
        verdict="warn",
        reason="idle_pending",
        next_stop_at="2026-05-29T13:00:00+00:00",
        seconds_until_stop=600,
        active_job_count=0,
        cluster_power_state="Running",
    ).to_dict()
    assert set(payload) == {
        "verdict",
        "reason",
        "next_stop_at",
        "seconds_until_stop",
        "active_job_count",
        "cluster_power_state",
    }


# ---------------------------------------------------------------------------
# Live K8s ``app=blast`` activity injection (OpenAPI-submitted runs that
# never write a dashboard jobstate row).
# ---------------------------------------------------------------------------


def test_live_active_jobs_block_stop_with_empty_state_repo() -> None:
    """An OpenAPI BLAST run leaves the dashboard jobstate Table empty, so
    the state-repo scan sees 0 active jobs and the deadline has passed.
    The injected live count must keep the cluster alive."""
    jobs = [
        _FakeJob(
            type="blast",
            status="completed",
            updated_at=(_NOW - timedelta(minutes=120)).isoformat(timespec="seconds"),
        ),
    ]
    decision = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="Running",
        live_active_jobs=2,
    )
    assert decision.verdict == "keep"
    assert decision.reason == "active_jobs:2"
    assert decision.active_job_count == 2


def test_live_active_jobs_add_to_state_repo_count() -> None:
    """Live count is ADDED to the Table-derived count, not max-ed."""
    jobs = [_FakeJob(type="blast", status="running")]
    decision = evaluate_cluster(
        _pref(),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="Running",
        live_active_jobs=3,
    )
    assert decision.verdict == "keep"
    assert decision.reason == "active_jobs:4"
    assert decision.active_job_count == 4


def test_live_active_none_falls_back_to_state_repo() -> None:
    """``live_active_jobs=None`` (probe unavailable) must not change the
    state-repo-only decision — an unreachable K8s API can never strand a
    cluster running forever."""
    jobs = [
        _FakeJob(
            type="blast",
            status="completed",
            updated_at=(_NOW - timedelta(minutes=120)).isoformat(timespec="seconds"),
        ),
    ]
    decision = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="Running",
        live_active_jobs=None,
    )
    assert decision.verdict == "stop"
    assert decision.reason.startswith("idle:")


def test_live_active_zero_does_not_block_stop() -> None:
    """A live probe that reports 0 active jobs (no in-flight run) must not
    keep an otherwise-idle cluster alive."""
    jobs = [
        _FakeJob(
            type="blast",
            status="completed",
            updated_at=(_NOW - timedelta(minutes=120)).isoformat(timespec="seconds"),
        ),
    ]
    decision = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="Running",
        live_active_jobs=0,
    )
    assert decision.verdict == "stop"
    assert decision.reason.startswith("idle:")


def test_live_latest_activity_resets_idle_clock() -> None:
    """A just-finished live burst (active now 0) seeds the idle anchor so
    the cluster gets the full idle grace instead of stopping immediately."""
    jobs = [
        _FakeJob(
            type="blast",
            status="completed",
            updated_at=(_NOW - timedelta(minutes=120)).isoformat(timespec="seconds"),
        ),
    ]
    decision = evaluate_cluster(
        _pref(idle_minutes=60),
        repo=_FakeRepo(jobs),
        now=_NOW,
        power_state="Running",
        live_active_jobs=0,
        live_latest_activity=_NOW - timedelta(minutes=5),
    )
    # Anchor = live activity (5 min ago) + 60 min window → ~55 min left.
    assert decision.verdict == "keep"
    assert decision.reason == "active"
    assert decision.seconds_until_stop > 0


def test_stale_live_latest_activity_does_not_block_stop() -> None:
    """``live_latest_activity`` only moves the anchor to a real observed
    time — an old live timestamp must not defer a due stop."""
    decision = evaluate_cluster(
        _pref(idle_minutes=15),
        repo=_FakeRepo([]),
        now=_NOW,
        power_state="Running",
        live_active_jobs=0,
        live_latest_activity=_NOW - timedelta(hours=3),
    )
    assert decision.verdict == "stop"
    assert decision.reason.startswith("idle:")


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

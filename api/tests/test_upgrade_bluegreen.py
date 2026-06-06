"""Tests for the STRICT_BLUEGREEN self-upgrade path.

Module summary: Exercises the flag-gated blue/green branch of the
upgrade pipeline and reconciler — pin-blue/stage-green in
`execute_upgrade_inline`, and the `validating` -> `confirming` ->
`succeeded` / `rolled_back` walk driven by
`reconcile_rolling_out_inline`. The Single-mode path (flag OFF) is
covered by `test_upgrade_task.py`; here we assert both the positive
(flag ON) behaviour and that leaving the flag OFF preserves the legacy
rolling_out finalisation (charter §12a Rule 4 positive+negative gate).

Responsibility: Verify blue/green traffic staging, cutover, confirm-window
  bake, guaranteed traffic-flip rollback, and blue garbage collection.
Edit boundaries: Update when the blue/green state labels, confirm window,
  or revisions traffic contract change.
Key entry points: Tests for pipeline staging, validating->confirming,
  confirming->succeeded+GC, confirming->rolled_back, validating abort,
  and the flag-OFF negative path.
Risky contracts: Success is gated on verified traffic on green
  (`serving_revision() == green`) AND an elapsed confirm window — never
  on `__version__` alone — so green's own beat cannot mark success early.
Validation: `uv run pytest -q api/tests/test_upgrade_bluegreen.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api.services.upgrade import (
    aca_template,
    acr_inventory,
    build_logs,
    history,
    image_builder,
    revisions,
    state,
)
from api.tasks import upgrade as upgrade_task
from api.tasks.upgrade import reconciler
from api.tasks.upgrade import rollback as rollback_task


class _FakeRunner:
    """Stub satisfying both `terminal_exec.run` and `.stream`."""

    def __init__(self) -> None:
        from api.services import terminal_exec

        self.TerminalExecError = terminal_exec.TerminalExecError

    def run(self, argv: list[str], *, cwd: str | None, timeout_seconds: int) -> dict[str, Any]:
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def stream(
        self, argv: list[str], *, cwd: str | None = None, timeout_seconds: int
    ) -> Iterator[dict[str, Any]]:
        yield {"stream": "stdout", "line": "step 1: ok"}
        yield {"exit_code": 0, "duration_ms": 1, "timed_out": False}


class _FakeAca:
    """Fake aca_template surface (read_current_images / swap_images)."""

    def __init__(self) -> None:
        self._current = aca_template.SidecarImages(
            api="myacr.azurecr.io/elb-api:v0.2.1",
            frontend="myacr.azurecr.io/elb-frontend:v0.2.1",
            terminal="myacr.azurecr.io/elb-terminal:v0.2.1",
        )
        self.swap_calls: list[tuple[str, str]] = []
        self.applied_images: list[aca_template.SidecarImages] = []

    def read_current_images(self) -> aca_template.SidecarImages:
        return self._current

    def swap_images(self, *, target_version: str, revision_suffix: str | None = None):
        self.swap_calls.append((target_version, revision_suffix or ""))
        target = aca_template._compute_target_images(target_version)
        self._current = target
        return ("poller", self._current, target)

    def apply_images(
        self, *, images: aca_template.SidecarImages, revision_suffix: str | None = None
    ):
        self.applied_images.append(images)
        return "poller-rb"

    def latest_revision_name(self) -> str:
        return "ca-elb-dashboard--latest"


class _Status:
    def __init__(self, running: str, provisioning: str, replicas: int, active: bool) -> None:
        self.running_state = running
        self.provisioning_state = provisioning
        self.replicas = replicas
        self.active = active
        self.name = "green"
        self.health_state = "Healthy"


class _FakeWatcher:
    def __init__(self, status: _Status) -> None:
        self._status = status

    def revision_status(self, _name: str) -> _Status:
        return self._status


class _FakeRevisions:
    """Injected stand-in for the revisions traffic module."""

    RevisionsError = revisions.RevisionsError
    BLUE_LABEL = revisions.BLUE_LABEL
    GREEN_LABEL = revisions.GREEN_LABEL

    def __init__(
        self,
        *,
        serving: str = "ca-elb-dashboard--blue",
        active: tuple[str, ...] = ("ca-elb-dashboard--blue", "ca-elb-dashboard--green"),
    ) -> None:
        self.serving = serving
        self._active = active
        self.pin_calls: list[str] = []
        self.cutover_calls: list[tuple[str, str]] = []
        self.flip_calls: list[tuple[str, str]] = []

    def strict_bluegreen(self) -> bool:
        return True

    def serving_revision(self, *, client: Any = None) -> str:
        return self.serving

    def list_revisions(self, *, client: Any = None) -> list[revisions.RevisionSummary]:
        return [
            revisions.RevisionSummary(
                name=name,
                active=True,
                weight=100 if name == self.serving else 0,
                label="",
                created_on=None,
                running_state="Running",
                provisioning_state="Provisioned",
            )
            for name in self._active
        ]

    def pin_traffic(self, *, revision_name: str, label: str | None = None, client: Any = None):
        self.pin_calls.append(revision_name)

    def cutover(self, *, green_revision: str, blue_revision: str, client: Any = None):
        self.cutover_calls.append((green_revision, blue_revision))
        self.serving = green_revision

    def flip_traffic(self, *, to_revision: str, from_revision: str, client: Any = None):
        self.flip_calls.append((to_revision, from_revision))
        self.serving = to_revision


class _FakeGc:
    def __init__(self) -> None:
        self.calls = 0

    def collect_garbage_inline(self) -> None:
        self.calls += 1


class _AlwaysExistsAcrClient:
    def get_tag_properties(self, _repo: str, _tag: str):
        return type("P", (), {"created_on": datetime(2026, 5, 22, tzinfo=UTC)})()

    def close(self) -> None:
        pass


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("UPGRADE_GIT_REMOTE", "https://example.test/foo.git")
    monkeypatch.setenv(image_builder.PLATFORM_ACR_NAME_ENV, "myacr")
    monkeypatch.setenv(aca_template.AZURE_SUBSCRIPTION_ID_ENV, "sub-1")
    monkeypatch.setenv(aca_template.AZURE_RESOURCE_GROUP_ENV, "rg-elb")
    monkeypatch.setenv(aca_template.CONTAINER_APP_NAME_ENV, "ca-elb-dashboard")
    state.set_backend(state.InMemoryBackend())
    build_logs.set_backend(build_logs.InMemoryBuildLogBackend())
    history.set_backend(history.InMemoryHistoryBackend())
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcrClient())
    yield
    state.set_backend(None)
    build_logs.set_backend(None)
    history.set_backend(None)
    acr_inventory.set_client_factory_for_tests(None)


def _start() -> Any:
    return upgrade_task.start_upgrade_inline(
        target_version="0.3.0",
        target_sha="abc1234",
        started_by_oid="oid-1",
        enqueue=lambda *args: None,
    )


def _enter_state(new_state: str, **fields: Any) -> None:
    def _mutate(s: state.UpgradeState) -> None:
        s.state = new_state
        for key, value in fields.items():
            setattr(s, key, value)

    state.update_state(_mutate)


# --------------------------------------------------------------------------
# Pipeline staging (execute_upgrade_inline)
# --------------------------------------------------------------------------


def test_pipeline_stages_green_and_enters_validating(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STRICT_BLUEGREEN", "true")
    pin_calls: list[str] = []
    monkeypatch.setattr(revisions, "serving_revision", lambda **_: "ca-elb-dashboard--blue")
    monkeypatch.setattr(
        revisions,
        "pin_traffic",
        lambda *, revision_name, label=None, **_: pin_calls.append(revision_name),
    )

    after_start = _start()
    after = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="abc1234",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=_FakeRunner(),
        aca=_FakeAca(),
    )
    assert after.state == state.STATE_VALIDATING
    assert after.blue_revision == "ca-elb-dashboard--blue"
    assert after.green_revision.startswith("ca-elb-dashboard--v0-3-0-")
    # The validating anchor must be stamped on entry so the green-health
    # timeout is measured from here, not from the upgrade's start.
    assert after.validating_started_at
    # Blue must be pinned (so green starts at 0% traffic) before the swap.
    assert pin_calls == ["ca-elb-dashboard--blue"]


def test_pipeline_flag_off_keeps_single_mode(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STRICT_BLUEGREEN", raising=False)
    after_start = _start()
    after = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="abc1234",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=_FakeRunner(),
        aca=_FakeAca(),
    )
    assert after.state == state.STATE_ROLLING_OUT
    assert after.green_revision == ""
    assert after.blue_revision == ""


# --------------------------------------------------------------------------
# Reconciler: validating -> confirming
# --------------------------------------------------------------------------


def test_validating_healthy_cuts_over_to_confirming(env: None) -> None:
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    _enter_state(
        state.STATE_VALIDATING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        started_at=now.isoformat(timespec="seconds"),
    )
    rev = _FakeRevisions()
    watcher = _FakeWatcher(_Status("Running", "Provisioned", 1, True))
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher, revisions_mod=rev, now=lambda: now
    )
    assert after.state == state.STATE_CONFIRMING
    assert rev.cutover_calls == [("ca-elb-dashboard--green", "ca-elb-dashboard--blue")]
    assert after.confirm_deadline
    assert after.traffic_serving == "ca-elb-dashboard--green"


def test_validating_booting_stays(env: None) -> None:
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    _enter_state(
        state.STATE_VALIDATING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        started_at=now.isoformat(timespec="seconds"),
    )
    rev = _FakeRevisions()
    watcher = _FakeWatcher(_Status("Provisioning", "Provisioning", -1, True))
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher, revisions_mod=rev, now=lambda: now
    )
    assert after.state == state.STATE_VALIDATING
    assert rev.cutover_calls == []


def test_validating_green_failed_aborts_without_flip(env: None) -> None:
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    _enter_state(
        state.STATE_VALIDATING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        started_at=now.isoformat(timespec="seconds"),
    )
    rev = _FakeRevisions()
    watcher = _FakeWatcher(_Status("Failed", "Provisioned", 0, False))
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher, revisions_mod=rev, now=lambda: now
    )
    assert after.state == state.STATE_FAILED_ROLLOUT
    # Green never took traffic → no flip needed.
    assert rev.flip_calls == []
    assert rev.cutover_calls == []


def test_validating_timeout_aborts(env: None) -> None:
    start = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    _enter_state(
        state.STATE_VALIDATING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        started_at=start.isoformat(timespec="seconds"),
    )
    rev = _FakeRevisions()
    watcher = _FakeWatcher(_Status("Provisioning", "Provisioning", -1, True))
    later = start + timedelta(seconds=reconciler.VALIDATING_TIMEOUT_SECONDS + 1)
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher, revisions_mod=rev, now=lambda: later
    )
    assert after.state == state.STATE_FAILED_ROLLOUT


def test_validating_timeout_uses_validating_anchor_not_build_time(env: None) -> None:
    """Regression: the green-health window is measured from when the row
    ENTERED validating, not from `started_at` (which already absorbed the
    clone+build minutes). A green that has only been booting for a few
    seconds must NOT abort just because the overall upgrade is old."""
    build_start = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    # Green was created 30 min into the upgrade (after a long build).
    validating_entry = build_start + timedelta(minutes=30)
    _enter_state(
        state.STATE_VALIDATING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        started_at=build_start.isoformat(timespec="seconds"),
        validating_started_at=validating_entry.isoformat(timespec="seconds"),
    )
    rev = _FakeRevisions()
    watcher = _FakeWatcher(_Status("Provisioning", "Provisioning", -1, True))
    # Only 10 s into the validating window — well under the timeout even
    # though `started_at` is 30 min + 10 s in the past.
    now = validating_entry + timedelta(seconds=10)
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher, revisions_mod=rev, now=lambda: now
    )
    assert after.state == state.STATE_VALIDATING
    assert rev.cutover_calls == []


def test_validating_timeout_env_override(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """UPGRADE_VALIDATING_TIMEOUT_SECONDS shortens the green-health window."""
    monkeypatch.setenv("UPGRADE_VALIDATING_TIMEOUT_SECONDS", "120")
    entry = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    _enter_state(
        state.STATE_VALIDATING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        started_at=entry.isoformat(timespec="seconds"),
        validating_started_at=entry.isoformat(timespec="seconds"),
    )
    rev = _FakeRevisions()
    watcher = _FakeWatcher(_Status("Provisioning", "Provisioning", -1, True))
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher, revisions_mod=rev, now=lambda: entry + timedelta(seconds=121)
    )
    assert after.state == state.STATE_FAILED_ROLLOUT


# --------------------------------------------------------------------------
# Reconciler: confirming -> succeeded / rolled_back
# --------------------------------------------------------------------------


def test_confirming_succeeds_after_deadline_and_gcs_blue(env: None) -> None:
    cut = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    deadline = cut + timedelta(seconds=reconciler.CONFIRM_WINDOW_SECONDS)
    _enter_state(
        state.STATE_CONFIRMING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        confirm_deadline=deadline.isoformat(timespec="seconds"),
    )
    rev = _FakeRevisions(serving="ca-elb-dashboard--green")
    watcher = _FakeWatcher(_Status("Running", "Provisioned", 1, True))
    gc = _FakeGc()
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher,
        revisions_mod=rev,
        gc=gc,
        now=lambda: deadline + timedelta(seconds=1),
    )
    assert after.state == state.STATE_SUCCEEDED
    assert after.traffic_serving == "ca-elb-dashboard--green"
    assert gc.calls == 1


def test_confirming_before_deadline_waits(env: None) -> None:
    cut = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    deadline = cut + timedelta(seconds=reconciler.CONFIRM_WINDOW_SECONDS)
    _enter_state(
        state.STATE_CONFIRMING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        confirm_deadline=deadline.isoformat(timespec="seconds"),
    )
    rev = _FakeRevisions(serving="ca-elb-dashboard--green")
    watcher = _FakeWatcher(_Status("Running", "Provisioned", 1, True))
    gc = _FakeGc()
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher, revisions_mod=rev, gc=gc, now=lambda: cut + timedelta(seconds=10)
    )
    assert after.state == state.STATE_CONFIRMING
    assert gc.calls == 0


def test_confirming_green_degraded_flips_back_to_blue(env: None) -> None:
    cut = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    deadline = cut + timedelta(seconds=reconciler.CONFIRM_WINDOW_SECONDS)
    _enter_state(
        state.STATE_CONFIRMING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        confirm_deadline=deadline.isoformat(timespec="seconds"),
    )
    rev = _FakeRevisions(serving="ca-elb-dashboard--green")
    watcher = _FakeWatcher(_Status("Degraded", "Provisioned", 1, True))
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher, revisions_mod=rev, now=lambda: cut + timedelta(seconds=30)
    )
    assert after.state == state.STATE_ROLLED_BACK
    assert rev.flip_calls == [("ca-elb-dashboard--blue", "ca-elb-dashboard--green")]


def test_confirming_traffic_not_on_green_re_cuts_and_waits(env: None) -> None:
    cut = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    deadline = cut + timedelta(seconds=reconciler.CONFIRM_WINDOW_SECONDS)
    _enter_state(
        state.STATE_CONFIRMING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        confirm_deadline=deadline.isoformat(timespec="seconds"),
    )
    # Serving still reports blue even though deadline elapsed + green healthy.
    rev = _FakeRevisions(serving="ca-elb-dashboard--blue")
    watcher = _FakeWatcher(_Status("Running", "Provisioned", 1, True))
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher, revisions_mod=rev, now=lambda: deadline + timedelta(seconds=1)
    )
    assert after.state == state.STATE_CONFIRMING
    assert rev.cutover_calls == [("ca-elb-dashboard--green", "ca-elb-dashboard--blue")]


def test_confirming_cutover_never_converges_escalates(env: None) -> None:
    """Bounded-loop guard: if the cutover never lands (serving keeps
    reporting blue) past the confirm deadline + grace, the row escalates to
    `rollback_failed` instead of spinning in `confirming` forever."""
    cut = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    deadline = cut + timedelta(seconds=reconciler.CONFIRM_WINDOW_SECONDS)
    _enter_state(
        state.STATE_CONFIRMING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        confirm_deadline=deadline.isoformat(timespec="seconds"),
    )
    rev = _FakeRevisions(serving="ca-elb-dashboard--blue")
    watcher = _FakeWatcher(_Status("Running", "Provisioned", 1, True))
    # Past deadline + the converge grace → escalate, do not re-cut again.
    past = deadline + timedelta(
        seconds=reconciler.CONFIRM_CUTOVER_CONVERGE_GRACE_SECONDS + 1
    )
    after = reconciler.reconcile_rolling_out_inline(
        watcher=watcher, revisions_mod=rev, now=lambda: past
    )
    assert after.state == state.STATE_ROLLBACK_FAILED
    assert rev.cutover_calls == []


# --------------------------------------------------------------------------
# Operator-triggered rollback (start_rollback_inline) fast path
# --------------------------------------------------------------------------


def test_operator_rollback_flips_to_blue_when_warm(env: None) -> None:
    """Blue still active → traffic flip in seconds, no ACR pull, no re-PATCH."""
    _enter_state(
        state.STATE_SUCCEEDED,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        traffic_serving="ca-elb-dashboard--green",
        rollback_target_json=(
            '{"api": "myacr.azurecr.io/elb-api:v0.2.1", '
            '"frontend": "myacr.azurecr.io/elb-frontend:v0.2.1", '
            '"terminal": "myacr.azurecr.io/elb-terminal:v0.2.1"}'
        ),
    )
    rev = _FakeRevisions(serving="ca-elb-dashboard--green")
    aca = _FakeAca()
    after = rollback_task.start_rollback_inline(
        started_by_oid="oid-1",
        reason="manual",
        aca=aca,
        revisions_mod=rev,
    )
    assert after.state == state.STATE_ROLLED_BACK
    assert rev.flip_calls == [("ca-elb-dashboard--blue", "ca-elb-dashboard--green")]
    assert after.traffic_serving == "ca-elb-dashboard--blue"
    # Fast path must NOT re-PATCH images from ACR.
    assert aca.applied_images == []


def test_operator_rollback_falls_back_when_blue_torn_down(env: None) -> None:
    """Blue already GC'd → fast path declines, snapshot re-PATCH runs."""
    _enter_state(
        state.STATE_SUCCEEDED,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        traffic_serving="ca-elb-dashboard--green",
        rollback_target_json=(
            '{"api": "myacr.azurecr.io/elb-api:v0.2.1", '
            '"frontend": "myacr.azurecr.io/elb-frontend:v0.2.1", '
            '"terminal": "myacr.azurecr.io/elb-terminal:v0.2.1"}'
        ),
    )
    # Only green is active now — blue was torn down by post-success GC.
    rev = _FakeRevisions(serving="ca-elb-dashboard--green", active=("ca-elb-dashboard--green",))
    aca = _FakeAca()
    after = rollback_task.start_rollback_inline(
        started_by_oid="oid-1",
        reason="manual",
        aca=aca,
        revisions_mod=rev,
    )
    assert after.state == state.STATE_ROLLED_BACK
    # No flip (blue gone) — the snapshot re-PATCH path ran instead.
    assert rev.flip_calls == []
    assert len(aca.applied_images) == 1


def test_operator_rollback_during_confirm_window_flips_to_blue(env: None) -> None:
    """Confirm window is the highest-value manual-rollback moment: green is
    serving, blue is still warm at 0% → flip back in seconds, no ACR gate."""
    _enter_state(
        state.STATE_CONFIRMING,
        job_id="job-1",
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        traffic_serving="ca-elb-dashboard--green",
        rollback_target_json=(
            '{"api": "myacr.azurecr.io/elb-api:v0.2.1", '
            '"frontend": "myacr.azurecr.io/elb-frontend:v0.2.1", '
            '"terminal": "myacr.azurecr.io/elb-terminal:v0.2.1"}'
        ),
    )
    rev = _FakeRevisions(serving="ca-elb-dashboard--green")
    aca = _FakeAca()
    after = rollback_task.start_rollback_inline(
        started_by_oid="oid-1",
        reason="manual confirm-window revert",
        aca=aca,
        revisions_mod=rev,
    )
    assert after.state == state.STATE_ROLLED_BACK
    assert rev.flip_calls == [("ca-elb-dashboard--blue", "ca-elb-dashboard--green")]
    assert after.traffic_serving == "ca-elb-dashboard--blue"
    # Confirm-window flip never touches ACR.
    assert aca.applied_images == []


def test_operator_rollback_flag_off_uses_snapshot_path(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STRICT_BLUEGREEN off → always the legacy snapshot re-PATCH path."""
    _enter_state(
        state.STATE_SUCCEEDED,
        job_id="job-1",
        blue_revision="ca-elb-dashboard--blue",
        rollback_target_json=(
            '{"api": "myacr.azurecr.io/elb-api:v0.2.1", '
            '"frontend": "myacr.azurecr.io/elb-frontend:v0.2.1", '
            '"terminal": "myacr.azurecr.io/elb-terminal:v0.2.1"}'
        ),
    )

    class _OffRevisions(_FakeRevisions):
        def strict_bluegreen(self) -> bool:
            return False

    rev = _OffRevisions(serving="ca-elb-dashboard--green")
    aca = _FakeAca()
    after = rollback_task.start_rollback_inline(
        started_by_oid="oid-1",
        reason="manual",
        aca=aca,
        revisions_mod=rev,
    )
    assert after.state == state.STATE_ROLLED_BACK
    assert rev.flip_calls == []
    assert len(aca.applied_images) == 1

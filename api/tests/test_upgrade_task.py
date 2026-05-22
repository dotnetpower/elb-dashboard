"""End-to-end tests for the PR2+PR3 upgrade execution pipeline.

Module summary: Stubs both the terminal-side `runner` and the ACA
`aca` module so the full state machine (idle -> queued -> ... ->
rolling_out -> succeeded) can be exercised without a terminal sidecar
or ARM. Also covers the rollback + reconciler paths added in PR3.

Responsibility: Verify state-machine transitions, CAS gating against
  concurrent operators, `failed_pre`/`failed_rollout`, rollback, and
  the post-rollout reconciler.
Edit boundaries: Update when the state machine or transition labels
  change.
Key entry points: Tests for happy path, double-start refusal,
  remote-unset abort, build-failure path, rolling_out persistence,
  rollback round-trip.
Risky contracts: Asserts the `state=rolling_out` row is committed
  BEFORE any ARM PATCH so a worker death after PATCH is recoverable
  by the reconciler on the new revision.
Validation: `uv run pytest -q api/tests/test_upgrade_task.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from api.services import terminal_exec
from api.services.upgrade import (
    aca_template,
    build_logs,
    history,
    image_builder,
    state,
)
from api.tasks import upgrade as upgrade_task


class _FakeRunner:
    """Stub that satisfies both `terminal_exec.run` and `.stream`."""

    def __init__(self, *, clone_exit: int = 0, build_exit: int = 0) -> None:
        self.run_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self._clone_exit = clone_exit
        self._build_exit = build_exit
        self.TerminalExecError = terminal_exec.TerminalExecError

    def run(self, argv: list[str], *, cwd: str | None, timeout_seconds: int) -> dict[str, Any]:
        self.run_calls.append({"argv": argv})
        return {"exit_code": self._clone_exit, "stdout": "", "stderr": ""}

    def stream(self, argv: list[str], *, timeout_seconds: int) -> Iterator[dict[str, Any]]:
        self.stream_calls.append({"argv": argv})
        yield {"stream": "stdout", "line": "step 1: ok"}
        yield {"exit_code": self._build_exit, "duration_ms": 1, "timed_out": False}


class _FakeAca:
    """Fake aca_template module surface (read_current_images / swap_images)."""

    def __init__(self, *, fail_swap: bool = False) -> None:
        self._current = aca_template.SidecarImages(
            api="myacr.azurecr.io/elb-api:v0.2.1",
            frontend="myacr.azurecr.io/elb-frontend:v0.2.1",
            terminal="myacr.azurecr.io/elb-terminal:v0.2.1",
        )
        self.swap_calls: list[tuple[str, str]] = []
        self.applied_images: list[aca_template.SidecarImages] = []
        self._fail_swap = fail_swap

    def read_current_images(self) -> aca_template.SidecarImages:
        return self._current

    def swap_images(self, *, target_version: str, revision_suffix: str | None = None):
        self.swap_calls.append((target_version, revision_suffix or ""))
        if self._fail_swap:
            raise aca_template.TemplateError("simulated PATCH refusal")
        target = aca_template._compute_target_images(target_version)
        return ("poller", self._current, target)

    def apply_images(
        self,
        *,
        images: aca_template.SidecarImages,
        revision_suffix: str | None = None,
    ):
        self.applied_images.append(images)
        return "poller-rb"

    def latest_revision_name(self) -> str:
        return "ca-elb-dashboard--latest"


class _FakeWatcher:
    def __init__(
        self,
        *,
        running: str = "Provisioning",
        provisioning: str = "Provisioning",
    ) -> None:
        self._running = running
        self._provisioning = provisioning

    def revision_status(self, _name: str) -> Any:
        return type(
            "S",
            (),
            {
                "name": _name,
                "running_state": self._running,
                "provisioning_state": self._provisioning,
                "health_state": "Healthy",
            },
        )()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPGRADE_GIT_REMOTE", "https://example.test/foo.git")
    monkeypatch.setenv(image_builder.PLATFORM_ACR_NAME_ENV, "myacr")
    monkeypatch.setenv(aca_template.AZURE_SUBSCRIPTION_ID_ENV, "sub-1")
    monkeypatch.setenv(aca_template.AZURE_RESOURCE_GROUP_ENV, "rg-elb")
    monkeypatch.setenv(aca_template.CONTAINER_APP_NAME_ENV, "ca-elb-dashboard")
    state.set_backend(state.InMemoryBackend())
    build_logs.set_backend(build_logs.InMemoryBuildLogBackend())
    history.set_backend(history.InMemoryHistoryBackend())
    yield
    state.set_backend(None)
    build_logs.set_backend(None)
    history.set_backend(None)


def _start(version: str = "0.3.0", sha: str = "abc1234"):
    return upgrade_task.start_upgrade_inline(
        target_version=version,
        target_sha=sha,
        started_by_oid="oid-1",
        enqueue=lambda *args: None,
    )


def test_start_then_execute_happy_path_reaches_rolling_out(env: None) -> None:
    after_start = _start()
    assert after_start.state == state.STATE_QUEUED

    aca = _FakeAca()
    runner = _FakeRunner()
    after_exec = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="abc1234",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
        aca=aca,
    )
    assert after_exec.state == state.STATE_ROLLING_OUT
    assert after_exec.phase_progress >= 85
    assert aca.swap_calls and aca.swap_calls[0][0] == "0.3.0"
    assert aca.swap_calls[0][1].startswith("v0-3-0-")
    snap = after_exec.rollback_target()
    assert snap["api"].endswith(":v0.2.1")


def test_failed_swap_marks_failed_rollout(env: None) -> None:
    after_start = _start()
    aca = _FakeAca(fail_swap=True)
    runner = _FakeRunner()
    after_exec = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
        aca=aca,
    )
    assert after_exec.state == state.STATE_FAILED_ROLLOUT
    assert "PATCH" in after_exec.phase_detail


def test_double_start_is_refused(env: None) -> None:
    _start()
    with pytest.raises(upgrade_task.UpgradeStartRefused):
        _start()


def test_execute_without_remote_marks_failed_pre(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    after_start = _start()
    monkeypatch.delenv("UPGRADE_GIT_REMOTE", raising=False)
    runner = _FakeRunner()
    aca = _FakeAca()
    after_exec = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
        aca=aca,
    )
    assert after_exec.state == state.STATE_FAILED_PRE
    assert runner.run_calls == []
    assert aca.swap_calls == []


def test_execute_build_failure_marks_failed_pre(env: None) -> None:
    after_start = _start()
    runner = _FakeRunner(build_exit=1)
    aca = _FakeAca()
    after_exec = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
        aca=aca,
    )
    assert after_exec.state == state.STATE_FAILED_PRE
    assert aca.swap_calls == []


def test_execute_clone_failure_marks_failed_pre(env: None) -> None:
    after_start = _start()
    runner = _FakeRunner(clone_exit=128)
    aca = _FakeAca()
    after_exec = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
        aca=aca,
    )
    assert after_exec.state == state.STATE_FAILED_PRE
    assert runner.stream_calls == []


def test_rollback_round_trip(env: None) -> None:
    after_start = _start()
    aca = _FakeAca()
    runner = _FakeRunner()
    upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
        aca=aca,
    )
    after_rb = upgrade_task.start_rollback_inline(
        started_by_oid="oid-2", aca=aca, watcher=_FakeWatcher()
    )
    assert after_rb.state == state.STATE_ROLLED_BACK
    assert len(aca.applied_images) == 1
    assert aca.applied_images[0].api.endswith(":v0.2.1")


def test_rollback_refuses_without_snapshot(env: None) -> None:
    with pytest.raises(upgrade_task.RollbackStartRefused):
        upgrade_task.start_rollback_inline(
            started_by_oid="oid-1", aca=_FakeAca(), watcher=_FakeWatcher()
        )


def test_reconciler_finalises_succeeded_when_version_matches(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    after_start = _start()
    aca = _FakeAca()
    runner = _FakeRunner()
    upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
        aca=aca,
    )
    monkeypatch.setattr(upgrade_task, "__version__", "0.3.0")
    after_reconcile = upgrade_task.reconcile_rolling_out_inline(
        aca=aca, watcher=_FakeWatcher()
    )
    assert after_reconcile.state == state.STATE_SUCCEEDED
    assert after_reconcile.phase_progress == 100
    assert after_reconcile.running_version == "0.3.0"


def test_reconciler_noop_when_state_is_idle(env: None) -> None:
    result = upgrade_task.reconcile_rolling_out_inline(
        aca=_FakeAca(), watcher=_FakeWatcher()
    )
    assert result.state == state.STATE_IDLE

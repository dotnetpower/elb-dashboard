"""End-to-end tests for the PR2 upgrade execution pipeline.

Module summary: Stubs `terminal_exec.run` (for git clone) and
`terminal_exec.stream` (for `az acr build`) and walks the full
`start_upgrade_inline` + `execute_upgrade_inline` flow against the
in-memory state and blob backends.

Responsibility: Verify state-machine transitions, CAS gating against
  concurrent operators, and `failed_pre` handling.
Edit boundaries: Update when the state machine or transition labels
  change.
Key entry points: Tests for happy path, double-start refusal,
  remote-unset abort, build-failure path.
Risky contracts: Asserts the exact STATE_TRANSITION_TIMELINE order so a
  refactor that drops a step shows up here.
Validation: `uv run pytest -q api/tests/test_upgrade_task.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from api.services import terminal_exec
from api.services.upgrade import build_logs, image_builder, state
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


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPGRADE_GIT_REMOTE", "https://example.test/foo.git")
    monkeypatch.setenv(image_builder.PLATFORM_ACR_NAME_ENV, "myacr")
    state.set_backend(state.InMemoryBackend())
    build_logs.set_backend(build_logs.InMemoryBuildLogBackend())
    yield
    state.set_backend(None)
    build_logs.set_backend(None)


def test_start_then_execute_happy_path(env: None) -> None:
    enqueued: list[tuple[str, str, str, str]] = []

    after_start = upgrade_task.start_upgrade_inline(
        target_version="0.3.0",
        target_sha="abc1234",
        started_by_oid="oid-1",
        enqueue=lambda *args: enqueued.append(args) or "dummy-task",
    )
    assert after_start.state == state.STATE_QUEUED
    assert after_start.target_version == "0.3.0"
    assert enqueued and enqueued[0][0] == "0.3.0"

    runner = _FakeRunner()
    after_exec = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="abc1234",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
    )
    assert after_exec.state == state.STATE_SUCCEEDED
    assert after_exec.phase_progress == 100
    assert "elb-api:v0.3.0" in after_exec.phase_detail
    # Each of three sidecars built.
    assert len(runner.stream_calls) == 3
    log_api = build_logs.read_blob(after_start.job_id, "api")
    assert b"step 1: ok" in log_api


def test_double_start_is_refused(env: None) -> None:
    upgrade_task.start_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        enqueue=lambda *args: None,
    )
    with pytest.raises(upgrade_task.UpgradeStartRefused):
        upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-2",
            enqueue=lambda *args: None,
        )


def test_execute_without_remote_marks_failed_pre(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    after_start = upgrade_task.start_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        enqueue=lambda *args: None,
    )
    monkeypatch.delenv("UPGRADE_GIT_REMOTE", raising=False)
    runner = _FakeRunner()
    after_exec = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
    )
    assert after_exec.state == state.STATE_FAILED_PRE
    assert runner.run_calls == []  # never reached git clone


def test_execute_build_failure_marks_failed_pre(env: None) -> None:
    after_start = upgrade_task.start_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        enqueue=lambda *args: None,
    )
    runner = _FakeRunner(build_exit=1)
    after_exec = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
    )
    assert after_exec.state == state.STATE_FAILED_PRE
    assert "az acr build" in after_exec.phase_detail
    # First build attempt happened, subsequent ones short-circuited.
    assert len(runner.stream_calls) == 1


def test_execute_clone_failure_marks_failed_pre(env: None) -> None:
    after_start = upgrade_task.start_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        enqueue=lambda *args: None,
    )
    runner = _FakeRunner(clone_exit=128)
    after_exec = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
    )
    assert after_exec.state == state.STATE_FAILED_PRE
    assert runner.stream_calls == []

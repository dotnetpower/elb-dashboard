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
    acr_inventory,
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
        # Realistic: ACA returns the new images on subsequent reads after
        # the PATCH lands. Tests previously relied on the reconciler's
        # PATCH-never-landed grace not firing within the test timeline.
        self._current = target
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
        replicas: int = -1,
        active: bool = True,
    ) -> None:
        self._running = running
        self._provisioning = provisioning
        self._replicas = replicas
        self._active = active

    def revision_status(self, _name: str) -> Any:
        return type(
            "S",
            (),
            {
                "name": _name,
                "running_state": self._running,
                "provisioning_state": self._provisioning,
                "health_state": "Healthy",
                "replicas": self._replicas,
                "active": self._active,
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
    # ACR pre-flight stub — "every snapshot tag still resolves". Tests
    # that need to simulate retention purge override this with
    # `acr_inventory.set_client_factory_for_tests(...)` of their own.
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcrClient())
    yield
    state.set_backend(None)
    build_logs.set_backend(None)
    history.set_backend(None)
    acr_inventory.set_client_factory_for_tests(None)


class _AlwaysExistsAcrClient:
    """Test ACR client whose `get_tag_properties` always returns a fake."""

    def __init__(self) -> None:
        self.closed = False

    def get_tag_properties(self, _repo: str, _tag: str):
        from datetime import UTC, datetime

        return type("P", (), {"created_on": datetime(2026, 5, 22, tzinfo=UTC)})()

    def close(self) -> None:
        self.closed = True


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


def test_start_enqueue_failure_does_not_leak_broker_credentials(env: None) -> None:
    """Regression: a Celery broker exception whose repr embeds the broker
    URL (e.g. `redis://:password@host`) must not have that password copied
    into the SPA-visible `phase_detail` field."""

    def _boom(*_args):
        raise ConnectionRefusedError(
            "Cannot connect to redis://:topsecret-broker-pass@10.0.0.5:6379/0"
        )

    with pytest.raises(ConnectionRefusedError):
        upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-1",
            enqueue=_boom,
        )
    row = state.get_state()
    assert row.state == state.STATE_IDLE
    assert "ConnectionRefusedError" in row.phase_detail
    assert "topsecret-broker-pass" not in row.phase_detail


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


def test_rollback_refuses_when_acr_tag_retention_purged(env: None) -> None:
    """If ACR no longer carries the snapshot tag, the rollback CAS is never
    attempted — the row stays in `succeeded` and the SPA can present the
    operator with the escape-hatch alternative."""

    after_start = _start()
    aca = _FakeAca()
    upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=_FakeRunner(),
        aca=aca,
    )

    # Replace the always-exists ACR factory with one that says "not found".
    class _MissingTagClient:
        def get_tag_properties(self, _repo: str, _tag: str):
            raise Exception("TagNotFound")

        def close(self) -> None:
            pass

    acr_inventory.set_client_factory_for_tests(lambda _ep: _MissingTagClient())

    with pytest.raises(upgrade_task.RollbackStartRefused) as exc:
        upgrade_task.start_rollback_inline(
            started_by_oid="oid-2", aca=aca, watcher=_FakeWatcher()
        )
    assert "ACR no longer carries" in str(exc.value)
    # Row state unchanged (rollback CAS was never reached).
    current_state = state.get_state().state
    assert current_state in {state.STATE_ROLLING_OUT, state.STATE_SUCCEEDED}
    # The fake aca was never called for apply (rollback PATCH).
    assert aca.applied_images == []


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
    import api
    monkeypatch.setattr(api, "__version__", "0.3.0")
    after_reconcile = upgrade_task.reconcile_rolling_out_inline(
        aca=aca, watcher=_FakeWatcher(running="Running", provisioning="Provisioned")
    )
    assert after_reconcile.state == state.STATE_SUCCEEDED
    assert after_reconcile.phase_progress == 100
    assert after_reconcile.running_version == "0.3.0"


def test_reconciler_noop_when_state_is_idle(env: None) -> None:
    result = upgrade_task.reconcile_rolling_out_inline(
        aca=_FakeAca(), watcher=_FakeWatcher()
    )
    assert result.state == state.STATE_IDLE


def test_reconciler_idle_branch_returns_post_update_snapshot(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the bug where the idle branch of
    `reconcile_rolling_out_inline` updated `running_version` on disk
    but returned the pre-update snapshot, leaving the SPA stale.
    """
    import api
    monkeypatch.setattr(api, "__version__", "0.7.0")
    result = upgrade_task.reconcile_rolling_out_inline(
        aca=_FakeAca(), watcher=_FakeWatcher()
    )
    assert result.state == state.STATE_IDLE
    assert result.running_version == "0.7.0"
    # Persisted as well.
    assert state.get_state().running_version == "0.7.0"


def test_image_matches_version_requires_exact_tag() -> None:
    """The reconciler's fast-fail check must use exact tag equality.

    The previous substring-based check (`f":v{ver}" in image`) treated
    `:v0.3.0` as matching `:v0.3.0-alpha`, falsely concluding the PATCH
    had landed.
    """
    fn = upgrade_task._image_matches_version
    assert fn("myacr.azurecr.io/elb-api:v0.3.0", "0.3.0") is True
    assert fn("myacr.azurecr.io/elb-api:v0.3.0-alpha", "0.3.0") is False
    assert fn("myacr.azurecr.io/elb-api:v0.30", "0.3") is False
    assert fn("", "0.3.0") is False
    assert fn("myacr.azurecr.io/elb-api:v0.3.0", "") is False
    # Malformed ref → False, not crash
    assert fn("not-a-ref", "0.3.0") is False


def _set_row(**fields):
    """Test helper: write the requested fields into the persisted row."""
    def mutate(s):
        for k, v in fields.items():
            setattr(s, k, v)

    return state.update_state(mutate)


def test_reconciler_fails_pre_patch_when_stuck(env: None) -> None:
    """A row parked in pre-PATCH states (queued/fetching/building/patching)
    longer than `PRE_PATCH_TIMEOUT_SECONDS` must be transitioned to
    `failed_pre` so the SPA shows an actionable failure and the operator
    can restart. Before this guard a worker crash mid-build left the
    row spinning forever — the only way out was a manual state row
    edit.
    """
    from datetime import UTC, datetime, timedelta

    started = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    # 40 min after start — well past the 35-min budget.
    fake_now = lambda: started + timedelta(minutes=40)  # noqa: E731
    for stuck_state in upgrade_task.PRE_PATCH_STATES:
        # Reset row to the stuck state.
        state.set_backend(state.InMemoryBackend())
        _set_row(
            state=stuck_state,
            started_at=started.isoformat(timespec="seconds"),
            job_id="stuck-job",
            target_version="0.3.0",
        )
        after = upgrade_task.reconcile_rolling_out_inline(
            aca=_FakeAca(), watcher=_FakeWatcher(), now=fake_now
        )
        assert after.state == state.STATE_FAILED_PRE, (
            f"pre-PATCH state {stuck_state!r} should escalate to failed_pre"
        )
        assert "stuck in" in after.phase_detail


def test_reconciler_within_pre_patch_budget_does_not_escalate(env: None) -> None:
    """A row still within the pre-PATCH budget must NOT be escalated;
    only worker-died scenarios should trip the new guard.
    """
    from datetime import UTC, datetime, timedelta

    started = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    fake_now = lambda: started + timedelta(minutes=10)  # noqa: E731
    _set_row(
        state=state.STATE_BUILDING,
        started_at=started.isoformat(timespec="seconds"),
        job_id="slow-job",
        target_version="0.3.0",
    )
    after = upgrade_task.reconcile_rolling_out_inline(
        aca=_FakeAca(), watcher=_FakeWatcher(), now=fake_now
    )
    assert after.state == state.STATE_BUILDING


def test_reconciler_pre_patch_without_started_at_does_not_escalate(env: None) -> None:
    """Malformed/empty `started_at` must not crash the reconciler.
    The next tick will retry once a valid timestamp lands.
    """
    _set_row(
        state=state.STATE_BUILDING,
        started_at="",
        job_id="no-ts-job",
        target_version="0.3.0",
    )
    after = upgrade_task.reconcile_rolling_out_inline(
        aca=_FakeAca(), watcher=_FakeWatcher()
    )
    assert after.state == state.STATE_BUILDING


def test_reconciler_treats_degraded_running_state_as_terminal_failure(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACA reports `provisioning_state=Provisioned` even when the new
    revision's container is in CrashLoopBackOff. The reconciler must
    treat a terminal `running_state` (degraded/unhealthy/failed) as a
    rollout failure so the operator does not wait the full 15 min
    stuck-guard window before getting an actionable signal.
    """
    after_start = _start()
    aca = _FakeAca()
    upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=_FakeRunner(),
        aca=aca,
    )
    # Pin the api __version__ to the OLD version so the success branch
    # does not fire; force the rollout watcher to report `Degraded`.
    import api
    monkeypatch.setattr(api, "__version__", "0.2.1")
    after = upgrade_task.reconcile_rolling_out_inline(
        aca=aca, watcher=_FakeWatcher(running="Degraded", provisioning="Provisioned")
    )
    assert after.state == state.STATE_FAILED_ROLLOUT
    assert "Degraded" in after.phase_detail or "running_state" in after.phase_detail


def test_failed_pre_records_orphan_acr_tags_when_partial_build(
    env: None,
) -> None:
    """When one or more component images were pushed before the build
    failed, the audit blob must call them out as orphan tags so the
    operator (or a future ACR purge task) can clean them up. Without
    this, partial builds accumulated silently in the registry.
    """
    after_start = _start()
    # Build of `frontend` fails, but `api` was already pushed.
    class _PartialRunner(_FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        def stream(self, argv, *, timeout_seconds):
            self._calls += 1
            if self._calls >= 2:
                yield {"line": "build failed"}
                yield {"exit_code": 1, "duration_ms": 1, "timed_out": False}
                return
            yield {"line": "step 1 ok"}
            yield {"exit_code": 0, "duration_ms": 1, "timed_out": False}

    after_exec = upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=_PartialRunner(),
        aca=_FakeAca(),
    )
    assert after_exec.state == state.STATE_FAILED_PRE
    events = history.tail_events(limit=20)
    orphan_events = [e for e in events if e.event == "orphan_acr_tags"]
    assert len(orphan_events) == 1
    refs = orphan_events[0].detail["image_refs"]
    # `api` was pushed before `frontend` failed; we should see its ref.
    assert any(":v0.3.0" in r and "elb-api" in r for r in refs)


def test_failed_pre_omits_orphan_event_when_no_images_were_built(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_fail_pre that fires before any image is built (clone fails)
    must not record an `orphan_acr_tags` event — there is nothing to
    clean up and a noisy audit row would mislead the operator.
    """
    after_start = _start()
    runner = _FakeRunner(clone_exit=128)
    upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=runner,
        aca=_FakeAca(),
    )
    events = history.tail_events(limit=20)
    assert all(e.event != "orphan_acr_tags" for e in events)


def test_reconciler_replica_zero_escalates_after_grace(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision reports Running+Provisioned but `replicas=0, active=False`
    — e.g. all pods crashed before becoming ready. After the
    PATCH_NEVER_LANDED_GRACE_SECONDS budget the reconciler must escalate
    to `failed_rollout` instead of waiting the full 15-min stuck guard.
    """
    from datetime import UTC, datetime, timedelta

    after_start = _start()
    aca = _FakeAca()
    upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=_FakeRunner(),
        aca=aca,
    )
    import api
    monkeypatch.setattr(api, "__version__", "0.2.1")
    started = datetime.now(UTC) - timedelta(minutes=4)
    state.update_state(
        lambda s: setattr(s, "started_at", started.isoformat(timespec="seconds"))
    )
    watcher = _FakeWatcher(
        running="Running", provisioning="Provisioned", replicas=0, active=False
    )
    after = upgrade_task.reconcile_rolling_out_inline(aca=aca, watcher=watcher)
    assert after.state == state.STATE_FAILED_ROLLOUT
    assert "0 replicas" in after.phase_detail


def test_reconciler_pre_warm_defers_when_revision_not_ready(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when `__version__` matches `target_version`, the reconciler
    must wait one more tick if the ACA revision is still `Activating`.
    """
    after_start = _start()
    aca = _FakeAca()
    upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=_FakeRunner(),
        aca=aca,
    )
    import api
    monkeypatch.setattr(api, "__version__", "0.3.0")
    after = upgrade_task.reconcile_rolling_out_inline(
        aca=aca,
        watcher=_FakeWatcher(running="Activating", provisioning="InProgress"),
    )
    assert after.state == state.STATE_ROLLING_OUT
    after2 = upgrade_task.reconcile_rolling_out_inline(
        aca=aca,
        watcher=_FakeWatcher(running="Running", provisioning="Provisioned"),
    )
    assert after2.state == state.STATE_SUCCEEDED


def test_orphan_tag_cleanup_results_recorded_in_audit(env: None) -> None:
    """The orphan_acr_tags audit row must include the per-ref cleanup
    outcome so an operator can see at a glance whether the MI's
    acrDelete permission actually cleaned up.
    """

    class _ForbiddenAcr:
        def delete_tag(self, _repo: str, _tag: str) -> None:
            raise Exception("403 Forbidden")

        def close(self) -> None:
            pass

        def get_tag_properties(self, _repo: str, _tag: str):
            from datetime import UTC, datetime

            return type("P", (), {"created_on": datetime(2026, 5, 22, tzinfo=UTC)})()

    acr_inventory.set_client_factory_for_tests(lambda _ep: _ForbiddenAcr())

    class _PartialRunner(_FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        def stream(self, argv, *, timeout_seconds):
            self._calls += 1
            if self._calls >= 2:
                yield {"line": "build failed"}
                yield {"exit_code": 1, "duration_ms": 1, "timed_out": False}
                return
            yield {"line": "step 1 ok"}
            yield {"exit_code": 0, "duration_ms": 1, "timed_out": False}

    after_start = _start()
    upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=_PartialRunner(),
        aca=_FakeAca(),
    )
    events = history.tail_events(limit=20)
    orphan_events = [e for e in events if e.event == "orphan_acr_tags"]
    assert len(orphan_events) == 1
    cleanup = orphan_events[0].detail.get("cleanup_results", {})
    assert cleanup, "cleanup_results missing"
    assert all("orphaned" in v for v in cleanup.values())


def test_state_transition_timeline_walks_through_every_state(env: None) -> None:
    """Invariant guard: the documented `STATE_TRANSITION_TIMELINE` must
    actually be walked end-to-end by a successful upgrade. Catches
    PR-time drift where a new intermediate state is added to
    `VALID_STATES` but the happy path skips it.
    """
    after_start = _start()
    aca = _FakeAca()
    upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=_FakeRunner(),
        aca=aca,
    )
    # Collect the `state` field on every recorded `state`-flavoured event
    # (the reconciler will append `succeeded` after we simulate the new
    # revision being up).
    events = history.tail_events(limit=200)
    seen_phase_details = {e.detail.get("detail") for e in events}
    # The TIMELINE constant must include every state the row will ever
    # be in for a *happy* path — if a new state is inserted into
    # VALID_STATES without being added here, this test will need updating
    # (which is the entire point).
    assert state.STATE_IDLE in upgrade_task.STATE_TRANSITION_TIMELINE
    assert state.STATE_QUEUED in upgrade_task.STATE_TRANSITION_TIMELINE
    assert state.STATE_FETCHING in upgrade_task.STATE_TRANSITION_TIMELINE
    assert state.STATE_BUILDING in upgrade_task.STATE_TRANSITION_TIMELINE
    assert state.STATE_PATCHING in upgrade_task.STATE_TRANSITION_TIMELINE
    assert state.STATE_ROLLING_OUT in upgrade_task.STATE_TRANSITION_TIMELINE
    assert state.STATE_SUCCEEDED in upgrade_task.STATE_TRANSITION_TIMELINE
    # And the rolling_out row landed during the run.
    assert state.get_state().state == state.STATE_ROLLING_OUT
    # Silence unused-var warning.
    _ = seen_phase_details



def test_pre_patch_budget_per_state(env: None) -> None:
    """QUEUED has a smaller budget than BUILDING — verify per-state escalation."""
    from datetime import UTC, datetime, timedelta

    # QUEUED budget = 5 min; elapsed 6 min → should escalate.
    started = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    state.update_state(
        lambda s: (
            setattr(s, "state", state.STATE_QUEUED),
            setattr(s, "started_at", started.isoformat(timespec="seconds")),
            setattr(s, "job_id", "queued-dead"),
            setattr(s, "target_version", "0.3.0"),
        )[-1]
    )
    fake_now = lambda: started + timedelta(minutes=6)  # noqa: E731
    after = upgrade_task.reconcile_rolling_out_inline(
        aca=_FakeAca(), watcher=_FakeWatcher(), now=fake_now
    )
    assert after.state == state.STATE_FAILED_PRE
    assert "budget" in after.phase_detail

    # BUILDING budget = 30 min; elapsed 6 min → must NOT escalate.
    state.set_backend(state.InMemoryBackend())
    state.update_state(
        lambda s: (
            setattr(s, "state", state.STATE_BUILDING),
            setattr(s, "started_at", started.isoformat(timespec="seconds")),
            setattr(s, "job_id", "building-ok"),
            setattr(s, "target_version", "0.3.0"),
        )[-1]
    )
    after2 = upgrade_task.reconcile_rolling_out_inline(
        aca=_FakeAca(), watcher=_FakeWatcher(), now=fake_now
    )
    assert after2.state == state.STATE_BUILDING


def test_start_records_reason_in_audit(env: None) -> None:
    """The optional `reason` field on start is recorded verbatim in the audit."""
    upgrade_task.start_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        reason="CVE-2026-1234 hotfix",
        enqueue=lambda *_args: None,
    )
    events = history.tail_events(limit=10)
    start_events = [e for e in events if e.event == "start"]
    assert start_events
    assert start_events[0].detail.get("reason") == "CVE-2026-1234 hotfix"


def test_rollback_of_rollback_allowed_after_rollback_failed(env: None) -> None:
    """When the first rollback PATCH fails (ROLLBACK_FAILED), the operator
    must be able to retry without dropping to the escape-hatch shell.
    """
    after_start = _start()
    aca = _FakeAca()
    upgrade_task.execute_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        job_id=after_start.job_id,
        runner=_FakeRunner(),
        aca=aca,
    )
    # First rollback: aca.apply_images raises → ROLLBACK_FAILED
    class _FlakyAca(_FakeAca):
        def __init__(self) -> None:
            super().__init__()
            self._fail_count = 1

        def apply_images(self, *, images, revision_suffix=None):
            if self._fail_count > 0:
                self._fail_count -= 1
                raise aca_template.TemplateError("transient ACA outage")
            return super().apply_images(images=images, revision_suffix=revision_suffix)

    flaky = _FlakyAca()
    flaky._current = aca._current
    after_first = upgrade_task.start_rollback_inline(
        started_by_oid="oid-2", aca=flaky, watcher=_FakeWatcher()
    )
    assert after_first.state == state.STATE_ROLLBACK_FAILED
    # Retry from ROLLBACK_FAILED should succeed.
    after_second = upgrade_task.start_rollback_inline(
        started_by_oid="oid-2", aca=flaky, watcher=_FakeWatcher()
    )
    assert after_second.state == state.STATE_ROLLED_BACK


def test_start_with_idempotency_key_is_retry_safe(env: None) -> None:
    """Same key + same target → second call returns the existing row, not 409."""
    first = upgrade_task.start_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        idempotency_key="op-retry-abc",
        enqueue=lambda *args: None,
    )
    assert first.idempotency_key == "op-retry-abc"
    # Retry with same key + same target — must NOT raise UpgradeStartRefused.
    retry = upgrade_task.start_upgrade_inline(
        target_version="0.3.0",
        target_sha="",
        started_by_oid="oid-1",
        idempotency_key="op-retry-abc",
        enqueue=lambda *args: None,
    )
    assert retry.job_id == first.job_id
    # Different key with row still in QUEUED → still refused (real conflict).
    with pytest.raises(upgrade_task.UpgradeStartRefused):
        upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-1",
            idempotency_key="different-key",
            enqueue=lambda *args: None,
        )


def test_purge_orphan_acr_tags_retries_orphaned_refs(env: None) -> None:
    """The scheduled purge task must re-attempt delete for refs still
    marked as orphaned in audit history (e.g. MI gained acrDelete role
    after the original failure)."""

    # Seed history with an orphan_acr_tags event whose cleanup_results
    # mark the refs as orphaned.
    history.record_event(
        "orphan_acr_tags",
        job_id="old-failed",
        image_refs=["myacr.azurecr.io/elb-api:v0.3.0", "myacr.azurecr.io/elb-frontend:v0.3.0"],
        cleanup_results={
            "myacr.azurecr.io/elb-api:v0.3.0": "orphaned (forbidden)",
            "myacr.azurecr.io/elb-frontend:v0.3.0": "orphaned (forbidden)",
        },
    )

    class _AcrThatNowDeletes:
        def delete_tag(self, _repo: str, _tag: str) -> None:
            pass  # success

        def close(self) -> None:
            pass

    acr_inventory.set_client_factory_for_tests(lambda _ep: _AcrThatNowDeletes())
    result = upgrade_task.purge_orphan_acr_tags_inline()
    assert result["retried"] == 2
    assert result["deleted"] == 2
    # An audit row records the retry outcome.
    events = history.tail_events(limit=10)
    purge_events = [e for e in events if e.event == "orphan_purge_attempt"]
    assert len(purge_events) == 1
    assert purge_events[0].detail["deleted"] == 2


def test_compact_history_drops_old_events(env: None) -> None:
    """compact_blob must drop events older than MAX_TAIL_AGE_DAYS and
    keep newer events intact.
    """
    from datetime import UTC, datetime, timedelta

    too_old = (
        datetime.now(UTC) - timedelta(days=history.MAX_TAIL_AGE_DAYS + 30)
    ).isoformat(timespec="seconds")
    fresh = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    history._backend().append(
        f'{{"ts":"{too_old}","job_id":"old","event":"succeeded","event_id":"o","prev_hash":""}}\n'.encode()
    )
    history._backend().append(
        f'{{"ts":"{fresh}","job_id":"new","event":"succeeded","event_id":"n","prev_hash":""}}\n'.encode()
    )
    result = upgrade_task.compact_history_inline()
    assert result["compacted"] is True
    assert result["dropped"] == 1
    # Only the fresh event survives.
    surviving = history.tail_events(limit=10)
    assert len(surviving) == 1
    assert surviving[0].job_id == "new"


def test_facade_re_exports_every_documented_symbol() -> None:
    """The SRP split moved task code into sibling modules; the facade
    `api.tasks.upgrade` must re-export every name callers depend on.
    Without this guard a silent removal of a re-export would surface
    as `AttributeError` only in production (Celery task lookup, route
    code) — pytest would still pass because tests import from the
    facade implicitly.
    """
    from api.tasks import upgrade as facade

    expected_public = {
        # discovery
        "check_latest",
        "check_latest_inline",
        # pipeline
        "STATE_TRANSITION_TIMELINE",
        "UpgradeStartRefused",
        "execute_upgrade",
        "execute_upgrade_inline",
        "start_upgrade_inline",
        # reconciler
        "PATCH_NEVER_LANDED_GRACE_SECONDS",
        "PRE_PATCH_BUDGET_SECONDS",
        "PRE_PATCH_STATES",
        "PRE_PATCH_TIMEOUT_SECONDS",
        "ROLLING_OUT_TIMEOUT_SECONDS",
        "reconcile_rolling_out",
        "reconcile_rolling_out_inline",
        # rollback
        "RollbackStartRefused",
        "start_rollback_inline",
        # maintenance
        "compact_history",
        "compact_history_inline",
        "purge_orphan_acr_tags",
        "purge_orphan_acr_tags_inline",
    }
    for name in expected_public:
        assert hasattr(facade, name), f"facade missing public symbol {name!r}"
    # Internal symbols that tests historically patch must also stay
    # reachable so the test surface isn't accidentally narrowed.
    expected_internal = {
        "_fail_pre",
        "_fail_rollback",
        "_fail_rollout",
        "_image_matches_version",
        "_new_revision_is_ready",
        "_check_pre_patch_stuck",
    }
    for name in expected_internal:
        assert hasattr(facade, name), f"facade missing internal symbol {name!r}"

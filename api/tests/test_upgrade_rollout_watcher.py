"""Tests for the rollout watcher.

Module summary: Drives `wait_for_revision` against a stub revision-status
function, asserting the timeout / healthy / unhealthy branches.

Responsibility: Verify rollout watcher's polling and state interpretation.
Edit boundaries: Update when revision-state semantics change.
Key entry points: Tests for healthy, transient unhealthy, terminal
  failure, timeout.
Risky contracts: Asserts that loops respect the injected `sleep` /
  `now` so tests run synchronously.
Validation: `uv run pytest -q api/tests/test_upgrade_rollout_watcher.py`.
"""

from __future__ import annotations

import pytest
from api.services.upgrade import aca_template, rollout_watcher


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(aca_template.AZURE_SUBSCRIPTION_ID_ENV, "sub-1")
    monkeypatch.setenv(aca_template.AZURE_RESOURCE_GROUP_ENV, "rg-elb")
    monkeypatch.setenv(aca_template.CONTAINER_APP_NAME_ENV, "ca-elb-dashboard")


class _StubClient:
    def __init__(self, statuses: list[tuple[str, str]]) -> None:
        self._statuses = list(statuses)
        self.container_apps_revisions = self

    def get_revision(self, rg: str, app: str, revision: str) -> object:
        if not self._statuses:
            raise AssertionError("ran out of stub statuses")
        running, provisioning = self._statuses.pop(0)
        return type(
            "Rev",
            (),
            {
                "name": revision,
                "properties": type(
                    "Props",
                    (),
                    {
                        "running_state": running,
                        "provisioning_state": provisioning,
                        "health_state": "Healthy",
                    },
                )(),
            },
        )


def test_wait_returns_quickly_on_healthy() -> None:
    client = _StubClient([("Running", "Provisioned")])
    sleeps: list[float] = []
    status = rollout_watcher.wait_for_revision(
        "rev-1",
        timeout_seconds=10.0,
        poll_interval_seconds=1.0,
        client=client,
        now=lambda: 0.0,
        sleep=sleeps.append,
    )
    assert status.running_state == "Running"
    assert sleeps == []


def test_wait_polls_then_succeeds() -> None:
    client = _StubClient([("Provisioning", "Provisioning"), ("Running", "Provisioned")])
    sleeps: list[float] = []
    times = iter([0.0, 1.0, 2.0])
    status = rollout_watcher.wait_for_revision(
        "rev-1",
        timeout_seconds=10.0,
        poll_interval_seconds=1.0,
        client=client,
        now=lambda: next(times),
        sleep=sleeps.append,
    )
    assert status.running_state == "Running"
    assert sleeps == [1.0]


def test_wait_raises_on_terminal_failure() -> None:
    client = _StubClient([("Failed", "Failed")])
    with pytest.raises(rollout_watcher.RevisionUnhealthy):
        rollout_watcher.wait_for_revision(
            "rev-1",
            timeout_seconds=10.0,
            poll_interval_seconds=1.0,
            client=client,
            now=lambda: 0.0,
            sleep=lambda _: None,
        )


def test_wait_raises_on_timeout() -> None:
    client = _StubClient(
        [
            ("Provisioning", "Provisioning"),
            ("Provisioning", "Provisioning"),
            ("Provisioning", "Provisioning"),
        ]
    )
    times = iter([0.0, 1.0, 2.0, 3.0, 99.0])
    with pytest.raises(rollout_watcher.RevisionTimeout):
        rollout_watcher.wait_for_revision(
            "rev-1",
            timeout_seconds=5.0,
            poll_interval_seconds=1.0,
            client=client,
            now=lambda: next(times),
            sleep=lambda _: None,
        )

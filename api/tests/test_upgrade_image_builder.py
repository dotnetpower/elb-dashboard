"""Tests for the upgrade image builder.

Module summary: Drives `api.services.upgrade.image_builder.build` with a
fake `runner` and the in-memory build-log backend so no terminal sidecar
or Azure Blob endpoint is touched.

Responsibility: Verify argv shape, log streaming, exit-code handling, and
  per-component result envelopes.
Edit boundaries: Update when build plans or argv shape change.
Key entry points: Tests for happy path, failure, log capture, env guards.
Risky contracts: Confirms `PLATFORM_ACR_NAME` is required and that
  `target_version` must be semver.
Validation: `uv run pytest -q api/tests/test_upgrade_image_builder.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from api.services import terminal_exec
from api.services.upgrade import build_logs, image_builder


class _StreamingRunner:
    def __init__(self, *, exit_code: int, lines: list[str]) -> None:
        self._exit_code = exit_code
        self._lines = lines
        self.calls: list[dict[str, Any]] = []
        self.TerminalExecError = terminal_exec.TerminalExecError

    def stream(self, argv: list[str], *, timeout_seconds: int) -> Iterator[dict[str, Any]]:
        self.calls.append({"argv": argv, "timeout_seconds": timeout_seconds})
        for line in self._lines:
            yield {"stream": "stdout", "line": line}
        yield {"exit_code": self._exit_code, "duration_ms": 100, "timed_out": False}


@pytest.fixture(autouse=True)
def _logs_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(image_builder.PLATFORM_ACR_NAME_ENV, "myacr")
    build_logs.set_backend(build_logs.InMemoryBuildLogBackend())
    yield
    build_logs.set_backend(None)


def test_build_happy_path_returns_image_ref_and_logs() -> None:
    runner = _StreamingRunner(
        exit_code=0,
        lines=[
            "step 1/5: pulling base image",
            "step 5/5: pushed image",
        ],
    )
    result = image_builder.build(
        component="api",
        target_version="0.3.0",
        source_dir="/tmp/elb-upgrade/jobABCD",  # noqa: S108
        job_id="jobABCD",
        runner=runner,
    )
    assert result.component == "api"
    assert result.image_ref == "myacr.azurecr.io/elb-api:v0.3.0"
    assert result.exit_code == 0
    assert result.log_blob == "jobABCD/build-api.log"
    log = build_logs.read_blob("jobABCD", "api")
    assert b"step 1/5" in log
    assert b"step 5/5" in log
    assert b"az acr build" in log  # command echo on first line
    argv = runner.calls[0]["argv"]
    assert argv[0:4] == ["az", "acr", "build", "--registry"]
    assert "elb-api:v0.3.0" in argv
    # `--build-arg APP_VERSION=0.3.0` MUST appear so the resulting image
    # bakes the right `APP_VERSION` env — the reconciler's success
    # detection depends on it.
    assert "--build-arg" in argv
    assert "APP_VERSION=0.3.0" in argv


def test_build_propagates_non_zero_exit_as_error() -> None:
    runner = _StreamingRunner(exit_code=2, lines=["error: dockerfile not found"])
    with pytest.raises(image_builder.ImageBuilderError) as exc:
        image_builder.build(
            component="frontend",
            target_version="0.3.0",
            source_dir="/tmp/elb-upgrade/jobABCD",  # noqa: S108
            job_id="jobABCD",
            runner=runner,
        )
    assert "exit=2" in str(exc.value)
    log = build_logs.read_blob("jobABCD", "frontend")
    assert b"error: dockerfile" in log


def test_build_rejects_invalid_target_version() -> None:
    with pytest.raises(image_builder.ImageBuilderError):
        image_builder.build(
            component="api",
            target_version="not-semver",
            source_dir="/tmp/elb-upgrade/jobABCD",  # noqa: S108
            job_id="jobABCD",
            runner=_StreamingRunner(exit_code=0, lines=[]),
        )


def test_build_commit_version_tags_image_and_stamps_frontend_commit() -> None:
    # api: commit-versioned tag, APP_VERSION baked, no GIT_COMMIT (api Dockerfile
    # does not declare it).
    runner = _StreamingRunner(exit_code=0, lines=["ok"])
    result = image_builder.build(
        component="api",
        target_version="0.2.0-commit.a1b2c3d",
        source_dir="/tmp/elb-upgrade/jobCMT",  # noqa: S108
        job_id="jobCMT",
        runner=runner,
    )
    assert result.image_ref == "myacr.azurecr.io/elb-api:v0.2.0-commit.a1b2c3d"
    argv = runner.calls[0]["argv"]
    assert "elb-api:v0.2.0-commit.a1b2c3d" in argv
    assert "APP_VERSION=0.2.0-commit.a1b2c3d" in argv
    assert not any(a.startswith("GIT_COMMIT=") for a in argv)

    # frontend: also stamps GIT_COMMIT=<short sha> so the SPA header + the
    # commit-available check clear after the upgrade lands.
    fe_runner = _StreamingRunner(exit_code=0, lines=["ok"])
    image_builder.build(
        component="frontend",
        target_version="0.2.0-commit.a1b2c3d",
        source_dir="/tmp/elb-upgrade/jobCMT",  # noqa: S108
        job_id="jobCMT",
        runner=fe_runner,
    )
    fe_argv = fe_runner.calls[0]["argv"]
    assert "GIT_COMMIT=a1b2c3d" in fe_argv


def test_build_requires_acr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(image_builder.PLATFORM_ACR_NAME_ENV, raising=False)
    with pytest.raises(image_builder.ImageBuilderError):
        image_builder.build(
            component="api",
            target_version="0.3.0",
            source_dir="/tmp/elb-upgrade/jobABCD",  # noqa: S108
            job_id="jobABCD",
            runner=_StreamingRunner(exit_code=0, lines=[]),
        )


def test_build_all_iterates_components_in_order() -> None:
    runner = _StreamingRunner(exit_code=0, lines=["ok"])
    results = list(
        image_builder.build_all(
            target_version="0.3.0",
            source_dir="/tmp/elb-upgrade/jobABCD",  # noqa: S108
            job_id="jobABCD",
            runner=runner,
        )
    )
    assert [r.component for r in results] == ["api", "frontend", "terminal"]

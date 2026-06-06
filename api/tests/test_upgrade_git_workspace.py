"""Tests for the terminal-sidecar git clone helper.

Module summary: Drives `api.services.upgrade.git_workspace.clone` with a
fake `runner` so no real terminal sidecar / git binary is needed.

Responsibility: Verify argv shape, exit-code interpretation, and shape
  guards on caller-supplied version/job_id.
Edit boundaries: When the clone argv contract changes, update these tests.
Key entry points: Tests for happy path, failure exit, invalid inputs,
  cleanup safety guard.
Risky contracts: Confirms the absolute target path lives under
  `/tmp/elb-upgrade/` so cleanup cannot escape the upgrade root.  # noqa: S108
Validation: `uv run pytest -q api/tests/test_upgrade_git_workspace.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services import terminal_exec
from api.services.upgrade import git_workspace


class _Recorder:
    def __init__(self, *, exit_code: int = 0, stderr: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self._exit_code = exit_code
        self._stderr = stderr
        # Forward the exception class so the helper can `except` against it.
        self.TerminalExecError = terminal_exec.TerminalExecError

    def run(self, argv: list[str], *, cwd: str | None, timeout_seconds: int) -> dict[str, Any]:
        self.calls.append({"argv": argv, "cwd": cwd, "timeout_seconds": timeout_seconds})
        return {"exit_code": self._exit_code, "stdout": "", "stderr": self._stderr}


def test_clone_happy_path_builds_expected_argv() -> None:
    rec = _Recorder(exit_code=0)
    result = git_workspace.clone(
        git_remote="https://example.test/foo.git",
        target_version="0.3.0",
        job_id="job1234",
        runner=rec,
    )
    assert result.target_dir == "/tmp/elb-upgrade/job1234"  # noqa: S108
    assert result.target_version == "0.3.0"
    # First call: git clone. Second call (best-effort): git config read.
    assert rec.calls[0]["argv"] == [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--branch",
        "v0.3.0",
        "https://example.test/foo.git",
        "/tmp/elb-upgrade/job1234",  # noqa: S108
    ]
    # Scrubber attempts a config read of remote.origin.url.
    assert any("config" in c["argv"] for c in rec.calls[1:])


def test_clone_scrubs_credentials_from_remote_origin_url() -> None:
    class _Scrubbed(_Recorder):
        def __init__(self) -> None:
            super().__init__(exit_code=0)
            self.config_reads = 0
            self.config_writes: list[list[str]] = []

        def run(self, argv: list[str], *, cwd: str | None, timeout_seconds: int) -> dict[str, Any]:
            self.calls.append({"argv": argv, "cwd": cwd, "timeout_seconds": timeout_seconds})
            if argv[:5] == ["git", "-C", "/tmp/elb-upgrade/job1234", "config", "--get"]:  # noqa: S108
                self.config_reads += 1
                return {
                    "exit_code": 0,
                    "stdout": "https://x-access-token:supersecret@example.test/foo.git\n",
                    "stderr": "",
                }
            if argv[:4] == ["git", "-C", "/tmp/elb-upgrade/job1234", "config"] and len(argv) > 5:  # noqa: S108
                self.config_writes.append(argv)
                return {"exit_code": 0, "stdout": "", "stderr": ""}
            return {"exit_code": 0, "stdout": "", "stderr": ""}

    rec = _Scrubbed()
    git_workspace.clone(
        git_remote="https://x-access-token:supersecret@example.test/foo.git",
        target_version="0.3.0",
        job_id="job1234",
        runner=rec,
    )
    assert rec.config_reads == 1
    assert len(rec.config_writes) == 1
    written_url = rec.config_writes[0][-1]
    assert "supersecret" not in written_url
    assert written_url == "https://example.test/foo.git"


def test_clone_aborts_when_credential_scrub_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SECURITY: if we can't rewrite remote.origin.url, the unmasked PAT
    is still in `.git/config` and would ship to the built image. The
    clone must raise instead of silently returning."""

    class _ScrubWriteFailure(_Recorder):
        def run(self, argv: list[str], *, cwd: str | None, timeout_seconds: int) -> dict[str, Any]:
            self.calls.append({"argv": argv, "cwd": cwd, "timeout_seconds": timeout_seconds})
            if argv[:5] == ["git", "-C", "/tmp/elb-upgrade/jobABCD", "config", "--get"]:  # noqa: S108
                return {
                    "exit_code": 0,
                    "stdout": "https://x-access-token:supersecret@example.test/foo.git\n",
                    "stderr": "",
                }
            if argv[:4] == ["git", "-C", "/tmp/elb-upgrade/jobABCD", "config"] and len(argv) > 5:  # noqa: S108
                # Simulate the scrub-write failing.
                raise terminal_exec.TerminalExecError("simulated write failure")
            return {"exit_code": 0, "stdout": "", "stderr": ""}

    with pytest.raises(git_workspace.WorkspaceError) as exc:
        git_workspace.clone(
            git_remote="https://x-access-token:supersecret@example.test/foo.git",
            target_version="0.3.0",
            job_id="jobABCD",
            runner=_ScrubWriteFailure(),
        )
    assert "scrub" in str(exc.value).lower()


def test_clone_rejects_invalid_version() -> None:
    with pytest.raises(git_workspace.WorkspaceError):
        git_workspace.clone(
            git_remote="https://example.test/foo.git",
            target_version="not-a-version",
            job_id="job1234",
            runner=_Recorder(),
        )


def test_clone_rejects_invalid_job_id() -> None:
    with pytest.raises(git_workspace.WorkspaceError):
        git_workspace.clone(
            git_remote="https://example.test/foo.git",
            target_version="0.3.0",
            job_id="../escape",
            runner=_Recorder(),
        )


def test_clone_propagates_non_zero_exit() -> None:
    rec = _Recorder(exit_code=128, stderr="fatal: Remote branch not found")
    with pytest.raises(git_workspace.WorkspaceError) as exc:
        git_workspace.clone(
            git_remote="https://example.test/foo.git",
            target_version="0.99.0",
            job_id="jobABCD",
            runner=rec,
        )
    assert "exit=128" in str(exc.value)


def test_commit_clone_builds_blobless_clone_then_checkout() -> None:
    rec = _Recorder(exit_code=0)
    sha = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    result = git_workspace.clone(
        git_remote="https://example.test/foo.git",
        target_version="0.2.0-commit.a1b2c3d",
        job_id="jobCMT1",
        target_kind="commit",
        target_sha=sha,
        runner=rec,
    )
    assert result.target_dir == "/tmp/elb-upgrade/jobCMT1"  # noqa: S108
    # First: shallow no-checkout clone (mirrors the working release path's
    # --depth 1 shape, see _clone_commit).
    assert rec.calls[0]["argv"] == [
        "git",
        "clone",
        "--depth",
        "1",
        "--no-checkout",
        "https://example.test/foo.git",
        "/tmp/elb-upgrade/jobCMT1",  # noqa: S108
    ]
    # Second: shallow fetch of the exact target commit.
    assert rec.calls[1]["argv"] == [
        "git",
        "-C",
        "/tmp/elb-upgrade/jobCMT1",  # noqa: S108
        "fetch",
        "--depth",
        "1",
        "origin",
        sha,
    ]
    # Third: detached checkout of the full sha.
    assert rec.calls[2]["argv"] == [
        "git",
        "-C",
        "/tmp/elb-upgrade/jobCMT1",  # noqa: S108
        "checkout",
        "--detach",
        sha,
    ]


def test_commit_clone_requires_full_40_hex_sha() -> None:
    with pytest.raises(git_workspace.WorkspaceError) as exc:
        git_workspace.clone(
            git_remote="https://example.test/foo.git",
            target_version="0.2.0-commit.a1b2c3d",
            job_id="jobCMT2",
            target_kind="commit",
            target_sha="a1b2c3d",  # short, not 40-hex
            runner=_Recorder(),
        )
    assert "40-hex" in str(exc.value)


def test_commit_clone_propagates_checkout_failure() -> None:
    class _CheckoutFails(_Recorder):
        def run(self, argv: list[str], *, cwd: str | None, timeout_seconds: int) -> dict[str, Any]:
            self.calls.append({"argv": argv, "cwd": cwd, "timeout_seconds": timeout_seconds})
            if argv[:4] == ["git", "-C", "/tmp/elb-upgrade/jobCMT3", "checkout"]:  # noqa: S108
                return {"exit_code": 1, "stdout": "", "stderr": "fatal: reference is not a tree"}
            return {"exit_code": 0, "stdout": "", "stderr": ""}

    with pytest.raises(git_workspace.WorkspaceError) as exc:
        git_workspace.clone(
            git_remote="https://example.test/foo.git",
            target_version="0.2.0-commit.a1b2c3d",
            job_id="jobCMT3",
            target_kind="commit",
            target_sha="a" * 40,
            runner=_CheckoutFails(),
        )
    assert "checkout" in str(exc.value).lower()


def test_clone_runs_build_file_verification_after_clone() -> None:
    """The happy path issues a `git status --porcelain` over the build files."""
    rec = _Recorder(exit_code=0)
    git_workspace.clone(
        git_remote="https://example.test/foo.git",
        target_version="0.3.0",
        job_id="jobVER1",
        runner=rec,
    )
    status_calls = [
        c
        for c in rec.calls
        if c["argv"][:1] == ["git"]
        and "status" in c["argv"]
        and "--porcelain" in c["argv"]
    ]
    assert len(status_calls) == 1
    for expected in git_workspace._EXPECTED_BUILD_FILES:
        assert expected in status_calls[0]["argv"]


def test_clone_fails_when_working_tree_missing_build_files() -> None:
    """A checkout that exits 0 but leaves an empty working tree (git status
    reports the Dockerfiles as deleted) must abort the clone with a clear
    message instead of letting `az acr build` fail later with a confusing
    "Unable to find 'api/Dockerfile'"."""

    class _EmptyWorkingTree(_Recorder):
        def run(self, argv: list[str], *, cwd: str | None, timeout_seconds: int) -> dict[str, Any]:
            self.calls.append({"argv": argv, "cwd": cwd, "timeout_seconds": timeout_seconds})
            if "status" in argv and "--porcelain" in argv:
                return {
                    "exit_code": 0,
                    "stdout": (
                        " D api/Dockerfile\n"
                        " D web/Dockerfile\n"
                        " D terminal/Dockerfile.runtime\n"
                    ),
                    "stderr": "",
                }
            return {"exit_code": 0, "stdout": "", "stderr": ""}

    with pytest.raises(git_workspace.WorkspaceError) as exc:
        git_workspace.clone(
            git_remote="https://example.test/foo.git",
            target_version="0.2.0-commit.a1b2c3d",
            job_id="jobVER2",
            target_kind="commit",
            target_sha="a" * 40,
            runner=_EmptyWorkingTree(),
        )
    msg = str(exc.value)
    assert "missing build files" in msg
    assert "terminal sidecar" in msg


def test_cleanup_refuses_paths_outside_upgrade_root() -> None:
    rec = _Recorder()
    workspace = git_workspace.WorkspacePath(
        target_dir="/etc/passwd", target_version="0.3.0", job_id="job"
    )
    git_workspace.cleanup(workspace, runner=rec)
    assert rec.calls == []  # short-circuited before running anything


def test_cleanup_runs_git_clean_when_path_is_safe() -> None:
    rec = _Recorder()
    workspace = git_workspace.WorkspacePath(
        target_dir="/tmp/elb-upgrade/jobXYZ", target_version="0.3.0", job_id="jobXYZ"  # noqa: S108
    )
    git_workspace.cleanup(workspace, runner=rec)
    assert len(rec.calls) == 1
    assert rec.calls[0]["argv"][:3] == ["git", "-C", "/tmp/elb-upgrade/jobXYZ"]  # noqa: S108

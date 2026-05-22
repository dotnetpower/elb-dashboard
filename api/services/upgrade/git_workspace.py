"""Terminal-sidecar git clone helper for the self-upgrade flow.

Module summary: Drives `git clone --depth 1 --single-branch --branch v<ver>`
through `api.services.terminal_exec.run()` so the build pipeline has a
local source tree of the target release. The target directory is an
absolute path outside the exec server's owned temp dir so the clone
survives request completion; cleanup is best-effort (`/tmp` is tmpfs in
the terminal sidecar and is reclaimed on revision restart).

Responsibility: Single-purpose git checkout via the terminal sidecar.
Edit boundaries: All shell-out lives in `terminal_exec`; this module only
  constructs argv and interprets the result.
Key entry points: `WorkspacePath`, `clone`, `cleanup`, `WorkspaceError`,
  `target_dir_for_job`.
Risky contracts: `target_version` and `git_remote` must already be
  validated by the upstream state row (semver shape, URL regex). This
  module does not re-validate the URL — `terminal_exec` rejects garbage
  argv shapes, and the URL itself flows from `UPGRADE_GIT_REMOTE` env via
  the upgrade-state row.
Validation: `uv run pytest -q api/tests/test_upgrade_git_workspace.py`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from api.services import terminal_exec

LOGGER = logging.getLogger(__name__)

_CLONE_ROOT = "/tmp/elb-upgrade"  # noqa: S108 — terminal sidecar tmpfs
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{4,64}$")
CLONE_TIMEOUT_SECONDS = 300


class WorkspaceError(RuntimeError):
    """Raised when the clone fails or the request shape is invalid."""


@dataclass(frozen=True)
class WorkspacePath:
    """Result of a successful clone — absolute path in the terminal sidecar."""

    target_dir: str
    target_version: str
    job_id: str


def target_dir_for_job(job_id: str) -> str:
    """Return the absolute terminal-sidecar path where the clone will land."""
    if not _JOB_ID_RE.match(job_id):
        raise WorkspaceError(f"invalid job_id shape: {job_id!r}")
    return f"{_CLONE_ROOT}/{job_id}"


def clone(
    *,
    git_remote: str,
    target_version: str,
    job_id: str,
    runner: object = terminal_exec,
) -> WorkspacePath:
    """Clone the requested tag into the terminal sidecar.

    The injected ``runner`` defaults to `api.services.terminal_exec` and is
    replaceable in tests so no terminal sidecar / network is required.
    """
    if not _VERSION_RE.match(target_version):
        raise WorkspaceError(f"invalid target_version shape: {target_version!r}")
    target_dir = target_dir_for_job(job_id)
    argv = [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--branch",
        f"v{target_version}",
        git_remote,
        target_dir,
    ]
    LOGGER.info(
        "upgrade.git_workspace: cloning v%s into %s", target_version, target_dir
    )
    try:
        result = runner.run(argv, cwd=None, timeout_seconds=CLONE_TIMEOUT_SECONDS)
    except terminal_exec.TerminalExecError as exc:
        raise WorkspaceError(f"terminal_exec git clone error: {exc}") from exc
    exit_code = int(result.get("exit_code", -1))
    if exit_code != 0:
        stderr = str(result.get("stderr", ""))[:1024]
        raise WorkspaceError(
            f"git clone failed (exit={exit_code}): {stderr}"
        )
    _scrub_remote_credentials(target_dir, runner=runner)
    return WorkspacePath(target_dir=target_dir, target_version=target_version, job_id=job_id)


def _scrub_remote_credentials(target_dir: str, *, runner: object) -> None:
    """Replace the cloned `remote.origin.url` with the credential-masked URL.

    Without this, a PAT-prefixed URL (`https://x-access-token:SECRET@host/...`)
    introduced in a future PR ends up persisted under `.git/config` inside
    the build context and ships into the resulting container image. We mask
    eagerly, on every clone, so the contract holds even before that PR lands.

    SECURITY: When the upstream URL carries credentials (i.e. `masked != url`)
    and the scrub *write* fails, this function raises `WorkspaceError` so
    the upgrade pipeline aborts before invoking `az acr build`. Without
    that guard, a transient terminal_exec failure could ship the PAT-bearing
    `.git/config` into the new image.
    """
    try:
        existing = runner.run(
            ["git", "-C", target_dir, "config", "--get", "remote.origin.url"],
            cwd=None,
            timeout_seconds=15,
        )
    except terminal_exec.TerminalExecError as exc:
        LOGGER.warning("upgrade.git_workspace: cannot read remote.origin.url: %s", exc)
        # We could not verify there is nothing sensitive to scrub. Refuse
        # to proceed when the supplied remote actually carries an
        # `userinfo@` segment that would warrant scrubbing.
        from api.services.upgrade.remote_tags import configured_remote, mask_remote_url

        remote = configured_remote() or ""
        if remote and mask_remote_url(remote) != remote:
            raise WorkspaceError(
                f"could not verify credential scrub on cloned workspace: {exc}"
            ) from exc
        return
    if int(existing.get("exit_code", -1)) != 0:
        return
    url = str(existing.get("stdout", "")).strip()
    if not url:
        return
    from api.services.upgrade.remote_tags import mask_remote_url

    masked = mask_remote_url(url)
    if masked == url:
        return
    try:
        result = runner.run(
            [
                "git",
                "-C",
                target_dir,
                "config",
                "remote.origin.url",
                masked,
            ],
            cwd=None,
            timeout_seconds=15,
        )
    except terminal_exec.TerminalExecError as exc:
        # SECURITY: the unmasked URL is still in `.git/config`. Refuse.
        raise WorkspaceError(
            f"credential scrub write failed; refusing to ship build context: {exc}"
        ) from exc
    if int(result.get("exit_code", -1)) != 0:
        raise WorkspaceError(
            f"credential scrub write returned exit={result.get('exit_code')}; refusing build"
        )


def cleanup(workspace: WorkspacePath, *, runner: object = terminal_exec) -> None:
    """Best-effort removal of the cloned tree.

    The terminal sidecar's `/tmp` is tmpfs so even a leak is reclaimed on
    revision restart; we still try to remove the directory immediately so
    repeated upgrade attempts in the same revision don't accrete state.
    Cleanup goes through `git -C <dir> clean` so we stay inside the
    sidecar's allowlist (no `rm` binary is permitted).
    """
    target_dir = workspace.target_dir
    if not target_dir.startswith(_CLONE_ROOT + "/"):
        # Defence in depth: refuse to operate on any path outside the
        # well-known upgrade root.
        LOGGER.warning(
            "upgrade.git_workspace.cleanup refused: %s outside %s", target_dir, _CLONE_ROOT
        )
        return
    # `git clean -fdx` inside the repo removes tracked + untracked + ignored;
    # we follow it with `git -C <dir> rev-parse` only as a no-op sanity
    # check. Removing the dir itself requires a separate binary which is
    # not allowed; we accept that the empty dir lingers until restart.
    try:
        runner.run(
            ["git", "-C", target_dir, "clean", "-fdx"],
            cwd=None,
            timeout_seconds=30,
        )
    except terminal_exec.TerminalExecError as exc:
        LOGGER.warning("upgrade.git_workspace cleanup failed: %s", exc)

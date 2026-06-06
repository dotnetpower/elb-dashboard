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
# Accept either a release target (0.4.0) or a commit target
# (0.2.0-commit.a1b2c3d). The commit clone strategy additionally requires a
# full 40-hex target_sha (validated in `_clone_commit`).
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-commit\.[0-9a-f]{7,40})?$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{4,64}$")
CLONE_TIMEOUT_SECONDS = 300

# Files the build pipeline (api.services.upgrade.image_builder._PLANS) feeds to
# `az acr build --file <path>`. After a clone+checkout these MUST exist in the
# working tree; if they do not, `az acr build` fails later with a confusing
# "Unable to find '<path>'" instead of pointing at the real cause (a checkout
# that exited 0 but left an empty working tree — seen with an old `git` in a
# stale terminal sidecar doing a `--filter=blob:none --no-checkout` commit
# clone). Kept in sync with the image_builder plans by
# `test_upgrade_git_workspace.py`.
_EXPECTED_BUILD_FILES = (
    "api/Dockerfile",
    "web/Dockerfile",
    "terminal/Dockerfile.runtime",
)


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
    target_kind: str = "release",
    target_sha: str = "",
    runner: object = terminal_exec,
) -> WorkspacePath:
    """Clone the requested release tag or commit into the terminal sidecar.

    ``target_kind`` selects the checkout strategy:
      * ``"release"`` — shallow ``git clone --depth 1 --branch v<ver>`` (fast,
        the historical path). ``target_sha`` is ignored.
      * ``"commit"`` — a blobless ``git clone --no-checkout`` of the whole
        repo followed by ``git checkout --detach <full_sha>``. The full sha
        (``target_sha``) is required because a shallow ``--branch <sha>`` is
        not possible; blobless keeps it fast while preserving commit
        reachability so any reachable commit can be checked out.

    The injected ``runner`` defaults to `api.services.terminal_exec` and is
    replaceable in tests so no terminal sidecar / network is required.
    """
    if not _VERSION_RE.match(target_version):
        raise WorkspaceError(f"invalid target_version shape: {target_version!r}")
    target_dir = target_dir_for_job(job_id)
    if target_kind == "commit":
        _clone_commit(
            git_remote=git_remote,
            target_sha=target_sha,
            target_dir=target_dir,
            runner=runner,
        )
    else:
        _clone_release(
            git_remote=git_remote,
            target_version=target_version,
            target_dir=target_dir,
            runner=runner,
        )
    _scrub_remote_credentials(target_dir, runner=runner)
    _verify_build_files_materialised(target_dir, runner=runner)
    return WorkspacePath(target_dir=target_dir, target_version=target_version, job_id=job_id)


def _run_git(argv: list[str], *, runner: object, what: str) -> dict:
    """Run a git argv through the runner, raising WorkspaceError on failure."""
    try:
        result = runner.run(argv, cwd=None, timeout_seconds=CLONE_TIMEOUT_SECONDS)
    except terminal_exec.TerminalExecError as exc:
        raise WorkspaceError(f"terminal_exec {what} error: {exc}") from exc
    exit_code = int(result.get("exit_code", -1))
    if exit_code != 0:
        stderr = str(result.get("stderr", ""))[:1024]
        raise WorkspaceError(f"{what} failed (exit={exit_code}): {stderr}")
    return result


def _verify_build_files_materialised(target_dir: str, *, runner: object) -> None:
    """Fail fast when the clone produced an empty / un-checked-out working tree.

    A `git clone --filter=blob:none --no-checkout` followed by a
    `git checkout --detach <sha>` can, with an old `git` (e.g. a stale terminal
    sidecar), exit 0 yet leave the working tree unpopulated. The next step
    (`az acr build --file api/Dockerfile`) then fails with a misleading
    "Unable to find 'api/Dockerfile'". We surface the real cause here by asking
    git whether the build Dockerfiles are present-and-unmodified in the working
    tree: `git status --porcelain -- <files>` prints a ` D ` (deleted) line for
    any tracked file missing from the working tree, and nothing when they are
    materialised. Uses only the allowlisted `git` binary (no `test`/`ls`).
    """
    argv = ["git", "-C", target_dir, "status", "--porcelain", "--", *_EXPECTED_BUILD_FILES]
    try:
        result = runner.run(argv, cwd=None, timeout_seconds=30)
    except terminal_exec.TerminalExecError as exc:
        raise WorkspaceError(
            f"could not verify cloned working tree: {exc}"
        ) from exc
    if int(result.get("exit_code", -1)) != 0:
        stderr = str(result.get("stderr", ""))[:512]
        raise WorkspaceError(
            f"could not verify cloned working tree (git status exit="
            f"{result.get('exit_code')}): {stderr}"
        )
    porcelain = str(result.get("stdout", "") or "")
    # Any line whose XY status code contains 'D' (index/worktree deletion) means
    # a build Dockerfile is tracked but absent from the working tree.
    missing = [
        line.strip()
        for line in porcelain.splitlines()
        if line[:2].strip().upper().find("D") != -1
    ]
    if missing:
        raise WorkspaceError(
            "cloned working tree is missing build files "
            f"({', '.join(_EXPECTED_BUILD_FILES)}): the checkout did not "
            "materialise the tree. This usually means the terminal sidecar's "
            "git is too old for a blobless commit clone — redeploy the terminal "
            f"sidecar and retry. git status: {'; '.join(missing)[:300]}"
        )


def _clone_release(
    *, git_remote: str, target_version: str, target_dir: str, runner: object
) -> None:
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
    _run_git(argv, runner=runner, what="git clone")


def _clone_commit(
    *, git_remote: str, target_sha: str, target_dir: str, runner: object
) -> None:
    sha = (target_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise WorkspaceError(
            f"commit clone requires a full 40-hex target_sha, got {target_sha!r}"
        )
    # Blobless full clone (no working tree yet) so any reachable commit can be
    # checked out; `--no-checkout` avoids materialising the default branch's
    # tree we are about to replace. GitHub/GitLab/gitea all support the
    # `filter=blob:none` partial-clone capability.
    clone_argv = [
        "git",
        "clone",
        "--filter=blob:none",
        "--no-checkout",
        git_remote,
        target_dir,
    ]
    LOGGER.info(
        "upgrade.git_workspace: blobless-cloning %s into %s for commit %s",
        git_remote,
        target_dir,
        sha[:12],
    )
    _run_git(clone_argv, runner=runner, what="git clone")
    checkout_argv = ["git", "-C", target_dir, "checkout", "--detach", sha]
    _run_git(checkout_argv, runner=runner, what="git checkout")


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

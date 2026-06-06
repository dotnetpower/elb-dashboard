"""ACR image-build orchestrator for the self-upgrade flow.

Module summary: Streams `az acr build` for each control-plane sidecar image
(`elb-api`, `elb-frontend`, `elb-terminal`) through the terminal sidecar
and writes its output to a per-component Append Blob. Mirrors the build
commands in `scripts/dev/postprovision.sh` so the produced images are
byte-identical to a fresh `azd up` build of the same git tree.

Responsibility: One stateless function per component build.
Edit boundaries: argv construction + stream consumption live here.
Key entry points: `BuildPlan`, `plan_builds`, `build`, `build_all`,
  `ImageBuilderError`, `ImageBuildResult`.
Risky contracts: Reads the platform ACR name from `PLATFORM_ACR_NAME` env;
  the image tag must already be a validated target version (`A.B.C` release or
  `A.B.C-commit.<sha>` commit form). The injected `runner` defaults to
  `terminal_exec` and is replaced in tests.
Validation: `uv run pytest -q api/tests/test_upgrade_image_builder.py`.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass

from api.services import terminal_exec
from api.services.upgrade import build_logs

LOGGER = logging.getLogger(__name__)

PLATFORM_ACR_NAME_ENV = "PLATFORM_ACR_NAME"
BUILD_TIMEOUT_SECONDS = 1800  # 30 min — `az acr build` is server-side anyway.
# Accept a release target (0.4.0) or a commit target (0.2.0-commit.a1b2c3d).
# Both produce a valid Docker tag once prefixed with "v".
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-commit\.[0-9a-f]{7,40})?$")

# Per-component build instructions. Mirrors postprovision.sh so an in-app
# build of git ref vA.B.0 produces the same images that a fresh `azd up`
# off that ref would.
@dataclass(frozen=True)
class BuildPlan:
    component: str  # "api" | "frontend" | "terminal"
    image_name: str  # "elb-api" | "elb-frontend" | "elb-terminal"
    dockerfile: str  # path relative to repo root
    context: str  # path relative to repo root


_PLANS: dict[str, BuildPlan] = {
    "api": BuildPlan(
        component="api", image_name="elb-api",
        dockerfile="api/Dockerfile", context=".",
    ),
    "frontend": BuildPlan(
        component="frontend", image_name="elb-frontend",
        dockerfile="web/Dockerfile", context=".",
    ),
    "terminal": BuildPlan(
        component="terminal", image_name="elb-terminal",
        dockerfile="terminal/Dockerfile.runtime", context=".",
    ),
}


class ImageBuilderError(RuntimeError):
    """Raised when an `az acr build` invocation fails."""


@dataclass(frozen=True)
class ImageBuildResult:
    component: str
    image_ref: str  # `<acr>.azurecr.io/<name>:vA.B.0`
    log_blob: str  # `<job_id>/build-<component>.log`
    exit_code: int


def plan_builds(components: list[str] | None = None) -> list[BuildPlan]:
    """Return the build plans for the requested components (default: all)."""
    keys = components or list(_PLANS.keys())
    out: list[BuildPlan] = []
    for key in keys:
        plan = _PLANS.get(key)
        if plan is None:
            raise ImageBuilderError(f"unknown component {key!r}")
        out.append(plan)
    return out


def _validate_inputs(target_version: str) -> None:
    if not _VERSION_RE.match(target_version):
        raise ImageBuilderError(f"invalid target_version: {target_version!r}")


def _acr_name() -> str:
    name = os.environ.get(PLATFORM_ACR_NAME_ENV, "").strip()
    if not name:
        raise ImageBuilderError(
            f"{PLATFORM_ACR_NAME_ENV} is not set; cannot run az acr build"
        )
    return name


def ensure_exec_az_login(*, runner: object = terminal_exec) -> None:
    """Best-effort `az login --identity` into the terminal sidecar's exec cache.

    `az acr build` requires an `az` account context. The exec server runs with
    a dedicated AZURE_CONFIG_DIR that the entrypoint bootstraps with a
    managed-identity login, but that bootstrap is async — a build fired
    immediately after a terminal restart can race it and fail with
    "Please run 'az login' to setup account.". Running the login here, just
    before the build loop, closes that race deterministically.

    Idempotent: `az login --identity` is a no-op refresh when already logged
    in. Failures are swallowed (logged) so a transient IMDS hiccup does not
    mask the real `az acr build` outcome — the build itself surfaces a clear
    error if the account context is still missing. The login runs IN the
    terminal sidecar (same place as the build) via the injected runner.
    """
    client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
    argv = ["az", "login", "--identity", "--allow-no-subscriptions"]
    if client_id:
        # User-assigned MI: modern Azure CLI uses `--client-id` (the old
        # `--username` alias was removed), so passing `--username` here fails
        # with exit 1 ("unrecognized arguments") on current CLI builds.
        argv += ["--client-id", client_id]
    try:
        result = runner.run(argv, cwd=None, timeout_seconds=120)
    except Exception as exc:  # best-effort; the build re-checks the account
        LOGGER.warning(
            "upgrade.image_builder: exec az login --identity failed: %s",
            type(exc).__name__,
        )
        return
    exit_code = int(result.get("exit_code", -1)) if isinstance(result, dict) else -1
    if exit_code != 0:
        LOGGER.warning(
            "upgrade.image_builder: exec az login --identity exit=%s", exit_code
        )


def _argv_for(plan: BuildPlan, *, target_version: str, source_dir: str) -> list[str]:
    acr = _acr_name()
    tag = f"v{target_version}"
    argv = [
        "az",
        "acr",
        "build",
        "--registry",
        acr,
        "--image",
        f"{plan.image_name}:{tag}",
        "--file",
        plan.dockerfile,
        # APP_VERSION is consumed by api/Dockerfile (and web/vite.config.ts)
        # to bake the release version into the image. The upgrade
        # reconciler relies on the running `api.__version__` matching
        # `target_version` to mark `succeeded`, so this MUST be set on
        # every self-built image.
        "--build-arg",
        f"APP_VERSION={target_version}",
    ]
    # For a commit build, also stamp the frontend bundle's commit hash so the
    # SPA header shows the real commit and `isCommitUpdateAvailable` clears
    # once the upgrade lands. web/Dockerfile declares the GIT_COMMIT ARG; the
    # api/terminal Dockerfiles do not, so only pass it to the frontend to
    # avoid an unused-build-arg warning on the other components.
    from api.services.upgrade.version_target import commit_short_sha

    short_sha = commit_short_sha(target_version)
    if short_sha and plan.component == "frontend":
        argv += ["--build-arg", f"GIT_COMMIT={short_sha}"]
    argv.append(source_dir if plan.context == "." else f"{source_dir}/{plan.context}")
    return argv


def build(
    *,
    component: str,
    target_version: str,
    source_dir: str,
    job_id: str,
    runner: object = terminal_exec,
) -> ImageBuildResult:
    """Build a single component image into the platform ACR."""
    _validate_inputs(target_version)
    plan = plan_builds([component])[0]
    argv = _argv_for(plan, target_version=target_version, source_dir=source_dir)
    writer = build_logs.open_writer(job_id, plan.component)
    # Diagnostic: confirm the Dockerfile is physically present in the cloned
    # context right before `az acr build` checks `os.path.isfile`. Helps
    # distinguish a clone/checkout gap from an az-side path issue. Best-effort.
    try:
        probe = runner.run(
            ["git", "-C", source_dir, "ls-files", "--", plan.dockerfile],
            cwd=None,
            timeout_seconds=30,
        )
        if isinstance(probe, dict):
            tracked = str(probe.get("stdout", "")).strip()
            writer.write_line(
                f"# context check: git ls-files {plan.dockerfile!r} -> "
                f"{tracked or '(empty)'} (exit={probe.get('exit_code')})"
            )
    except Exception as exc:  # diagnostic only
        writer.write_line(f"# context check skipped: {type(exc).__name__}")
    writer.write_line(f"$ {' '.join(argv)}")
    exit_code = -1
    try:
        for entry in runner.stream(argv, timeout_seconds=BUILD_TIMEOUT_SECONDS):
            if "line" in entry:
                writer.write_line(str(entry.get("line", "")))
            if "exit_code" in entry:
                exit_code = int(entry["exit_code"])
    except terminal_exec.TerminalExecError as exc:
        writer.write_line(f"!! terminal_exec error: {exc}")
        writer.flush()
        raise ImageBuilderError(f"terminal_exec stream error: {exc}") from exc
    finally:
        writer.flush()
    if exit_code != 0:
        raise ImageBuilderError(
            f"az acr build failed for {plan.component} (exit={exit_code})"
        )
    acr = _acr_name().lower()
    image_ref = f"{acr}.azurecr.io/{plan.image_name}:v{target_version}"
    return ImageBuildResult(
        component=plan.component,
        image_ref=image_ref,
        log_blob=writer.name,
        exit_code=exit_code,
    )


def build_all(
    *,
    target_version: str,
    source_dir: str,
    job_id: str,
    components: list[str] | None = None,
    runner: object = terminal_exec,
) -> Iterator[ImageBuildResult]:
    """Yield one ImageBuildResult per component, in declaration order.

    Sequential by design so log streams stay readable and so a failure in
    one component short-circuits the rest without leaving half-built
    images behind. PR2 ships sequential; parallelisation lands later.
    """
    for plan in plan_builds(components):
        yield build(
            component=plan.component,
            target_version=target_version,
            source_dir=source_dir,
            job_id=job_id,
            runner=runner,
        )

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
  the image tag must already be a validated semver (`vA.B.0` form). The
  injected `runner` defaults to `terminal_exec` and is replaced in tests.
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
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

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


def _argv_for(plan: BuildPlan, *, target_version: str, source_dir: str) -> list[str]:
    acr = _acr_name()
    tag = f"v{target_version}"
    return [
        "az",
        "acr",
        "build",
        "--registry",
        acr,
        "--image",
        f"{plan.image_name}:{tag}",
        "--file",
        plan.dockerfile,
        source_dir if plan.context == "." else f"{source_dir}/{plan.context}",
    ]


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

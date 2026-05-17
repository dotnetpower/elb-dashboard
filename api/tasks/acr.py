"""ACR image build Celery tasks — build ElasticBLAST container images via Azure SDK.

Side effects: Schedules `az acr build` runs on the ACR via the management API.
All tasks are idempotent — re-running a build for an already-built tag is a no-op
at the ACR level.
"""

from __future__ import annotations

import base64
import logging
import shlex
from typing import Any

from celery import shared_task

from api.services import get_credential
from api.services.image_tags import IMAGE_BUILD_INFO, IMAGE_TAGS, SOURCE_REPO

LOGGER = logging.getLogger(__name__)

# `begin_schedule_run` (the SDK equivalent of `az acr build`) only exists on
# the legacy `2019-06-01-preview` API version. The default api-version that
# `ContainerRegistryManagementClient` selects today (2023-07-01) dropped the
# scheduled-run surface. We therefore construct a build-only client pinned
# to the older api-version, while everything else (registry get/list, repo
# inspection) continues to use the default surface.
_BUILD_API_VERSION = "2019-06-01-preview"


@shared_task(
    name="api.tasks.acr.build_images",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def build_images(
    self,
    *,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
    images: list[str] | None = None,
) -> dict[str, Any]:
    """Schedule ACR build runs for the requested (or all) images.

    Uses the ACR management API to queue quick build runs — no local Docker
    daemon required. The API sidecar does not ship a Docker CLI.

    Source paths come from a single source of truth (`IMAGE_BUILD_INFO`
    in `api.services.image_tags`), which mirrors the sibling
    `elastic-blast-azure` repo's actual layout. Images whose
    `IMAGE_BUILD_INFO` entry carries a `pre_build_cmd` (currently the
    `ncbi/elasticblast-job-submit` template-copy trick) are submitted as
    multi-step ACR Tasks via `EncodedTaskRunRequest`; the rest go in as
    plain `DockerBuildRequest`.
    """
    cred = get_credential()
    # Build-only client — pinned to the legacy api-version that still
    # exposes `begin_schedule_run` (the SDK form of `az acr build`).
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient

    mgmt = ContainerRegistryManagementClient(
        cred, subscription_id, api_version=_BUILD_API_VERSION,
    )

    targets = images or list(IMAGE_TAGS.keys())
    results: list[dict[str, str]] = []

    for image_name in targets:
        tag = IMAGE_TAGS.get(image_name)
        if not tag:
            results.append({"image": image_name, "status": "skipped", "error": "unknown image"})
            continue

        full_image = f"{image_name}:{tag}"
        build_info = IMAGE_BUILD_INFO.get(image_name)

        if not build_info:
            results.append(
                {"image": full_image, "status": "skipped", "error": "no IMAGE_BUILD_INFO entry"}
            )
            continue

        try:
            _schedule_acr_build(
                mgmt, resource_group, registry_name, image_name, tag, build_info,
            )
            results.append({"image": full_image, "status": "scheduled"})
            LOGGER.info("ACR build scheduled: %s in %s", full_image, registry_name)
        except Exception as exc:
            error_msg = str(exc)[:500]
            results.append({"image": full_image, "status": "failed", "error": error_msg})
            LOGGER.warning("ACR build failed for %s: %s", full_image, exc)

    return {"results": results}


def _schedule_acr_build(
    mgmt: Any,
    resource_group: str,
    registry_name: str,
    image_name: str,
    tag: str,
    build_info: dict[str, str],
) -> None:
    """Schedule a build run on ACR.

    Every NCBI / dotnetpower Dockerfile in the sibling repo assumes its
    own ``docker-XXX/`` subdirectory is the build context (the upstream
    Makefiles literally do ``cd docker-XXX && docker build .``). The
    SDK's ``DockerBuildRequest`` doesn't expose a separate "context
    directory" knob — the source root is always the build context, so
    a Dockerfile that says ``COPY requirements.txt .`` blows up because
    ACR looks at ``<repo>/requirements.txt`` instead of
    ``<repo>/docker-XXX/requirements.txt``.

    To keep behaviour identical to ``cd docker-XXX && docker build`` we
    submit every build as an inline ACR Tasks v1.1.0 YAML using
    ``EncodedTaskRunRequest`` with ``workingDirectory`` set to the
    image's context directory. Images that need a pre-build step
    (currently ``ncbi/elasticblast-job-submit`` rsyncing templates)
    prepend a ``cmd`` step.

    The build models live alongside ``begin_schedule_run`` on the legacy
    api-version; importing from ``azure.mgmt.containerregistry.models``
    picks the default (2023-07-01) surface, which no longer exposes
    these classes. Pin the import to match the build client.
    """
    from azure.mgmt.containerregistry.v2019_06_01_preview.models import (
        EncodedTaskRunRequest,
        PlatformProperties,
    )

    image_ref = f"{image_name}:{tag}"
    pre_cmd = build_info.get("pre_build_cmd")
    # `dockerfile` is documented as relative to `context`, so the
    # basename inside the per-image working directory is what
    # ``docker build -f`` should see.
    ctx_dir = (
        build_info.get("build_context_dir")
        or build_info.get("context")
        or "."
    )
    dockerfile_in_ctx = build_info["dockerfile"]
    if dockerfile_in_ctx.startswith(f"{ctx_dir}/"):
        dockerfile_in_ctx = dockerfile_in_ctx[len(ctx_dir) + 1 :]

    steps: list[str] = []
    if pre_cmd:
        steps.append("  - cmd: >\n" f"      bash -lc {shlex.quote(pre_cmd)}")
    steps.append(
        "  - build: >\n"
        f"      -t {{{{.Run.Registry}}}}/{image_ref}\n"
        f"      -f {dockerfile_in_ctx}\n"
        "      ."
    )
    if ctx_dir and ctx_dir != ".":
        steps[-1] += f"\n    workingDirectory: {ctx_dir}"
    steps.append(f"  - push:\n      - {{{{.Run.Registry}}}}/{image_ref}")

    task_yaml = "version: v1.1.0\nsteps:\n" + "\n".join(steps) + "\n"
    encoded = base64.b64encode(task_yaml.encode("utf-8")).decode("ascii")
    request = EncodedTaskRunRequest(
        encoded_task_content=encoded,
        source_location=SOURCE_REPO,
        platform=PlatformProperties(os="Linux", architecture="amd64"),
        timeout=3600,
    )

    mgmt.registries.begin_schedule_run(
        resource_group, registry_name, request,
    )

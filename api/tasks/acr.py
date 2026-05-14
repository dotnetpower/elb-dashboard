"""ACR image build Celery tasks — build ElasticBLAST container images via Azure SDK.

Side effects: Schedules `az acr build` runs on the ACR via the management API.
All tasks are idempotent — re-running a build for an already-built tag is a no-op
at the ACR level.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from api.services.azure_clients import acr_client
from api.services import get_credential
from api.services.image_tags import IMAGE_TAGS

LOGGER = logging.getLogger(__name__)

# Source repos for each image (github context → Dockerfile).
# These match the elastic-blast-azure build pipeline.
_IMAGE_SOURCES: dict[str, dict[str, str]] = {
    "ncbi/elb": {
        "source": "https://github.com/ncbi/ElasticBLAST.git",
        "docker_file": "Dockerfile",
        "context": ".",
    },
    "ncbi/elasticblast-job-submit": {
        "source": "https://github.com/ncbi/ElasticBLAST.git",
        "docker_file": "docker/elasticblast-job-submit/Dockerfile",
        "context": ".",
    },
    "ncbi/elasticblast-query-split": {
        "source": "https://github.com/ncbi/ElasticBLAST.git",
        "docker_file": "docker/elasticblast-query-split/Dockerfile",
        "context": ".",
    },
    "elb-openapi": {
        "source": "https://github.com/dotnetpower/elastic-blast-azure.git",
        "docker_file": "docker/elb-openapi/Dockerfile",
        "context": ".",
    },
}


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
    """Schedule ACR build tasks for the requested (or all) images.

    Uses the ACR management API to queue quick build tasks — no local Docker
    daemon required. The API sidecar does not ship a Docker CLI.
    """
    cred = get_credential()
    mgmt = acr_client(cred, subscription_id)

    targets = images or list(IMAGE_TAGS.keys())
    results: list[dict[str, str]] = []

    for image_name in targets:
        tag = IMAGE_TAGS.get(image_name)
        if not tag:
            results.append({"image": image_name, "status": "skipped", "error": "unknown image"})
            continue

        full_image = f"{image_name}:{tag}"
        source_info = _IMAGE_SOURCES.get(image_name)

        if not source_info:
            # Fall back to a quick build from a minimal Dockerfile
            results.append({"image": full_image, "status": "skipped", "error": "no source config"})
            continue

        try:
            _schedule_acr_build(
                mgmt, resource_group, registry_name, image_name, tag, source_info,
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
    source_info: dict[str, str],
) -> None:
    """Schedule a quick build task on ACR using the management API."""
    from azure.mgmt.containerregistry.models import (
        DockerBuildRequest,
        PlatformProperties,
    )

    build_request = DockerBuildRequest(
        image_names=[f"{image_name}:{tag}"],
        source_location=source_info["source"],
        docker_file_path=source_info["docker_file"],
        is_push_enabled=True,
        platform=PlatformProperties(os="Linux", architecture="amd64"),
        timeout=3600,
    )

    mgmt.registries.begin_schedule_run(
        resource_group, registry_name, build_request,
    )

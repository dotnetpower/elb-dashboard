"""OpenAPI deployment status helpers.

Responsibility: Read the sibling OpenAPI Kubernetes deployment status
Edit boundaries: Keep Kubernetes deployment inspection here; routes should only validate HTTP
input and shape responses.
Key entry points: `get_openapi_deployment_status`
Risky contracts: Use direct Kubernetes API helpers; never shell out or use Azure Run Command.
Validation: `uv run pytest -q api/tests/test_openapi_deployment.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.monitoring import _get_k8s_session
from api.tasks.openapi.constants import (
    OPENAPI_MANIFEST_REVISION,
    OPENAPI_MANIFEST_REVISION_ANNOTATION,
)

OPENAPI_DEPLOYMENT_NAME = "elb-openapi"
OPENAPI_CONTAINER_NAME = "openapi"
K8S_NAMESPACE = "default"


@dataclass(frozen=True)
class OpenApiDeploymentError(Exception):
    status_code: int
    code: str
    message: str


def _deployment_url(server: str, namespace: str, deployment_name: str) -> str:
    return f"{server}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}"


def _read_deployment(
    session: Any,
    server: str,
    namespace: str,
    deployment_name: str,
) -> dict[str, Any]:
    response = session.get(_deployment_url(server, namespace, deployment_name), timeout=10)
    if response.status_code == 404:
        raise OpenApiDeploymentError(
            404,
            "openapi_deployment_not_found",
            "The elb-openapi deployment was not found in AKS.",
        )
    if response.status_code != 200:
        raise OpenApiDeploymentError(
            502,
            "openapi_deployment_unavailable",
            f"Kubernetes returned HTTP {response.status_code} while reading elb-openapi.",
        )
    data = response.json()
    return data if isinstance(data, dict) else {}


def _container_image(deployment: dict[str, Any], container_name: str) -> str:
    containers = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
        or []
    )
    for container in containers:
        if container.get("name") == container_name and container.get("image"):
            return str(container["image"]).strip()
    return ""


def _image_tag(image: str) -> str:
    if not image or "@" in image:
        return ""
    last_segment = image.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return ""
    return last_segment.rsplit(":", 1)[-1].strip()


def _image_repository(image: str) -> str:
    if not image:
        return ""
    if "@" in image:
        return image.split("@", 1)[0]
    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    if last_colon > last_slash:
        return image[:last_colon]
    return image


def _manifest_revision(deployment: dict[str, Any]) -> int | None:
    """Return the live Deployment's stamped manifest revision, or None.

    Deployments applied before the annotation existed (or by an external tool)
    carry no revision; those are treated as outdated by the caller so the SPA
    prompts a redeploy onto the current manifest.
    """
    annotations = deployment.get("metadata", {}).get("annotations", {}) or {}
    raw = annotations.get(OPENAPI_MANIFEST_REVISION_ANNOTATION)
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def get_openapi_deployment_status(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str = K8S_NAMESPACE,
) -> dict[str, Any]:
    """Return the deployed ``elb-openapi`` container image and image tag."""

    session, server = _get_k8s_session(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        admin=True,
    )
    try:
        deployment = _read_deployment(session, server, namespace, OPENAPI_DEPLOYMENT_NAME)
        image = _container_image(deployment, OPENAPI_CONTAINER_NAME)
        manifest_revision = _manifest_revision(deployment)
    finally:
        session.close()

    # A live Deployment whose manifest predates the dashboard's current
    # generation (missing annotation, or a lower revision) needs a redeploy to
    # pick up a redeploy-only manifest change (e.g. the single-replica queue
    # owner). Bicep/azd never touch this in-cluster Deployment, so the SPA is
    # the only place that can surface the drift.
    manifest_outdated = manifest_revision is None or manifest_revision < OPENAPI_MANIFEST_REVISION

    return {
        "configured": bool(image),
        "deployment_name": OPENAPI_DEPLOYMENT_NAME,
        "container_name": OPENAPI_CONTAINER_NAME,
        "namespace": namespace,
        "image": image,
        "image_repository": _image_repository(image),
        "image_tag": _image_tag(image),
        "manifest_revision": manifest_revision,
        "expected_manifest_revision": OPENAPI_MANIFEST_REVISION,
        # Only meaningful when the Deployment actually exists; an absent
        # Deployment raises 404 upstream before reaching here.
        "manifest_outdated": bool(image) and manifest_outdated,
    }

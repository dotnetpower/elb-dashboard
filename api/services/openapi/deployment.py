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

from api.services.k8s_monitoring import _get_k8s_session

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
    finally:
        session.close()

    return {
        "configured": bool(image),
        "deployment_name": OPENAPI_DEPLOYMENT_NAME,
        "container_name": OPENAPI_CONTAINER_NAME,
        "namespace": namespace,
        "image": image,
        "image_repository": _image_repository(image),
        "image_tag": _image_tag(image),
    }

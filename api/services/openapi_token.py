"""OpenAPI API token lifecycle helpers.

Responsibility: Generate, read, and apply the sibling OpenAPI API token
Edit boundaries: Keep Kubernetes token storage and runtime cache synchronization here; routes
should only validate HTTP input and shape responses.
Key entry points: `get_openapi_api_token_status`, `ensure_openapi_api_token`
Risky contracts: Never log token values; keep tokens in server-side env/runtime cache and only
return them to authenticated dashboard callers.
Validation: `uv run pytest -q api/tests/test_openapi_token.py`.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s_monitoring import _get_k8s_session
from api.services.openapi_runtime import save_openapi_api_token

OPENAPI_DEPLOYMENT_NAME = "elb-openapi"
OPENAPI_CONTAINER_NAME = "openapi"
OPENAPI_TOKEN_ENV = "ELB_OPENAPI_API_TOKEN"  # noqa: S105 - env var name, not a token value.
K8S_NAMESPACE = "default"


@dataclass(frozen=True)
class OpenApiTokenError(Exception):
    status_code: int
    code: str
    message: str


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return "*" * len(token)
    return f"{token[:4]}{'*' * 12}{token[-6:]}"


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


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
        raise OpenApiTokenError(
            404,
            "openapi_deployment_not_found",
            "The elb-openapi deployment was not found in AKS.",
        )
    if response.status_code != 200:
        raise OpenApiTokenError(
            502,
            "openapi_deployment_unavailable",
            f"Kubernetes returned HTTP {response.status_code} while reading elb-openapi.",
        )
    data = response.json()
    return data if isinstance(data, dict) else {}


def _container_env_value(deployment: dict[str, Any], container_name: str, env_name: str) -> str:
    containers = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
        or []
    )
    for container in containers:
        if container.get("name") != container_name:
            continue
        for env in container.get("env", []) or []:
            if env.get("name") == env_name and env.get("value"):
                return str(env["value"]).strip()
    return ""


def _patch_deployment_token(
    session: Any,
    server: str,
    *,
    namespace: str,
    deployment_name: str,
    container_name: str,
    token: str,
) -> None:
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "elb-dashboard/openapi-api-token-rotated-at": _now_iso(),
                    },
                },
                "spec": {
                    "containers": [
                        {
                            "name": container_name,
                            "env": [{"name": OPENAPI_TOKEN_ENV, "value": token}],
                        }
                    ]
                },
            }
        }
    }
    response = session.patch(
        _deployment_url(server, namespace, deployment_name),
        json=patch,
        headers={"Content-Type": "application/strategic-merge-patch+json"},
        timeout=15,
    )
    if response.status_code == 404:
        raise OpenApiTokenError(
            404,
            "openapi_deployment_not_found",
            "The elb-openapi deployment was not found in AKS.",
        )
    if response.status_code not in {200, 201, 202}:
        raise OpenApiTokenError(
            502,
            "openapi_token_patch_failed",
            f"Kubernetes returned HTTP {response.status_code} while updating the API token.",
        )


def _status_payload(
    *,
    token: str,
    source: str,
    updated_at: str | None = None,
    generated: bool = False,
    rotated: bool = False,
) -> dict[str, Any]:
    return {
        "configured": bool(token),
        "token": token,
        "masked_token": _mask_token(token),
        "header_name": "X-ELB-API-Token",
        "env_name": OPENAPI_TOKEN_ENV,
        "source": source,
        "updated_at": updated_at,
        "generated": generated,
        "rotated": rotated,
    }


def _sync_runtime_token(token: str, metadata: dict[str, Any]) -> None:
    if not token:
        return
    os.environ[OPENAPI_TOKEN_ENV] = token
    save_openapi_api_token(token, metadata=metadata)


def get_openapi_api_token_status(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str = K8S_NAMESPACE,
) -> dict[str, Any]:
    """Return the current OpenAPI API token status and value."""

    session, server = _get_k8s_session(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        admin=True,
    )
    try:
        deployment = _read_deployment(session, server, namespace, OPENAPI_DEPLOYMENT_NAME)
        token = _container_env_value(deployment, OPENAPI_CONTAINER_NAME, OPENAPI_TOKEN_ENV)
    finally:
        session.close()

    metadata = {
        "subscription_id": subscription_id,
        "resource_group": resource_group,
        "cluster_name": cluster_name,
        "deployment_name": OPENAPI_DEPLOYMENT_NAME,
        "namespace": namespace,
    }
    _sync_runtime_token(token, metadata)
    return _status_payload(token=token, source="deployment_env")


def ensure_openapi_api_token(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    regenerate: bool,
    namespace: str = K8S_NAMESPACE,
) -> dict[str, Any]:
    """Create or rotate the OpenAPI API token on the AKS deployment."""

    session, server = _get_k8s_session(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        admin=True,
    )
    try:
        deployment = _read_deployment(session, server, namespace, OPENAPI_DEPLOYMENT_NAME)
        existing = _container_env_value(deployment, OPENAPI_CONTAINER_NAME, OPENAPI_TOKEN_ENV)
        if existing and not regenerate:
            token = existing
            generated = False
            rotated = False
            updated_at = None
        else:
            token = _generate_token()
            _patch_deployment_token(
                session,
                server,
                namespace=namespace,
                deployment_name=OPENAPI_DEPLOYMENT_NAME,
                container_name=OPENAPI_CONTAINER_NAME,
                token=token,
            )
            generated = not existing
            rotated = bool(existing)
            updated_at = _now_iso()
    finally:
        session.close()

    metadata = {
        "subscription_id": subscription_id,
        "resource_group": resource_group,
        "cluster_name": cluster_name,
        "deployment_name": OPENAPI_DEPLOYMENT_NAME,
        "namespace": namespace,
        "rotated": rotated,
        "generated": generated,
    }
    _sync_runtime_token(token, metadata)
    return _status_payload(
        token=token,
        source="deployment_env",
        updated_at=updated_at,
        generated=generated,
        rotated=rotated,
    )

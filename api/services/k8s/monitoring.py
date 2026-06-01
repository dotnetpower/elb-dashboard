"""Direct Kubernetes API helpers for AKS-backed ElasticBLAST monitoring.

Responsibility: Direct Kubernetes API helpers for AKS-backed ElasticBLAST monitoring
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `reset_k8s_credential_cache`, `_get_k8s_session`,
`_get_k8s_credential_material`, `k8s_ensure_job_manifests`,
`k8s_ensure_warmup_scripts_configmap`, `k8s_check_blast_status`
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s import credentials as _credentials

# Blast search status / cancellation and warmup inspection were split into
# sibling modules for SRP. They are re-exported here so existing importers of
# `api.services.k8s.monitoring` keep working. These modules import the session
# seams from this module lazily (inside function bodies), so there is no
# circular import at module load time.
from api.services.k8s.blast_status import (
    _reset_blast_status_cache,
    k8s_cancel_blast_job,
    k8s_check_blast_status,
)
from api.services.k8s.manifests import (
    _ensure_job_manifests,
    k8s_ensure_job_manifests,
    k8s_ensure_warmup_scripts_configmap,
)
from api.services.k8s.metrics import k8s_top_nodes
from api.services.k8s.nodes import (
    _candidate_warmup_node_names,
    k8s_get_nodes,
    k8s_ready_warmup_node_names,
)
from api.services.k8s.observability import (
    SYSTEM_NAMESPACES,
    k8s_list_events,
    k8s_pod_delete,
    k8s_pod_describe,
    k8s_pod_logs,
)
from api.services.k8s.warmup_status import (
    k8s_check_namespace_exists,
    k8s_release_stale_warmup_jobs,
    k8s_release_warmup_cache,
    k8s_warmup_status,
)

LOGGER = logging.getLogger(__name__)
aks_client = _credentials.aks_client
reset_k8s_session_pool = _credentials.reset_k8s_session_pool

_K8S_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
_SAFE_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

__all__ = [
    "SYSTEM_NAMESPACES",
    "_candidate_warmup_node_names",
    "_ensure_job_manifests",
    "_get_k8s_credential_material",
    "_get_k8s_session",
    "aks_client",
    "k8s_cancel_blast_job",
    "k8s_check_blast_status",
    "k8s_check_namespace_exists",
    "k8s_ensure_job_manifests",
    "k8s_ensure_warmup_scripts_configmap",
    "k8s_get_deployment_env_value",
    "k8s_get_nodes",
    "k8s_get_pods",
    "k8s_get_service_ip",
    "k8s_list_events",
    "k8s_pod_delete",
    "k8s_pod_describe",
    "k8s_pod_logs",
    "k8s_ready_warmup_node_names",
    "k8s_release_stale_warmup_jobs",
    "k8s_release_warmup_cache",
    "k8s_top_nodes",
    "k8s_warmup_status",
    "reset_k8s_credential_cache",
    "reset_k8s_session_pool",
]


def _get_k8s_session(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool = False,
) -> tuple[Any, str]:
    original = _credentials.aks_client
    _credentials.aks_client = aks_client
    try:
        return _credentials._get_k8s_session(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            admin=admin,
        )
    finally:
        _credentials.aks_client = original


def _get_k8s_credential_material(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool,
) -> Any:
    original = _credentials.aks_client
    _credentials.aks_client = aks_client
    try:
        return _credentials._get_k8s_credential_material(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            admin=admin,
        )
    finally:
        _credentials.aks_client = original


def reset_k8s_credential_cache() -> None:
    _credentials.reset_k8s_credential_cache()
    _reset_blast_status_cache()


def _namespace_or_default(session: Any, server: str, namespace: str) -> str:
    response = session.get(f"{server}/api/v1/namespaces/{namespace}", timeout=10)
    return "default" if response.status_code == 404 else namespace


def k8s_get_service_ip(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    service_name: str,
    namespace: str = "default",
) -> str | None:
    """Return the external IP of a Kubernetes LoadBalancer service."""

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(
            f"{server}/api/v1/namespaces/{namespace}/services/{service_name}",
            timeout=10,
        )
        if response.status_code != 200:
            return None
        ingress = response.json().get("status", {}).get("loadBalancer", {}).get("ingress", [])
        return ingress[0].get("ip") if ingress else None
    except Exception:
        return None
    finally:
        session.close()


def k8s_get_deployment_ready_replicas(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    deployment_name: str,
    namespace: str = "default",
) -> tuple[int, int]:
    """Return ``(ready_replicas, desired_replicas)`` for a Deployment.

    Returns ``(0, 0)`` when the Deployment is missing or unreachable. Used by
    the OpenAPI deploy task to detect "Service has an IP but no pod actually
    Ready" failure modes (ImagePullBackOff, RBAC denying the SA, taint
    mismatch …) so the task can mark itself ``failed`` instead of returning
    a misleading ``succeeded`` payload.
    """

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(
            f"{server}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}",
            timeout=10,
        )
        if response.status_code != 200:
            return (0, 0)
        body = response.json() if response.content else {}
        status = body.get("status", {}) or {}
        spec = body.get("spec", {}) or {}
        ready = int(status.get("readyReplicas") or 0)
        desired = int(spec.get("replicas") or 0)
        return (ready, desired)
    except Exception:
        return (0, 0)
    finally:
        session.close()


def k8s_get_deployment_env_value(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    deployment_name: str,
    env_name: str,
    namespace: str = "default",
    container_name: str | None = None,
) -> str | None:
    """Return a literal env value from a Kubernetes Deployment container."""

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(
            f"{server}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}",
            timeout=10,
        )
        if response.status_code != 200:
            return None
        containers = (
            response.json()
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        for container in containers:
            if container_name and container.get("name") != container_name:
                continue
            for env in container.get("env", []) or []:
                if env.get("name") == env_name and env.get("value"):
                    return str(env["value"])
        return None
    finally:
        session.close()


def k8s_get_pods(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    """Return non-succeeded pods via the Kubernetes API."""

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        url = (
            f"{server}/api/v1/pods"
            if not namespace
            else f"{server}/api/v1/namespaces/{namespace}/pods"
        )
        response = session.get(
            url,
            params={"fieldSelector": "status.phase!=Succeeded"},
            timeout=10,
        )
        response.raise_for_status()
        pods: list[dict[str, Any]] = []
        for item in response.json().get("items", []):
            meta = item.get("metadata", {})
            spec = item.get("spec", {})
            status = item.get("status", {})
            containers = status.get("containerStatuses", [])
            ready = sum(1 for container in containers if container.get("ready"))
            total = len(spec.get("containers", []))
            restarts = sum(container.get("restartCount", 0) for container in containers)
            pods.append(
                {
                    "namespace": meta.get("namespace", ""),
                    "name": meta.get("name", ""),
                    "ready": f"{ready}/{total}",
                    "status": status.get("phase", "Unknown"),
                    "restarts": restarts,
                    "age": meta.get("creationTimestamp", ""),
                    "node": spec.get("nodeName", ""),
                }
            )
        return pods
    finally:
        session.close()

"""Kubernetes manifest apply helpers for ElasticBLAST warmup and jobs.

Responsibility: Create/update Kubernetes Job manifests and the warmup scripts
ConfigMap through the direct Kubernetes API session.
Edit boundaries: Manifest application only. Status polling, pod/job lifecycle,
and warmup status logic live in `monitoring.py`.
Key entry points: `k8s_ensure_job_manifests`,
`k8s_ensure_warmup_scripts_configmap`, `_ensure_configmap`, `_ensure_job_manifests`.
Risky contracts: Validate namespace/job names before creating Jobs; keep using
admin sessions for manifest writes.
Validation: `uv run pytest -q api/tests/test_k8s_release_stale_warmup_jobs.py`.
"""

from __future__ import annotations

import re
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.credentials import _get_k8s_session
from api.services.warmup.jobs import build_warmup_scripts_configmap

_SAFE_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def k8s_ensure_job_manifests(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create Kubernetes Jobs if missing, leaving existing Jobs untouched."""

    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    try:
        return _ensure_job_manifests(session, server, jobs)
    finally:
        session.close()

def k8s_ensure_warmup_scripts_configmap(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Create or update the ConfigMap mounted by warmup Jobs."""

    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    try:
        manifest = build_warmup_scripts_configmap()
        return _ensure_configmap(session, server, manifest)
    finally:
        session.close()


def _ensure_configmap(session: Any, server: str, manifest: dict[str, Any]) -> dict[str, Any]:
    metadata = manifest.get("metadata", {}) or {}
    namespace = str(metadata.get("namespace") or "default")
    name = str(metadata.get("name") or "")
    if not name:
        raise ValueError("configmap name is required")
    url = f"{server}/api/v1/namespaces/{namespace}/configmaps/{name}"
    response = session.get(url, timeout=10)
    if response.status_code == 404:
        create = session.post(
            f"{server}/api/v1/namespaces/{namespace}/configmaps",
            json=manifest,
            timeout=10,
        )
        if create.status_code not in {200, 201}:
            return {"status": "error", "name": name, "status_code": create.status_code}
        return {"status": "created", "name": name}
    if response.status_code != 200:
        return {"status": "error", "name": name, "status_code": response.status_code}

    existing = response.json()
    if existing.get("data") == manifest.get("data"):
        return {"status": "unchanged", "name": name}
    manifest = {
        **manifest,
        "metadata": {
            **metadata,
            "resourceVersion": existing.get("metadata", {}).get("resourceVersion"),
        },
    }
    update = session.put(url, json=manifest, timeout=10)
    if update.status_code not in {200, 201}:
        return {"status": "error", "name": name, "status_code": update.status_code}
    return {"status": "updated", "name": name}


def _ensure_job_manifests(session: Any, server: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    created: list[str] = []
    existing: list[str] = []
    errors: list[dict[str, Any]] = []
    for job in jobs:
        metadata = job.get("metadata", {}) or {}
        namespace = str(metadata.get("namespace") or "default")
        name = str(metadata.get("name") or "")
        if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(name):
            errors.append({"name": name, "namespace": namespace, "error": "invalid job identity"})
            continue

        url = f"{server}/apis/batch/v1/namespaces/{namespace}/jobs"
        get_response = session.get(f"{url}/{name}", timeout=10)
        if get_response.status_code == 200:
            existing.append(name)
            continue
        if get_response.status_code not in (404,):
            errors.append(
                {
                    "name": name,
                    "namespace": namespace,
                    "status_code": get_response.status_code,
                    "error": get_response.text[:300],
                }
            )
            continue

        create_response = session.post(url, json=job, timeout=10)
        if create_response.status_code in (200, 201, 202):
            created.append(name)
        elif create_response.status_code == 409:
            existing.append(name)
        else:
            errors.append(
                {
                    "name": name,
                    "namespace": namespace,
                    "status_code": create_response.status_code,
                    "error": create_response.text[:300],
                }
            )
    return {
        "created": created,
        "existing": existing,
        "errors": errors,
        "created_count": len(created),
        "existing_count": len(existing),
        "error_count": len(errors),
    }

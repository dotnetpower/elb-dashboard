"""Direct Kubernetes API helpers for AKS-backed ElasticBLAST monitoring.

This module owns kubeconfig parsing, short-lived credential material, and all
read-only or narrowly-scoped Kubernetes API calls. ARM, Storage, ACR, and VM
helpers intentionally stay in ``api.services.monitoring``.

Do not reintroduce AKS Run Command here. Add another direct ``k8s_*`` helper
for Kubernetes API reads, or use ``api.services.terminal_exec`` for genuinely
shell-only tooling that must run inside the terminal sidecar.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import yaml  # type: ignore[import-untyped]
from azure.core.credentials import TokenCredential

from api.services.azure_clients import aks_client
from api.services.warmup_jobs import (
    DEFAULT_WARMUP_APP_LABEL,
    attach_pod_progress_to_database_status,
    build_warmup_scripts_configmap,
    database_status_from_warmup_jobs,
)

LOGGER = logging.getLogger(__name__)

_AKS_SERVER_APP_ID = "6dae42f8-4368-4678-94ff-3960e28e3630"
_K8S_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
_SAFE_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_K8S_CREDENTIAL_CACHE_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class _K8sCredentialMaterial:
    server: str
    ca_data: bytes | None
    client_cert: bytes | None
    client_key: bytes | None
    expires_at: float


_K8S_CREDENTIAL_CACHE: dict[tuple[str, str, str, bool], _K8sCredentialMaterial] = {}
_K8S_CREDENTIAL_CACHE_LOCK = threading.Lock()


def reset_k8s_credential_cache() -> None:
    """Clear cached AKS kubeconfig material. Test-only."""
    with _K8S_CREDENTIAL_CACHE_LOCK:
        _K8S_CREDENTIAL_CACHE.clear()


def _k8s_credential_cache_ttl() -> float:
    raw = os.environ.get("K8S_CREDENTIAL_CACHE_TTL_SECONDS", "")
    if raw:
        try:
            return max(0.0, min(float(raw), 3600.0))
        except ValueError:
            return _K8S_CREDENTIAL_CACHE_TTL_SECONDS
    return _K8S_CREDENTIAL_CACHE_TTL_SECONDS


def _get_k8s_credential_material(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool,
) -> _K8sCredentialMaterial:
    cache_key = (subscription_id, resource_group, cluster_name, admin)
    now = time.monotonic()
    with _K8S_CREDENTIAL_CACHE_LOCK:
        cached = _K8S_CREDENTIAL_CACHE.get(cache_key)
    if cached is not None and cached.expires_at > now:
        return cached

    client = aks_client(credential, subscription_id)
    if admin:
        creds = client.managed_clusters.list_cluster_admin_credentials(
            resource_group,
            cluster_name,
        )
    else:
        creds = client.managed_clusters.list_cluster_user_credentials(
            resource_group,
            cluster_name,
        )
    kubeconfig_bytes = creds.kubeconfigs[0].value
    kubeconfig = yaml.safe_load(bytes(kubeconfig_bytes))

    cluster_info = kubeconfig["clusters"][0]["cluster"]
    user_info = kubeconfig["users"][0]["user"]
    ca_data = cluster_info.get("certificate-authority-data", "")
    client_cert = user_info.get("client-certificate-data")
    client_key = user_info.get("client-key-data")

    material = _K8sCredentialMaterial(
        server=cluster_info["server"],
        ca_data=base64.b64decode(ca_data) if ca_data else None,
        client_cert=base64.b64decode(client_cert) if client_cert else None,
        client_key=base64.b64decode(client_key) if client_key else None,
        expires_at=now + _k8s_credential_cache_ttl(),
    )
    if material.expires_at > now:
        with _K8S_CREDENTIAL_CACHE_LOCK:
            _K8S_CREDENTIAL_CACHE[cache_key] = material
    return material


def _get_k8s_session(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool = False,
) -> tuple[Any, str]:
    """Return ``(requests.Session, server_url)`` for direct K8s API calls.

    The session owns any temporary CA/client-cert files and deletes them when
    ``session.close()`` is called. Temp files are also cleaned up on partial
    setup failure so credential material never lingers after an exception.
    """

    import requests as _requests

    material = _get_k8s_credential_material(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        admin=admin,
    )

    session = _requests.Session()
    temp_files: list[str] = []

    def cleanup_temp_files() -> None:
        for path in temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass

    def write_secret_file(suffix: str, content: bytes) -> str:
        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            handle.write(content)
            handle.flush()
        finally:
            handle.close()
        os.chmod(handle.name, 0o600)
        temp_files.append(handle.name)
        return handle.name

    try:
        if material.ca_data:
            session.verify = write_secret_file(".crt", material.ca_data)
        else:
            session.verify = True

        if material.client_cert and material.client_key:
            cert_path = write_secret_file(".crt", material.client_cert)
            key_path = write_secret_file(".key", material.client_key)
            session.cert = (cert_path, key_path)
        else:
            token = credential.get_token(f"{_AKS_SERVER_APP_ID}/.default")
            session.headers["Authorization"] = f"Bearer {token.token}"
    except Exception:
        cleanup_temp_files()
        try:
            session.close()
        except Exception:  # noqa: S110 - session close failures are non-actionable here
            pass
        raise

    original_close = session.close

    def cleanup_close() -> None:
        try:
            original_close()
        finally:
            cleanup_temp_files()

    session.close = cleanup_close  # type: ignore[assignment]
    return session, material.server


def _namespace_or_default(session: Any, server: str, namespace: str) -> str:
    response = session.get(f"{server}/api/v1/namespaces/{namespace}", timeout=10)
    return "default" if response.status_code == 404 else namespace


def _pod_has_env_value(pod: dict[str, Any], name: str, value: str) -> bool:
    for container in pod.get("spec", {}).get("containers", []) or []:
        for env in container.get("env", []) or []:
            if env.get("name") == name and env.get("value") == value:
                return True
    return False


def _owned_job_names(pods: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for pod in pods:
        for owner in pod.get("metadata", {}).get("ownerReferences", []) or []:
            if owner.get("kind") == "Job" and owner.get("name"):
                names.add(owner["name"])
    return names


def k8s_get_nodes(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> list[dict[str, Any]]:
    """Return cluster nodes from the Kubernetes API."""

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(f"{server}/api/v1/nodes", timeout=10)
        response.raise_for_status()
        nodes: list[dict[str, Any]] = []
        for item in response.json().get("items", []):
            meta = item.get("metadata", {})
            status = item.get("status", {})
            conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
            info = status.get("nodeInfo", {})
            addresses = {a["type"]: a["address"] for a in status.get("addresses", [])}
            roles = (
                ",".join(
                    key.replace("node-role.kubernetes.io/", "")
                    for key in meta.get("labels", {})
                    if key.startswith("node-role.kubernetes.io/")
                )
                or "<none>"
            )
            nodes.append(
                {
                    "name": meta.get("name", ""),
                    "status": "Ready" if conditions.get("Ready") == "True" else "NotReady",
                    "roles": roles,
                    "age": meta.get("creationTimestamp", ""),
                    "version": info.get("kubeletVersion", ""),
                    "internal_ip": addresses.get("InternalIP", ""),
                    "os_image": info.get("osImage", ""),
                    "kernel": info.get("kernelVersion", ""),
                    "runtime": info.get("containerRuntimeVersion", ""),
                }
            )
        return nodes
    finally:
        session.close()


def k8s_ready_warmup_node_names(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    preferred_pool: str = "blastpool",
) -> list[str]:
    """Return Ready node names suitable for node-local DB warmup Jobs."""

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(f"{server}/api/v1/nodes", timeout=10)
        response.raise_for_status()
        return _candidate_warmup_node_names(
            response.json().get("items", []), preferred_pool=preferred_pool
        )
    finally:
        session.close()


def _candidate_warmup_node_names(
    nodes: list[dict[str, Any]], *, preferred_pool: str = "blastpool"
) -> list[str]:
    candidates: list[tuple[str, str, str]] = []
    for node in nodes:
        metadata = node.get("metadata", {}) or {}
        spec = node.get("spec", {}) or {}
        status = node.get("status", {}) or {}
        name = str(metadata.get("name") or "")
        if not name or spec.get("unschedulable") is True:
            continue
        conditions = {
            item.get("type"): item.get("status")
            for item in status.get("conditions", []) or []
            if isinstance(item, dict)
        }
        if conditions.get("Ready") != "True":
            continue
        labels = metadata.get("labels", {}) or {}
        pool = str(labels.get("agentpool") or labels.get("kubernetes.azure.com/agentpool") or "")
        mode = str(labels.get("kubernetes.azure.com/mode") or "")
        candidates.append((name, pool, mode))

    preferred = [name for name, pool, _mode in candidates if pool == preferred_pool]
    if preferred:
        return sorted(preferred)

    user_nodes = [
        name
        for name, pool, mode in candidates
        if mode.lower() != "system" and pool.lower() not in {"system", "systempool"}
    ]
    if user_nodes:
        return sorted(user_nodes)
    return sorted(name for name, _pool, _mode in candidates)


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


def k8s_check_blast_status(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Return ElasticBLAST search status scoped by ``BLAST_ELB_JOB_ID``.

    Empty ``app=blast`` Jobs/Pods means the search has not been scheduled yet,
    so the honest status is ``creating`` rather than ``completed``.
    """

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        target_ns = _namespace_or_default(session, server, namespace)
        pods_response = session.get(
            f"{server}/api/v1/namespaces/{target_ns}/pods",
            params={"labelSelector": "app=blast"},
            timeout=10,
        )
        if pods_response.status_code != 200:
            return {
                "status": "unknown",
                "pods": 0,
                "detail": f"pods API error: {pods_response.status_code}",
            }

        all_pods = pods_response.json().get("items", [])
        blast_pods = (
            [pod for pod in all_pods if _pod_has_env_value(pod, "BLAST_ELB_JOB_ID", job_id)]
            if job_id
            else all_pods
        )

        jobs_response = session.get(
            f"{server}/apis/batch/v1/namespaces/{target_ns}/jobs",
            params={"labelSelector": "app=blast"},
            timeout=10,
        )
        if jobs_response.status_code != 200:
            return {
                "status": "unknown",
                "pods": len(blast_pods),
                "detail": f"jobs API error: {jobs_response.status_code}",
            }

        all_jobs = jobs_response.json().get("items", [])
        if job_id and blast_pods:
            scoped_names = _owned_job_names(blast_pods)
            jobs = [job for job in all_jobs if job.get("metadata", {}).get("name") in scoped_names]
        else:
            jobs = all_jobs

        if not jobs and not blast_pods:
            return {
                "status": "creating",
                "pods": 0,
                "jobs": 0,
                "detail": "no app=blast jobs/pods yet",
                "namespace": target_ns,
            }

        succeeded = 0
        failed = 0
        active = 0
        for job in jobs:
            job_status = job.get("status", {})
            succeeded += job_status.get("succeeded", 0)
            failed += job_status.get("failed", 0)
            active += job_status.get("active", 0)

        if failed > 0:
            blast_status = "failed"
        elif active > 0:
            blast_status = "running"
        elif succeeded > 0 and succeeded >= len(jobs):
            blast_status = "completed"
        else:
            blast_status = "creating"

        return {
            "status": blast_status,
            "pods": len(blast_pods),
            "jobs": len(jobs),
            "succeeded": succeeded,
            "failed": failed,
            "active": active,
            "namespace": target_ns,
            "scoped_by_job_id": bool(job_id),
        }
    except Exception as exc:
        return {"status": "unknown", "pods": 0, "detail": str(exc)[:200]}
    finally:
        session.close()


def k8s_cancel_blast_job(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    job_id: str,
) -> dict[str, Any]:
    """Delete this submission's Kubernetes Jobs by ``elb-job-id`` label."""

    if not _K8S_LABEL_VALUE_RE.match(job_id):
        raise ValueError("job_id is not a valid Kubernetes label value")

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        target_ns = _namespace_or_default(session, server, namespace)
        deleted: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for app in ("blast", "submit"):
            selector = f"app={app},elb-job-id={job_id}"
            response = session.delete(
                f"{server}/apis/batch/v1/namespaces/{target_ns}/jobs",
                params={"labelSelector": selector, "propagationPolicy": "Background"},
                timeout=10,
            )
            item = {"app": app, "selector": selector, "status_code": response.status_code}
            if response.status_code in (200, 201, 202, 404):
                deleted.append(item)
            else:
                errors.append({**item, "detail": response.text[:200]})

        return {
            "status": "cancelled" if not errors else "unknown",
            "namespace": target_ns,
            "job_id": job_id,
            "deleted": deleted,
            "errors": errors,
        }
    finally:
        session.close()


def k8s_release_warmup_cache(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    db_name: str,
    namespace: str = "default",
) -> dict[str, Any]:
    """Release node-local warmup resources for one database.

    The operation removes the Kubernetes resources that keep the dashboard's
    warm-cache state alive. Node-local kernel/page cache may drain gradually,
    but subsequent status checks no longer report the DB as warmed.
    """

    db_label = _warmup_db_label_value(db_name)
    if not _K8S_LABEL_VALUE_RE.match(db_label):
        raise ValueError("db_name is not a valid Kubernetes label value")

    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    try:
        target_ns = _namespace_or_default(session, server, namespace)
        deleted: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        targets = [
            (
                "jobs",
                f"{server}/apis/batch/v1/namespaces/{target_ns}/jobs",
                f"app={DEFAULT_WARMUP_APP_LABEL},db={db_label}",
            ),
            (
                "legacy-daemonsets",
                f"{server}/apis/apps/v1/namespaces/{target_ns}/daemonsets",
                f"app=db-warmup,db={db_label}",
            ),
        ]

        for kind, url, selector in targets:
            response = session.delete(
                url,
                params={"labelSelector": selector, "propagationPolicy": "Background"},
                timeout=10,
            )
            item = {"kind": kind, "selector": selector, "status_code": response.status_code}
            if response.status_code in (200, 201, 202, 404):
                deleted.append(item)
            else:
                errors.append({**item, "detail": response.text[:200]})

        return {
            "status": "released" if not errors else "partial",
            "database": db_name,
            "db_label": db_label,
            "namespace": target_ns,
            "deleted": deleted,
            "errors": errors,
        }
    finally:
        session.close()


def k8s_release_stale_warmup_jobs(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    db_name: str,
    current_node_names: Iterable[str],
    namespace: str = "default",
) -> dict[str, Any]:
    """Delete warmup Jobs (and their pods) pinned to nodes no longer present.

    ``Job.spec.template.spec.nodeName`` is immutable, so when AKS stop/start
    rotates VMSS instances the dashboard's previously-succeeded warmup Jobs
    cannot run again on the replacement nodes — they sit at ``succeeded=1``
    forever while ``_mark_stale_warmup_nodes`` correctly flags the DB as
    ``Stale``. Re-running ``k8s_ensure_job_manifests`` won't help either,
    because the existing Job names collide and ensure skips them.

    This helper finds Jobs labelled ``app=db-warmup, db=<name>`` whose pinned
    ``nodeName`` is not in ``current_node_names`` and deletes them with
    ``propagationPolicy=Background`` so the pods clean up too. The next
    ``k8s_ensure_job_manifests`` call will then recreate fresh Jobs on the
    current ready nodes.
    """

    db_label = _warmup_db_label_value(db_name)
    if not _K8S_LABEL_VALUE_RE.match(db_label):
        raise ValueError("db_name is not a valid Kubernetes label value")

    live_nodes = {str(name) for name in current_node_names if name}

    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    try:
        target_ns = _namespace_or_default(session, server, namespace)
        list_url = f"{server}/apis/batch/v1/namespaces/{target_ns}/jobs"
        response = session.get(
            list_url,
            params={"labelSelector": f"app={DEFAULT_WARMUP_APP_LABEL},db={db_label}"},
            timeout=10,
        )
        if response.status_code != 200:
            return {
                "status": "error",
                "database": db_name,
                "namespace": target_ns,
                "status_code": response.status_code,
                "detail": response.text[:200],
            }

        deleted: list[dict[str, Any]] = []
        kept: list[str] = []
        errors: list[dict[str, Any]] = []
        for job in response.json().get("items", []):
            metadata = job.get("metadata", {}) or {}
            name = str(metadata.get("name") or "")
            if not name:
                continue
            pinned = (
                job.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("nodeName")
                or ""
            )
            if not pinned or str(pinned) in live_nodes:
                kept.append(name)
                continue
            del_response = session.delete(
                f"{list_url}/{name}",
                params={"propagationPolicy": "Background"},
                timeout=10,
            )
            if del_response.status_code in (200, 201, 202, 404):
                deleted.append({"name": name, "stale_node": str(pinned)})
            else:
                errors.append(
                    {
                        "name": name,
                        "stale_node": str(pinned),
                        "status_code": del_response.status_code,
                        "detail": del_response.text[:200],
                    }
                )

        return {
            "status": "released" if not errors else "partial",
            "database": db_name,
            "namespace": target_ns,
            "deleted": deleted,
            "kept": kept,
            "errors": errors,
        }
    finally:
        session.close()


def _warmup_db_label_value(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-_.")
    if not label:
        return "db"
    return label[:63].rstrip("-_.") or "db"


def k8s_check_namespace_exists(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
) -> bool:
    """Return whether ElasticBLAST warmup resources appear to exist."""

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(
            f"{server}/apis/apps/v1/namespaces/kube-system/daemonsets/create-workspace",
            timeout=10,
        )
        if response.status_code == 200:
            ready = response.json().get("status", {}).get("numberReady", 0)
            if ready > 0:
                return True

        response = session.get(f"{server}/api/v1/namespaces/default/pods", timeout=10)
        if response.status_code != 200:
            return False
        pods = response.json().get("items", [])
        return any(
            "vmtouch" in pod.get("metadata", {}).get("name", "")
            or "elb" in pod.get("metadata", {}).get("name", "")
            for pod in pods
        )
    except Exception:
        return False
    finally:
        session.close()


def k8s_warmup_status(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Detect warmup state by inspecting ElasticBLAST Kubernetes resources."""

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        result: dict[str, Any] = {
            "warm": False,
            "workspace_ready": 0,
            "workspace_desired": 0,
            "databases": [],
            "vmtouch_ready": 0,
            "namespaces": [],
        }

        response = session.get(
            f"{server}/apis/apps/v1/namespaces/kube-system/daemonsets/create-workspace",
            timeout=10,
        )
        if response.status_code == 200:
            status = response.json().get("status", {})
            result["workspace_ready"] = status.get("numberReady", 0)
            result["workspace_desired"] = status.get("desiredNumberScheduled", 0)
            result["warm"] = result["workspace_ready"] > 0

        response = session.get(
            f"{server}/apis/apps/v1/namespaces/default/daemonsets/vmtouch-db-cache",
            timeout=10,
        )
        if response.status_code == 200:
            result["vmtouch_ready"] = response.json().get("status", {}).get("numberReady", 0)
            result["warm"] = result["warm"] or result["vmtouch_ready"] > 0

        response = session.get(
            f"{server}/apis/batch/v1/namespaces/default/jobs",
            params={"labelSelector": "app=setup"},
            timeout=10,
        )
        if response.status_code == 200:
            result["databases"] = _database_status_from_setup_jobs(response.json().get("items", []))

        response = session.get(
            f"{server}/apis/batch/v1/namespaces/default/jobs",
            params={"labelSelector": f"app={DEFAULT_WARMUP_APP_LABEL}"},
            timeout=10,
        )
        if response.status_code == 200:
            warmup_databases = database_status_from_warmup_jobs(response.json().get("items", []))
            _mark_stale_warmup_nodes(session, server, warmup_databases)
            pods, logs_by_pod = _warmup_pods_and_logs(session, server)
            attach_pod_progress_to_database_status(warmup_databases, pods, logs_by_pod)
            _merge_database_statuses(result, warmup_databases)

        response = session.get(
            f"{server}/apis/apps/v1/namespaces/default/daemonsets",
            params={"labelSelector": "app=db-warmup"},
            timeout=10,
        )
        if response.status_code == 200:
            _append_warmup_daemonsets(result, response.json().get("items", []))

        response = session.get(f"{server}/api/v1/namespaces", timeout=10)
        if response.status_code == 200:
            result["namespaces"] = [
                namespace_item.get("metadata", {}).get("name", "")
                for namespace_item in response.json().get("items", [])
                if namespace_item.get("metadata", {}).get("name", "").startswith("elastic-blast-")
            ][:20]

        return result
    except Exception as exc:
        LOGGER.warning("k8s_warmup_status failed for %s: %s", cluster_name, str(exc)[:200])
        return {
            "warm": False,
            "workspace_ready": 0,
            "workspace_desired": 0,
            "databases": [],
            "vmtouch_ready": 0,
            "namespaces": [],
            "error": str(exc)[:200],
        }
    finally:
        session.close()


def _database_status_from_setup_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    db_map: dict[str, dict[str, Any]] = {}
    for job in jobs:
        job_name = job.get("metadata", {}).get("name", "")
        if not job_name.startswith("init-ssd-"):
            continue

        db_name = ""
        mol_type = ""
        containers = job.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            for env in container.get("env", []):
                if env.get("name") == "ELB_DB":
                    db_name = env.get("value", "")
                elif env.get("name") == "ELB_DB_MOL_TYPE":
                    mol_type = env.get("value", "")
        if not db_name:
            continue

        info = db_map.setdefault(
            db_name,
            {
                "name": db_name,
                "mol_type": mol_type,
                "nodes_ready": 0,
                "nodes_failed": 0,
                "nodes_active": 0,
                "total_jobs": 0,
            },
        )
        job_status = job.get("status", {})
        info["total_jobs"] += 1
        info["nodes_ready"] += job_status.get("succeeded", 0)
        info["nodes_failed"] += job_status.get("failed", 0)
        info["nodes_active"] += job_status.get("active", 0)

    for info in db_map.values():
        total = info["total_jobs"]
        if info["nodes_ready"] == total and total > 0:
            info["status"] = "Ready"
        elif info["nodes_active"] > 0:
            info["status"] = "Loading"
        elif info["nodes_failed"] > 0:
            info["status"] = "Failed"
        else:
            info["status"] = "Unknown"
    return list(db_map.values())


def _merge_database_statuses(result: dict[str, Any], incoming: list[dict[str, Any]]) -> None:
    existing = {database["name"]: database for database in result["databases"]}
    for database in incoming:
        name = database.get("name")
        if not name:
            continue
        if name in existing:
            current = existing[name]
            for key in ("nodes_ready", "nodes_failed", "nodes_active", "total_jobs"):
                current[key] = max(int(current.get(key) or 0), int(database.get(key) or 0))
            if database.get("status") == "Ready" or current.get("status") != "Ready":
                current["status"] = database.get("status", current.get("status", "Unknown"))
            if database.get("shards"):
                current["shards"] = sorted(
                    set(current.get("shards", [])) | set(database.get("shards", []))
                )
            for key in (
                "progress_pct",
                "started_at",
                "elapsed_seconds",
                "estimated_remaining_seconds",
                "active_phase",
                "active_phase_label",
                "phase_counts",
                "pod_statuses",
                "shard_nodes",
                "shard_host_paths",
            ):
                if key in database:
                    current[key] = database[key]
        else:
            result["databases"].append(database)
            existing[name] = database
        if database.get("status") == "Ready":
            result["warm"] = True


def _mark_stale_warmup_nodes(
    session: Any,
    server: str,
    databases: list[dict[str, Any]],
) -> None:
    response = session.get(f"{server}/api/v1/nodes", timeout=10)
    if response.status_code != 200:
        return
    ready_nodes = set(_candidate_warmup_node_names(response.json().get("items", [])))
    if not ready_nodes:
        return
    for database in databases:
        shard_nodes = database.get("shard_nodes") or {}
        if not isinstance(shard_nodes, dict):
            continue
        stale_shards = sorted(
            shard for shard, node_name in shard_nodes.items() if str(node_name) not in ready_nodes
        )
        if not stale_shards:
            continue
        database["status"] = "Stale"
        database["nodes_active"] = 0
        database["nodes_ready"] = 0
        database["nodes_failed"] = int(database.get("total_jobs") or len(stale_shards))
        database["stale_shards"] = stale_shards
        database["active_phase"] = "failed"
        database["active_phase_label"] = "Warmup stale"
        database["active_message"] = "Warmup jobs are pinned to nodes that are no longer Ready."


def _warmup_pods_and_logs(session: Any, server: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    response = session.get(
        f"{server}/api/v1/namespaces/default/pods",
        params={"labelSelector": f"app={DEFAULT_WARMUP_APP_LABEL}"},
        timeout=10,
    )
    if response.status_code != 200:
        return [], {}
    pods = response.json().get("items", [])
    logs_by_pod: dict[str, str] = {}
    for pod in pods[:12]:
        name = pod.get("metadata", {}).get("name", "")
        if not name:
            continue
        log_response = session.get(
            f"{server}/api/v1/namespaces/default/pods/{name}/log",
            params={"container": "warmup", "tailLines": 80},
            timeout=2,
        )
        if log_response.status_code == 200:
            logs_by_pod[name] = log_response.text[-8000:]
    return pods, logs_by_pod


def _append_warmup_daemonsets(result: dict[str, Any], daemonsets: list[dict[str, Any]]) -> None:
    existing_db_names = {database["name"] for database in result["databases"]}
    for daemonset in daemonsets:
        db_label = daemonset.get("metadata", {}).get("labels", {}).get("db", "")
        if not db_label or db_label in existing_db_names:
            continue
        status = daemonset.get("status", {})
        desired = status.get("desiredNumberScheduled", 0)
        ready = status.get("numberReady", 0)
        if desired == 0:
            continue
        result["databases"].append(
            {
                "name": db_label,
                "mol_type": "",
                "nodes_ready": ready,
                "nodes_failed": 0,
                "nodes_active": desired - ready,
                "total_jobs": desired,
                "status": "Ready" if ready == desired else "Loading",
            }
        )
        if ready > 0:
            result["warm"] = True


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


def k8s_top_nodes(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> list[dict[str, Any]]:
    """Return node resource usage from the Kubernetes metrics API.

    Each entry includes both raw values (``cpu_m``, ``mem_ki``,
    ``cpu_capacity_m``, ``mem_capacity_ki``) and pre-formatted strings so the
    SPA can humanize freely (e.g. ``0.10 / 32 cores``, ``1.2 / 252 GiB``)
    without re-parsing kubernetes-style quantities. Each entry also carries
    pool/Ready metadata pulled from the same ``/api/v1/nodes`` snapshot so
    the dashboard can colour rows by pool and flag NotReady nodes inline.
    """

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        capacity = _node_capacity_with_meta(session, server)
        response = session.get(f"{server}/apis/metrics.k8s.io/v1beta1/nodes", timeout=10)
        response.raise_for_status()
        nodes: list[dict[str, Any]] = []
        for item in response.json().get("items", []):
            name = item["metadata"]["name"]
            usage = item.get("usage", {})
            cpu_m = _parse_cpu_millicores(usage.get("cpu", "0"))
            mem_ki = _parse_memory_ki(usage.get("memory", "0"))
            meta = capacity.get(
                name,
                {
                    "cpu_m": 1,
                    "mem_ki": 1,
                    "pool": "",
                    "ready": True,
                    "conditions": {},
                },
            )
            cpu_cap = meta.get("cpu_m") or 1
            mem_cap = meta.get("mem_ki") or 1
            nodes.append(
                {
                    "name": name,
                    "cpu": f"{cpu_m}m",
                    "cpu_pct": round(cpu_m / cpu_cap * 100) if cpu_cap else 0,
                    "memory": f"{mem_ki // 1024}Mi",
                    "memory_pct": round(mem_ki / mem_cap * 100) if mem_cap else 0,
                    "memory_total": f"{mem_cap // 1024}Mi",
                    # Raw numbers for client-side humanization.
                    "cpu_m": cpu_m,
                    "mem_ki": mem_ki,
                    "cpu_capacity_m": cpu_cap,
                    "mem_capacity_ki": mem_cap,
                    # Pool / health metadata.
                    "pool": meta.get("pool", ""),
                    "ready": bool(meta.get("ready", True)),
                    "conditions": meta.get("conditions", {}),
                }
            )
        return nodes
    finally:
        session.close()


def _node_capacity(session: Any, server: str) -> dict[str, dict[str, int]]:
    response = session.get(f"{server}/api/v1/nodes", timeout=10)
    response.raise_for_status()
    capacity: dict[str, dict[str, int]] = {}
    for item in response.json().get("items", []):
        name = item["metadata"]["name"]
        cap = item.get("status", {}).get("capacity", {})
        capacity[name] = {
            "cpu_m": _parse_cpu_millicores(cap.get("cpu", "0")),
            "mem_ki": _parse_memory_ki(cap.get("memory", "0")),
        }
    return capacity


def _node_capacity_with_meta(session: Any, server: str) -> dict[str, dict[str, Any]]:
    """Like ``_node_capacity`` but also returns pool / Ready / conditions.

    A single ``/api/v1/nodes`` GET feeds both capacity and metadata so we do
    not double-roundtrip kube-apiserver from ``k8s_top_nodes``.
    """
    response = session.get(f"{server}/api/v1/nodes", timeout=10)
    response.raise_for_status()
    out: dict[str, dict[str, Any]] = {}
    for item in response.json().get("items", []):
        meta = item.get("metadata", {})
        labels = meta.get("labels", {}) or {}
        status = item.get("status", {})
        cap = status.get("capacity", {})
        # AKS labels system / user pools with ``agentpool=<name>``.
        pool = labels.get("agentpool") or labels.get("kubernetes.azure.com/agentpool") or ""
        # Ready + pressure flags from the conditions array.
        ready = False
        conditions: dict[str, str] = {}
        for cond in status.get("conditions", []) or []:
            ctype = cond.get("type", "")
            cstatus = cond.get("status", "")
            if not ctype:
                continue
            conditions[ctype] = cstatus
            if ctype == "Ready":
                ready = cstatus == "True"
        out[meta.get("name", "")] = {
            "cpu_m": _parse_cpu_millicores(cap.get("cpu", "0")),
            "mem_ki": _parse_memory_ki(cap.get("memory", "0")),
            "pool": pool,
            "ready": ready,
            "conditions": conditions,
        }
    return out


def _parse_cpu_millicores(raw: str) -> int:
    value = str(raw)
    if value.endswith("n"):
        return int(value[:-1]) // 1_000_000
    if value.endswith("m"):
        return int(value[:-1])
    return int(value) * 1000


def _parse_memory_ki(raw: str) -> int:
    value = str(raw)
    if value.endswith("Ki"):
        return int(value[:-2])
    if value.endswith("Mi"):
        return int(value[:-2]) * 1024
    if value.endswith("Gi"):
        return int(value[:-2]) * 1024 * 1024
    return int(value) // 1024


def k8s_pod_logs(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    pod_name: str,
    tail_lines: int = 200,
) -> str:
    """Return pod logs via the Kubernetes API."""

    if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(pod_name):
        raise ValueError("Invalid namespace or pod name")

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(
            f"{server}/api/v1/namespaces/{namespace}/pods/{pod_name}/log",
            params={"tailLines": tail_lines},
            timeout=15,
        )
        response.raise_for_status()
        return response.text
    finally:
        session.close()


def k8s_list_events(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Return recent k8s events sorted newest-first.

    `namespace=None` returns events across all namespaces (cluster-wide
    `/api/v1/events`).  Otherwise scoped to one namespace, which is
    validated against the same DNS-1123 regex as pod names.

    The output schema is intentionally flat and small — only the fields
    the dashboard's Live Activity rail consumes.  Free-form `message`
    is left as-is here; the caller is responsible for sanitising it
    before sending to the SPA (see `api.routes.monitor.aks_events`).
    """

    if namespace is not None and not _SAFE_K8S_NAME_RE.match(namespace):
        raise ValueError("Invalid namespace")
    if limit <= 0 or limit > 1000:
        raise ValueError("limit must be in (0, 1000]")

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        if namespace:
            url = f"{server}/api/v1/namespaces/{namespace}/events"
        else:
            url = f"{server}/api/v1/events"
        # We pull a generous slice (`limit*4` capped at 500) and sort
        # client-side because the K8s API doesn't reliably honour
        # ordering across paginated reads.  500 events is ~50 KiB JSON
        # which is well within budget for one dashboard tile.
        params = {"limit": min(500, max(limit * 4, 100))}
        response = session.get(url, params=params, timeout=10)
        response.raise_for_status()
        items = response.json().get("items", []) or []
    finally:
        session.close()

    out: list[dict[str, Any]] = []

    # Defence in depth: every free-form string we surface gets a length
    # cap.  Even though the route layer also runs `sanitise()` on
    # `message`, we cap *here* so a malformed event with multi-MB
    # fields can't bloat the JSON response between this helper and the
    # route.  Caps mirror what the dashboard can actually display
    # (Live Activity rail truncates to ~110 chars).
    def _capped(value: Any, limit: int) -> str:
        s = str(value or "")
        return s[:limit]

    for ev in items:
        if not isinstance(ev, dict):
            continue
        meta = ev.get("metadata", {}) if isinstance(ev.get("metadata"), dict) else {}
        involved = (
            ev.get("involvedObject", {}) if isinstance(ev.get("involvedObject"), dict) else {}
        )
        source = ev.get("source", {}) if isinstance(ev.get("source"), dict) else {}
        last_ts = (
            ev.get("lastTimestamp") or ev.get("eventTime") or meta.get("creationTimestamp") or ""
        )
        # `count` is sometimes a float in malformed payloads; coerce
        # safely and clamp to a sane upper bound to avoid surfacing
        # eye-watering "1.7M events" badges from a single misbehaving
        # controller.
        try:
            count_val = max(1, min(int(float(ev.get("count") or 1)), 1_000_000))
        except (TypeError, ValueError):
            count_val = 1
        # Type field is a closed enum in K8s — coerce anything else to
        # "Normal" so the frontend's classifier doesn't have to defend
        # against attacker-controlled severity strings.
        ev_type = str(ev.get("type") or "Normal")
        if ev_type not in ("Normal", "Warning"):
            ev_type = "Normal"
        out.append(
            {
                "namespace": _capped(meta.get("namespace") or involved.get("namespace"), 63),
                "name": _capped(meta.get("name"), 253),
                "type": ev_type,
                "reason": _capped(ev.get("reason"), 64),
                "message": _capped(ev.get("message"), 1024),
                "count": count_val,
                "last_timestamp": _capped(last_ts, 32),
                "involved_kind": _capped(involved.get("kind"), 64),
                "involved_name": _capped(involved.get("name"), 253),
                "source_component": _capped(source.get("component"), 64),
                "source_host": _capped(source.get("host"), 253),
            }
        )

    out.sort(key=lambda e: e.get("last_timestamp") or "", reverse=True)
    return out[:limit]

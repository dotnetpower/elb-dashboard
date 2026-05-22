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
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from azure.core.credentials import TokenCredential

from api.services.azure_clients import aks_client as aks_client
from api.services.k8s import client as _k8s_client
from api.services.k8s_metrics import k8s_top_nodes
from api.services.k8s_nodes import (
    _candidate_warmup_node_names,
    k8s_get_nodes,
    k8s_ready_warmup_node_names,
)
from api.services.k8s_observability import k8s_list_events, k8s_pod_logs
from api.services.k8s_timestamps import (
    k8s_timestamp_span_payload as _k8s_timestamp_span_payload,
)
from api.services.k8s_timestamps import (
    max_k8s_timestamp as _max_k8s_timestamp,
)
from api.services.k8s_timestamps import (
    min_k8s_timestamp as _min_k8s_timestamp,
)
from api.services.warmup_jobs import (
    DEFAULT_WARMUP_APP_LABEL,
    attach_pod_progress_to_database_status,
    build_warmup_scripts_configmap,
    database_status_from_warmup_jobs,
)

LOGGER = logging.getLogger(__name__)

_K8S_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
_SAFE_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

__all__ = [
    "_candidate_warmup_node_names",
    "_ensure_job_manifests",
    "_get_k8s_credential_material",
    "_get_k8s_session",
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
    "k8s_pod_logs",
    "k8s_ready_warmup_node_names",
    "k8s_release_stale_warmup_jobs",
    "k8s_release_warmup_cache",
    "k8s_top_nodes",
    "k8s_warmup_status",
    "reset_k8s_credential_cache",
    "reset_k8s_session_pool",
]


def reset_k8s_credential_cache() -> None:
    _k8s_client.reset_k8s_credential_cache()
    _reset_blast_status_cache()


def reset_k8s_session_pool() -> None:
    """Drop all pooled K8s sessions. Test-only re-export of the client helper."""
    _k8s_client.reset_k8s_session_pool()


# ---------------------------------------------------------------------------
# Short-TTL cache for cluster-wide app=blast pods/jobs lookups.
# ---------------------------------------------------------------------------
# ``k8s_check_blast_status`` performs two cluster-wide HTTP roundtrips
# (``GET .../pods?labelSelector=app=blast`` and ``.../jobs?labelSelector=app=blast``)
# before doing in-memory filtering for a specific ``job_id``. The BLAST jobs
# list endpoint calls this helper once per active row, so each browser poll
# pays N × 2 round-trips. A 3 s TTL memoisation reduces this to ~2 round-trips
# per browser poll regardless of how many active rows the user has, while
# keeping the freshness budget well under the frontend polling cadence.

_BLAST_STATUS_CACHE_TTL_SECONDS = 3.0
_BLAST_STATUS_CACHE: dict[tuple[str, str, str, str], tuple[float, dict[str, Any]]] = {}
_BLAST_STATUS_CACHE_LOCK = threading.Lock()


def _reset_blast_status_cache() -> None:
    with _BLAST_STATUS_CACHE_LOCK:
        _BLAST_STATUS_CACHE.clear()


def _fetch_blast_pods_and_jobs(
    session: Any,
    server: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
) -> dict[str, Any]:
    """Return ``{target_ns, all_pods, all_jobs}`` or ``{error: ...}`` for the cluster.

    Short-TTL memoised (~3 s) keyed by the cluster + namespace so multiple
    per-row calls during one ``/api/blast/jobs`` request share one set of
    HTTP roundtrips.
    """

    key = (subscription_id, resource_group, cluster_name, namespace)
    now = time.monotonic()
    with _BLAST_STATUS_CACHE_LOCK:
        cached = _BLAST_STATUS_CACHE.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

    target_ns = _namespace_or_default(session, server, namespace)
    pods_response = session.get(
        f"{server}/api/v1/namespaces/{target_ns}/pods",
        params={"labelSelector": "app=blast"},
        timeout=10,
    )
    if pods_response.status_code != 200:
        return {
            "error": f"pods API error: {pods_response.status_code}",
            "target_ns": target_ns,
        }
    all_pods = pods_response.json().get("items", [])

    jobs_response = session.get(
        f"{server}/apis/batch/v1/namespaces/{target_ns}/jobs",
        params={"labelSelector": "app=blast"},
        timeout=10,
    )
    if jobs_response.status_code != 200:
        return {
            "error": f"jobs API error: {jobs_response.status_code}",
            "target_ns": target_ns,
            "all_pods": all_pods,
        }
    all_jobs = jobs_response.json().get("items", [])

    result: dict[str, Any] = {
        "target_ns": target_ns,
        "all_pods": all_pods,
        "all_jobs": all_jobs,
    }
    with _BLAST_STATUS_CACHE_LOCK:
        _BLAST_STATUS_CACHE[key] = (now + _BLAST_STATUS_CACHE_TTL_SECONDS, result)
    return result


def _container_terminated_state(container_status: dict[str, Any]) -> dict[str, Any] | None:
    for state_key in ("state", "lastState"):
        state = container_status.get(state_key, {})
        if not isinstance(state, dict):
            continue
        terminated = state.get("terminated")
        if isinstance(terminated, dict):
            return terminated
    return None


def _get_k8s_session(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool = False,
) -> tuple[Any, str]:
    original = _k8s_client.aks_client
    _k8s_client.aks_client = aks_client
    try:
        return _k8s_client._get_k8s_session(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            admin=admin,
        )
    finally:
        _k8s_client.aks_client = original


def _get_k8s_credential_material(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool,
) -> Any:
    original = _k8s_client.aks_client
    _k8s_client.aks_client = aks_client
    try:
        return _k8s_client._get_k8s_credential_material(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            admin=admin,
        )
    finally:
        _k8s_client.aks_client = original


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


def _job_has_label_value(job: dict[str, Any], name: str, value: str) -> bool:
    return cast(bool, job.get("metadata", {}).get("labels", {}).get(name) == value)


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
    """Return ElasticBLAST search status scoped by ElasticBLAST's K8s job id.

    Empty ``app=blast`` Jobs/Pods means the search has not been scheduled yet,
    so the honest status is ``creating`` rather than ``completed``.
    """

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        snapshot = _fetch_blast_pods_and_jobs(
            session,
            server,
            subscription_id,
            resource_group,
            cluster_name,
            namespace,
        )
        target_ns = snapshot.get("target_ns") or namespace
        if "error" in snapshot:
            error = str(snapshot["error"])
            return {
                "status": "unknown",
                "pods": len(snapshot.get("all_pods") or []),
                "detail": error,
            }
        all_pods = snapshot.get("all_pods") or []
        blast_pods = (
            [pod for pod in all_pods if _pod_has_env_value(pod, "BLAST_ELB_JOB_ID", job_id)]
            if job_id
            else all_pods
        )

        all_jobs = snapshot.get("all_jobs") or []
        if job_id:
            scoped_names = _owned_job_names(blast_pods)
            jobs = [
                job
                for job in all_jobs
                if job.get("metadata", {}).get("name") in scoped_names
                or _job_has_label_value(job, "elb-job-id", job_id)
            ]
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
        started_at_values: list[str] = []
        completed_at_values: list[str] = []
        blast_container_started_at_values: list[str] = []
        blast_container_completed_at_values: list[str] = []
        results_export_container_started_at_values: list[str] = []
        results_export_container_completed_at_values: list[str] = []
        blast_container_count = 0
        results_export_container_count = 0
        for job in jobs:
            job_status = job.get("status", {})
            succeeded += job_status.get("succeeded", 0)
            failed += job_status.get("failed", 0)
            active += job_status.get("active", 0)
            if job_status.get("startTime"):
                started_at_values.append(str(job_status["startTime"]))
            if job_status.get("completionTime"):
                completed_at_values.append(str(job_status["completionTime"]))

        for pod in blast_pods:
            pod_status = pod.get("status", {})
            if pod_status.get("startTime"):
                started_at_values.append(str(pod_status["startTime"]))
            for container_status in pod_status.get("containerStatuses", []) or []:
                if not isinstance(container_status, dict):
                    continue
                terminated = _container_terminated_state(container_status)
                if terminated is None:
                    continue
                container_name = str(container_status.get("name") or "")
                if terminated.get("startedAt"):
                    started_at_values.append(str(terminated["startedAt"]))
                if terminated.get("finishedAt"):
                    completed_at_values.append(str(terminated["finishedAt"]))
                if container_name == "blast":
                    blast_container_count += 1
                    if terminated.get("startedAt"):
                        blast_container_started_at_values.append(str(terminated["startedAt"]))
                    if terminated.get("finishedAt"):
                        blast_container_completed_at_values.append(str(terminated["finishedAt"]))
                elif container_name == "results-export":
                    results_export_container_count += 1
                    if terminated.get("startedAt"):
                        results_export_container_started_at_values.append(
                            str(terminated["startedAt"])
                        )
                    if terminated.get("finishedAt"):
                        results_export_container_completed_at_values.append(
                            str(terminated["finishedAt"])
                        )

        if failed > 0:
            blast_status = "failed"
        elif active > 0:
            blast_status = "running"
        elif succeeded > 0 and succeeded >= len(jobs):
            blast_status = "completed"
        else:
            blast_status = "creating"

        started_at = _min_k8s_timestamp(started_at_values)
        completed_at = _max_k8s_timestamp(completed_at_values)
        result = {
            "status": blast_status,
            "job_id": job_id,
            "pods": len(blast_pods),
            "jobs": len(jobs),
            "succeeded": succeeded,
            "failed": failed,
            "active": active,
            "namespace": target_ns,
            "scoped_by_job_id": bool(job_id),
            "blast_container_count": blast_container_count,
            "results_export_container_count": results_export_container_count,
        }
        if started_at:
            result["started_at"] = started_at
        if completed_at:
            result["completed_at"] = completed_at
        result.update(
            _k8s_timestamp_span_payload(
                "blast_container",
                blast_container_started_at_values,
                blast_container_completed_at_values,
            )
        )
        result.update(
            _k8s_timestamp_span_payload(
                "results_export_container",
                results_export_container_started_at_values,
                results_export_container_completed_at_values,
            )
        )
        return result
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
    current_source_version: str = "",
) -> dict[str, Any]:
    """Delete warmup Jobs (and their pods) pinned to stale nodes or generations.

    ``Job.spec.template.spec.nodeName`` is immutable, so when AKS stop/start
    rotates VMSS instances the dashboard's previously-succeeded warmup Jobs
    cannot run again on the replacement nodes — they sit at ``succeeded=1``
    forever while ``_mark_stale_warmup_nodes`` correctly flags the DB as
    ``Stale``. Re-running ``k8s_ensure_job_manifests`` won't help either,
    because the existing Job names collide and ensure skips them.

    This helper finds Jobs labelled ``app=db-warmup, db=<name>`` whose pinned
    ``nodeName`` is not in ``current_node_names`` or whose source-version
    annotation does not match ``current_source_version`` and deletes them with
    ``propagationPolicy=Background`` so the pods clean up too. The next
    ``k8s_ensure_job_manifests`` call will then recreate fresh Jobs on the
    current ready nodes and DB generation.
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
            pinned = job.get("spec", {}).get("template", {}).get("spec", {}).get("nodeName") or ""
            metadata_annotations = metadata.get("annotations", {}) or {}
            template_metadata = job.get("spec", {}).get("template", {}).get("metadata", {}) or {}
            template_annotations = template_metadata.get("annotations", {}) or {}
            source_version = str(
                metadata_annotations.get("elb.dashboard/source-version")
                or template_annotations.get("elb.dashboard/source-version")
                or ""
            )
            source_stale = bool(current_source_version and source_version != current_source_version)
            node_stale = bool(pinned and str(pinned) not in live_nodes)
            if not node_stale and not source_stale:
                kept.append(name)
                continue
            del_response = session.delete(
                f"{list_url}/{name}",
                params={"propagationPolicy": "Background"},
                timeout=10,
            )
            if del_response.status_code in (200, 201, 202, 404):
                deleted.append(
                    {
                        "name": name,
                        "stale_node": str(pinned) if node_stale else "",
                        "stale_source_version": source_version if source_stale else "",
                        "current_source_version": current_source_version if source_stale else "",
                    }
                )
            else:
                errors.append(
                    {
                        "name": name,
                        "stale_node": str(pinned) if node_stale else "",
                        "stale_source_version": source_version if source_stale else "",
                        "current_source_version": current_source_version if source_stale else "",
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
    """Detect warmup state by inspecting ElasticBLAST Kubernetes resources.

    The six top-level GETs are independent and fan out via a thread pool so
    the total wall time is bounded by the slowest call instead of the sum
    of all calls. ``requests.Session`` is thread-safe for concurrent
    requests (it's just a connection pool + cookie jar — we don't mutate
    session state on these read paths).
    """

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

        # Phase 1 — fan out the six independent reads in parallel.
        def _get(url: str, params: dict[str, str] | None = None) -> Any:
            return session.get(url, params=params, timeout=10)

        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="warmup-status") as pool:
            f_workspace = pool.submit(
                _get,
                f"{server}/apis/apps/v1/namespaces/kube-system/daemonsets/create-workspace",
            )
            f_vmtouch = pool.submit(
                _get,
                f"{server}/apis/apps/v1/namespaces/default/daemonsets/vmtouch-db-cache",
            )
            f_setup_jobs = pool.submit(
                _get,
                f"{server}/apis/batch/v1/namespaces/default/jobs",
                {"labelSelector": "app=setup"},
            )
            f_warmup_jobs = pool.submit(
                _get,
                f"{server}/apis/batch/v1/namespaces/default/jobs",
                {"labelSelector": f"app={DEFAULT_WARMUP_APP_LABEL}"},
            )
            f_warmup_ds = pool.submit(
                _get,
                f"{server}/apis/apps/v1/namespaces/default/daemonsets",
                {"labelSelector": "app=db-warmup"},
            )
            f_namespaces = pool.submit(_get, f"{server}/api/v1/namespaces")

            response = f_workspace.result()
            if response.status_code == 200:
                status = response.json().get("status", {})
                result["workspace_ready"] = status.get("numberReady", 0)
                result["workspace_desired"] = status.get("desiredNumberScheduled", 0)
                result["warm"] = result["workspace_ready"] > 0

            response = f_vmtouch.result()
            if response.status_code == 200:
                result["vmtouch_ready"] = response.json().get("status", {}).get("numberReady", 0)
                result["warm"] = result["warm"] or result["vmtouch_ready"] > 0

            response = f_setup_jobs.result()
            if response.status_code == 200:
                result["databases"] = _database_status_from_setup_jobs(
                    response.json().get("items", [])
                )

            response = f_warmup_jobs.result()
            if response.status_code == 200:
                warmup_databases = database_status_from_warmup_jobs(
                    response.json().get("items", [])
                )
                # Phase 2 — node-pinning check and pod-log fetch are independent
                # of each other and of the remaining Phase 1 results, so fan
                # them out too. Pod log fetches are themselves parallelised
                # inside `_warmup_pods_and_logs`.
                f_stale = pool.submit(
                    _mark_stale_warmup_nodes, session, server, warmup_databases
                )
                f_pods = pool.submit(_warmup_pods_and_logs, session, server)
                f_stale.result()
                pods, logs_by_pod = f_pods.result()
                attach_pod_progress_to_database_status(warmup_databases, pods, logs_by_pod)
                _merge_database_statuses(result, warmup_databases)

            response = f_warmup_ds.result()
            if response.status_code == 200:
                _append_warmup_daemonsets(result, response.json().get("items", []))

            response = f_namespaces.result()
            if response.status_code == 200:
                result["namespaces"] = [
                    namespace_item.get("metadata", {}).get("name", "")
                    for namespace_item in response.json().get("items", [])
                    if namespace_item.get("metadata", {}).get("name", "").startswith(
                        "elastic-blast-"
                    )
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
        shard = ""
        shard_match = re.match(r"^(?P<db>.+)_shard_(?P<shard>\d{2,})$", str(db_name))
        if shard_match:
            db_name = shard_match.group("db")
            shard = shard_match.group("shard")

        info = db_map.setdefault(
            db_name,
            {
                "name": db_name,
                "mol_type": mol_type,
                "nodes_ready": 0,
                "nodes_failed": 0,
                "nodes_active": 0,
                "total_jobs": 0,
                "shards": [],
            },
        )
        job_status = job.get("status", {})
        info["total_jobs"] += 1
        info["nodes_ready"] += job_status.get("succeeded", 0)
        info["nodes_failed"] += job_status.get("failed", 0)
        info["nodes_active"] += job_status.get("active", 0)
        if shard:
            info["shards"].append(shard)

    for info in db_map.values():
        info["shards"] = sorted(set(info.get("shards", [])))
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
    pod_names = [
        pod.get("metadata", {}).get("name", "")
        for pod in pods[:12]
        if pod.get("metadata", {}).get("name")
    ]
    if not pod_names:
        return pods, {}

    def _fetch_log(name: str) -> tuple[str, str | None]:
        try:
            log_response = session.get(
                f"{server}/api/v1/namespaces/default/pods/{name}/log",
                params={"container": "warmup", "tailLines": 80},
                timeout=2,
            )
        except Exception:
            return name, None
        if log_response.status_code != 200:
            return name, None
        return name, log_response.text[-8000:]

    # Up to 12 pod log GETs — fire concurrently so the wall time is bounded
    # by the slowest log fetch (2 s timeout each) instead of summing all 12.
    logs_by_pod: dict[str, str] = {}
    with ThreadPoolExecutor(
        max_workers=min(12, len(pod_names)),
        thread_name_prefix="warmup-logs",
    ) as pool:
        for name, text in pool.map(_fetch_log, pod_names):
            if text is not None:
                logs_by_pod[name] = text
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

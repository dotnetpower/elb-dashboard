"""Bounded garbage collection for terminal ElasticBLAST Kubernetes objects.

Responsibility: Delete old terminal BLAST Jobs and OpenAPI job-state ConfigMaps while
preserving active, recent, and unclassifiable objects.
Edit boundaries: Kubernetes list/delete mechanics only; scheduling and cluster discovery
belong to the Celery task wrapper.
Key entry points: `collect_runtime_garbage`.
Risky contracts: Every run has page, delete, and wall-clock bounds. Missing status or
unparseable timestamps fail closed and are never deleted.
Validation: `uv run pytest -q api/tests/test_k8s_runtime_gc.py`.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.timestamps import parse_k8s_timestamp

LOGGER = logging.getLogger(__name__)

_JOB_APPS = ("blast", "finalizer", "setup", "submit")
_TERMINAL_CONFIGMAP_STATUSES = frozenset({"cancelled", "completed", "failed"})
_SUCCESS_DELETE_CODES = frozenset({200, 202, 404})
_TRANSIENT_DELETE_CODES = frozenset({429, 500, 502, 503, 504})


def _safe_count(value: object) -> int | None:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return None


def _is_old_enough(value: object, *, now: datetime, retention_seconds: int) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        timestamp = parse_k8s_timestamp(text)
    except ValueError:
        return False
    return (now - timestamp).total_seconds() >= retention_seconds


def _terminal_job_timestamp(job: dict[str, Any]) -> str:
    status = job.get("status", {}) or {}
    active = _safe_count(status.get("active"))
    succeeded = _safe_count(status.get("succeeded"))
    if active is None or succeeded is None or active > 0:
        return ""
    terminal = succeeded > 0
    for condition in status.get("conditions", []) or []:
        if not isinstance(condition, dict):
            continue
        if condition.get("type") in {"Complete", "Failed"} and str(
            condition.get("status") or ""
        ).lower() == "true":
            terminal = True
            break
    if not terminal:
        return ""
    metadata = job.get("metadata", {}) or {}
    return str(status.get("completionTime") or metadata.get("creationTimestamp") or "")


def _terminal_configmap_timestamp(configmap: dict[str, Any]) -> str:
    metadata = configmap.get("metadata", {}) or {}
    labels = metadata.get("labels", {}) or {}
    if str(labels.get("status") or "").lower() not in _TERMINAL_CONFIGMAP_STATUSES:
        return ""
    job_payload = str((configmap.get("data", {}) or {}).get("job") or "").strip()
    if not job_payload:
        return ""
    try:
        job = json.loads(job_payload)
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(job, dict):
        return ""
    return str(
        job.get("completed_at")
        or job.get("failed_at")
        or job.get("updated_at")
        or ""
    )


def _list_candidates(
    session: Any,
    url: str,
    *,
    selector: str,
    timestamp_resolver: Any,
    now: datetime,
    retention_seconds: int,
    limit: int,
    page_size: int,
    max_pages: int,
    deadline: float,
    stats: dict[str, Any],
    scanned_key: str,
) -> list[str]:
    from api.services.k8s.client import get_with_transient_retry

    candidates: list[str] = []
    continue_token = ""
    for _page in range(max_pages):
        if len(candidates) >= limit or time.monotonic() >= deadline:
            break
        params: dict[str, Any] = {
            "labelSelector": selector,
            "limit": page_size,
        }
        if continue_token:
            params["continue"] = continue_token
        response = get_with_transient_retry(
            session,
            url,
            params=params,
            timeout=10,
        )
        if response.status_code != 200:
            stats["errors"].append(f"list {selector}: HTTP {response.status_code}")
            break
        payload = response.json()
        items = payload.get("items", []) if isinstance(payload, dict) else []
        stats[scanned_key] += len(items)
        for item in items:
            if not isinstance(item, dict):
                continue
            timestamp = timestamp_resolver(item)
            if not timestamp or not _is_old_enough(
                timestamp,
                now=now,
                retention_seconds=retention_seconds,
            ):
                continue
            name = str((item.get("metadata", {}) or {}).get("name") or "")
            if name:
                candidates.append(name)
                if len(candidates) >= limit:
                    break
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        continue_token = str((metadata or {}).get("continue") or "")
        if not continue_token:
            break
    return candidates


def _delete_candidates(
    session: Any,
    base_url: str,
    names: list[str],
    *,
    deadline: float,
    stats: dict[str, Any],
    deleted_key: str,
    resource_kind: str,
) -> None:
    for name in names:
        if time.monotonic() >= deadline - 5:
            stats["truncated"] = True
            return
        status_code = 0
        for attempt in range(2):
            response = session.delete(
                f"{base_url}/{name}",
                params={"propagationPolicy": "Background"},
                timeout=5,
            )
            status_code = int(response.status_code)
            if status_code in _SUCCESS_DELETE_CODES:
                stats[deleted_key] += 1
                break
            if (
                attempt == 0
                and status_code in _TRANSIENT_DELETE_CODES
                and time.monotonic() < deadline - 5.5
            ):
                time.sleep(0.5)
                continue
            if len(stats["errors"]) < 20:
                stats["errors"].append(
                    f"delete {resource_kind}/{name}: HTTP {status_code} attempts={attempt + 1}"
                )
            LOGGER.warning(
                "k8s_runtime_gc delete_failed resource=%s name=%s status=%d attempts=%d",
                resource_kind,
                name,
                status_code,
                attempt + 1,
            )
            break


def collect_runtime_garbage(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str = "default",
    job_retention_seconds: int = 3600,
    configmap_retention_seconds: int = 14 * 24 * 60 * 60,
    max_deletes: int = 200,
    page_size: int = 200,
    max_pages_per_selector: int = 4,
    deadline_seconds: float = 90.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete a bounded batch of old terminal runtime objects."""
    from api.services.k8s.credentials import _get_k8s_session

    bounded_deletes = max(2, min(int(max_deletes), 1000))
    job_budget = bounded_deletes // 2
    configmap_budget = bounded_deletes - job_budget
    bounded_page_size = max(1, min(int(page_size), 500))
    bounded_pages = max(1, min(int(max_pages_per_selector), 20))
    current_time = now or datetime.now(UTC)
    deadline = time.monotonic() + max(1.0, min(float(deadline_seconds), 240.0))
    stats: dict[str, Any] = {
        "cluster_name": cluster_name,
        "jobs_scanned": 0,
        "jobs_deleted": 0,
        "configmaps_scanned": 0,
        "configmaps_deleted": 0,
        "errors": [],
        "truncated": False,
    }
    session, server = _get_k8s_session(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        admin=True,
    )
    try:
        jobs_url = f"{server}/apis/batch/v1/namespaces/{namespace}/jobs"
        per_app_budget = max(1, job_budget // len(_JOB_APPS))
        job_names: list[str] = []
        for app in _JOB_APPS:
            if time.monotonic() >= deadline:
                stats["truncated"] = True
                break
            job_names.extend(
                _list_candidates(
                    session,
                    jobs_url,
                    selector=f"app={app}",
                    timestamp_resolver=_terminal_job_timestamp,
                    now=current_time,
                    retention_seconds=max(0, int(job_retention_seconds)),
                    limit=per_app_budget,
                    page_size=bounded_page_size,
                    max_pages=bounded_pages,
                    deadline=deadline,
                    stats=stats,
                    scanned_key="jobs_scanned",
                )
            )
        _delete_candidates(
            session,
            jobs_url,
            job_names[:job_budget],
            deadline=deadline,
            stats=stats,
            deleted_key="jobs_deleted",
            resource_kind="job",
        )

        configmaps_url = f"{server}/api/v1/namespaces/{namespace}/configmaps"
        configmap_names = _list_candidates(
            session,
            configmaps_url,
            selector="elb-job=true",
            timestamp_resolver=_terminal_configmap_timestamp,
            now=current_time,
            retention_seconds=max(0, int(configmap_retention_seconds)),
            limit=configmap_budget,
            page_size=bounded_page_size,
            max_pages=bounded_pages,
            deadline=deadline,
            stats=stats,
            scanned_key="configmaps_scanned",
        )
        _delete_candidates(
            session,
            configmaps_url,
            configmap_names,
            deadline=deadline,
            stats=stats,
            deleted_key="configmaps_deleted",
            resource_kind="configmap",
        )
    finally:
        session.close()

    if stats["jobs_deleted"] or stats["configmaps_deleted"] or stats["errors"]:
        LOGGER.info(
            "k8s_runtime_gc cluster=%s jobs_deleted=%d configmaps_deleted=%d "
            "errors=%d truncated=%s",
            cluster_name,
            stats["jobs_deleted"],
            stats["configmaps_deleted"],
            len(stats["errors"]),
            stats["truncated"],
        )
    return stats


__all__ = ("collect_runtime_garbage",)

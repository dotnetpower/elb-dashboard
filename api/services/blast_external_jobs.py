"""External OpenAPI BLAST job cache, sync, and projection helpers.

Responsibility: External OpenAPI BLAST job cache, sync, and projection helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_external_status_to_dashboard`, `_exception_reason`,
`_external_list_jobs_cached`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

LOGGER = logging.getLogger(__name__)


def _external_status_to_dashboard(status: str) -> str:
    if status in {"success", "completed"}:
        return "completed"
    if status in {"queued", "running", "failed", "cancelled"}:
        return status
    return "running" if status else "unknown"


def _exception_reason(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = detail.get("code")
            if code not in (None, ""):
                return str(code)
        if detail not in (None, ""):
            return str(detail)[:120]
        return f"http_{exc.status_code}"
    return type(exc).__name__


_EXTERNAL_NOT_ENABLED_REASONS = frozenset(
    {
        "openapi_not_configured",
        "openapi_not_enabled",
    }
)
_EXTERNAL_DETAIL_ENRICH_LIMIT = 20
_EXTERNAL_JOBS_CACHE_TTL_SECONDS = 70.0
_EXTERNAL_JOBS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_EXTERNAL_JOBS_CACHE_LOCK = threading.Lock()
_EXTERNAL_JOBS_INFLIGHT: dict[str, threading.Event] = {}
_EXTERNAL_JOB_DETAIL_CACHE_TTL_SECONDS = 70.0
_EXTERNAL_JOB_DETAIL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_EXTERNAL_SYNC_CACHE_TTL_SECONDS = 70.0
_EXTERNAL_SYNC_CACHE: dict[str, tuple[float, tuple[int, int, set[str]]]] = {}
_OPENAPI_CLIENT_KWARGS_CACHE_TTL_SECONDS = 70.0
_OPENAPI_CLIENT_KWARGS_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


def _external_list_jobs_cached(external_kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    """Cached wrapper around ``external_blast.list_jobs(**kwargs)``."""

    import json
    import time as _time

    from api.services import external_blast

    key = json.dumps(external_kwargs, sort_keys=True, default=str)
    while True:
        now = _time.monotonic()
        with _EXTERNAL_JOBS_CACHE_LOCK:
            entry = _EXTERNAL_JOBS_CACHE.get(key)
            if entry and entry[0] > now:
                return entry[1]
            inflight = _EXTERNAL_JOBS_INFLIGHT.get(key)
            if inflight is None:
                inflight = threading.Event()
                _EXTERNAL_JOBS_INFLIGHT[key] = inflight
                leader = True
            else:
                leader = False
        if not leader:
            inflight.wait(timeout=35.0)
            continue
        try:
            rows = external_blast.list_jobs(**external_kwargs).get("jobs", []) or []
            if not isinstance(rows, list):
                rows = []
            expires_at = _time.monotonic() + _EXTERNAL_JOBS_CACHE_TTL_SECONDS
            with _EXTERNAL_JOBS_CACHE_LOCK:
                _EXTERNAL_JOBS_CACHE[key] = (expires_at, rows)
                if len(_EXTERNAL_JOBS_CACHE) > 32:
                    oldest = min(_EXTERNAL_JOBS_CACHE.items(), key=lambda kv: kv[1][0])[0]
                    _EXTERNAL_JOBS_CACHE.pop(oldest, None)
            return rows
        finally:
            with _EXTERNAL_JOBS_CACHE_LOCK:
                _EXTERNAL_JOBS_INFLIGHT.pop(key, None)
                inflight.set()


def _reset_external_jobs_cache() -> None:
    """Test hook: clear the in-memory external jobs caches."""

    with _EXTERNAL_JOBS_CACHE_LOCK:
        _EXTERNAL_JOBS_CACHE.clear()
        _EXTERNAL_JOBS_INFLIGHT.clear()
        _EXTERNAL_JOB_DETAIL_CACHE.clear()
        _EXTERNAL_SYNC_CACHE.clear()
        _OPENAPI_CLIENT_KWARGS_CACHE.clear()


def _sync_external_jobs_to_table(
    external_jobs: list[dict[str, Any]],
    *,
    caller_oid: str,
    tenant_id: str = "",
) -> tuple[int, int, set[str]]:
    """Best-effort upsert of external OpenAPI jobs into Azure Table Storage."""

    if not external_jobs:
        return (0, 0, set())
    import json
    import time as _time

    sync_key = json.dumps(
        {
            "caller_oid": caller_oid,
            "tenant_id": tenant_id,
            "jobs": [
                {
                    "job_id": str(ext.get("job_id") or ""),
                    "status": str(ext.get("status") or ""),
                    "phase": str(ext.get("phase") or ""),
                    "updated_at": str(ext.get("updated_at") or ext.get("completed_at") or ""),
                }
                for ext in external_jobs
            ],
        },
        sort_keys=True,
        default=str,
    )
    now = _time.monotonic()
    with _EXTERNAL_JOBS_CACHE_LOCK:
        cached = _EXTERNAL_SYNC_CACHE.get(sync_key)
        if cached and cached[0] > now:
            c_created, c_updated, c_tombstoned = cached[1]
            return (c_created, c_updated, set(c_tombstoned))
    try:
        from api.services.state_repo import JobState, JobStateRepository

        repo = JobStateRepository()
    except Exception:
        return (0, 0, set())

    job_ids = [str(ext.get("job_id") or "") for ext in external_jobs]
    try:
        existing_map = repo.get_many([jid for jid in job_ids if jid])
    except Exception as exc:
        LOGGER.debug("sync_external_jobs batch lookup failed: %s", type(exc).__name__)
        existing_map = {}

    created = 0
    updated = 0
    tombstoned: set[str] = set()
    for ext in external_jobs:
        job_id = str(ext.get("job_id") or "")
        if not job_id:
            continue
        try:
            converted = _external_to_blast_job(ext)
            ext_status = str(converted.get("status") or "unknown")
            ext_phase = str(converted.get("phase") or ext_status)
            existing = existing_map.get(job_id)
            if existing is not None:
                cur_status = str(existing.status or "")
                cur_phase = str(existing.phase or "")
                if cur_status == "deleted":
                    tombstoned.add(job_id)
                    continue
                if ext_status and (ext_status != cur_status or ext_phase != cur_phase):
                    try:
                        repo.update(job_id, status=ext_status, phase=ext_phase)
                        updated += 1
                    except KeyError:
                        existing = None
                if existing is not None:
                    continue
            payload = converted.get("payload") or {"external": ext}
            state = JobState(
                job_id=job_id,
                type="blast",
                status=ext_status,
                phase=ext_phase,
                owner_oid=caller_oid,
                tenant_id=tenant_id,
                created_at=str(converted.get("created_at") or ""),
                updated_at=str(converted.get("updated_at") or ""),
                payload=payload,
                job_title=str(converted.get("job_title") or ""),
                program=str(converted.get("program") or ""),
                db=str(converted.get("db") or ""),
                query_label=str(converted.get("query_label") or ""),
                subscription_id=str(
                    (converted.get("infrastructure") or {}).get("subscription_id") or ""
                ),
                resource_group=str(
                    (converted.get("infrastructure") or {}).get("resource_group") or ""
                ),
                cluster_name=str((converted.get("infrastructure") or {}).get("cluster_name") or ""),
                storage_account=str(
                    (converted.get("infrastructure") or {}).get("storage_account") or ""
                ),
            )
            repo.create(state)
            created += 1
        except Exception as exc:
            LOGGER.debug(
                "sync_external_job_to_table failed job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )
    if created or updated:
        LOGGER.info("external job sync: created=%d updated=%d", created, updated)
    with _EXTERNAL_JOBS_CACHE_LOCK:
        _EXTERNAL_SYNC_CACHE[sync_key] = (
            _time.monotonic() + _EXTERNAL_SYNC_CACHE_TTL_SECONDS,
            (created, updated, set(tombstoned)),
        )
        if len(_EXTERNAL_SYNC_CACHE) > 128:
            oldest = min(_EXTERNAL_SYNC_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _EXTERNAL_SYNC_CACHE.pop(oldest, None)
    return (created, updated, tombstoned)


def _short_external_db_name(*values: Any) -> str:
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        if raw.startswith(("http://", "https://", "az://")):
            parsed = urlparse(
                "https://" + raw.removeprefix("az://") if raw.startswith("az://") else raw
            )
            parts = [part for part in parsed.path.split("/") if part]
            if parts:
                return parts[-1]
        parts = [part for part in raw.replace("\\", "/").split("/") if part]
        return parts[-1] if parts else raw
    return ""


def _external_error_message(error: Any) -> tuple[str | None, str | None]:
    if not error:
        return None, None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip() or None
        message = str(error.get("message") or code or "").strip() or None
        return code, message
    message = str(error).strip()
    return None, message or None


def _external_execution_summary(job: dict[str, Any]) -> dict[str, int]:
    execution = job.get("execution")
    if not isinstance(execution, dict):
        result = job.get("result")
        if isinstance(result, dict) and isinstance(result.get("execution"), dict):
            execution = result.get("execution")
    if not isinstance(execution, dict):
        return {}

    def number(key: str) -> int:
        value = execution.get(key)
        try:
            return max(0, int(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    shard_count = number("shard_count")
    succeeded = number("shards_succeeded")
    active = number("shards_active")
    failed = number("shards_failed")
    done = min(shard_count, succeeded + failed) if shard_count else succeeded + failed
    out: dict[str, int] = {
        "splits_done": done,
        "splits_failed": failed,
    }
    if shard_count:
        out["splits_total"] = shard_count
    out["splits_active"] = active
    return out


def _merge_external_detail(row: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(row)
    for key, value in detail.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _external_job_detail_or_row(
    external_blast: Any,
    row: dict[str, Any],
    external_kwargs: dict[str, str],
) -> dict[str, Any]:
    import json
    import time as _time

    job_id = str(row.get("job_id") or "").strip()
    if not job_id:
        return row
    detail_key = json.dumps(
        {"job_id": job_id, "kwargs": external_kwargs},
        sort_keys=True,
        default=str,
    )
    now = _time.monotonic()
    with _EXTERNAL_JOBS_CACHE_LOCK:
        entry = _EXTERNAL_JOB_DETAIL_CACHE.get(detail_key)
        if entry and entry[0] > now:
            return _merge_external_detail(row, entry[1])
    try:
        detail = external_blast.get_job(job_id, **external_kwargs)
    except Exception as exc:
        LOGGER.info(
            "external blast job detail unavailable job_id=%s: %s",
            job_id,
            _exception_reason(exc),
        )
        return row
    if not isinstance(detail, dict):
        return row
    with _EXTERNAL_JOBS_CACHE_LOCK:
        _EXTERNAL_JOB_DETAIL_CACHE[detail_key] = (
            _time.monotonic() + _EXTERNAL_JOB_DETAIL_CACHE_TTL_SECONDS,
            detail,
        )
        if len(_EXTERNAL_JOB_DETAIL_CACHE) > 256:
            oldest = min(_EXTERNAL_JOB_DETAIL_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _EXTERNAL_JOB_DETAIL_CACHE.pop(oldest, None)
    return _merge_external_detail(row, detail)


def _external_to_blast_job(
    job: dict[str, Any],
    *,
    include_database_metadata: bool = False,
) -> dict[str, Any]:
    from api.services.state_repo import canonical_job_metadata

    external_status = str(job.get("status") or "unknown")
    status = _external_status_to_dashboard(external_status)
    metadata = canonical_job_metadata(
        {
            "job_title": job.get("job_title") or job.get("title"),
            "program": job.get("program"),
            "db": job.get("db_name") or job.get("db"),
            "query_file": job.get("query_file") or job.get("query"),
            "subscription_id": job.get("subscription_id"),
            "resource_group": job.get("resource_group"),
            "cluster_name": job.get("cluster_name"),
            "storage_account": job.get("storage_account"),
        },
        job_id=str(job.get("job_id") or ""),
    )
    db = metadata["db"]
    program = metadata["program"]
    created_at = str(job.get("created_at") or "")
    updated_at = str(
        job.get("updated_at")
        or job.get("last_progress_at")
        or job.get("completed_at")
        or job.get("failed_at")
        or created_at
    )
    source = str(job.get("submission_source") or "external_api")
    error_code, error_message = _external_error_message(job.get("error"))
    out: dict[str, Any] = {
        "job_id": job.get("job_id"),
        "job_title": metadata["job_title"],
        "program": program,
        "db": db,
        "status": status,
        "phase": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "source": source,
        "submission_source": source,
        "external_correlation_id": job.get("external_correlation_id") or "",
        "query_label": metadata["query_label"] or "query.fa",
        "custom_status": {
            "phase": status,
            "blast_status": external_status,
            "progress_pct": job.get("progress_pct"),
            "queue_position": job.get("queue_position"),
        },
        "output": {
            "status": status,
            "external_status": external_status,
            "result": job.get("result"),
            "execution": job.get("execution"),
        },
        "payload": {"external": job},
    }
    out.update(_external_execution_summary(job))
    infrastructure = {
        "subscription_id": metadata["subscription_id"],
        "resource_group": metadata["resource_group"],
        "cluster_name": metadata["cluster_name"],
        "storage_account": metadata["storage_account"],
    }
    if any(infrastructure.values()):
        out["infrastructure"] = {k: v for k, v in infrastructure.items() if v}
    if include_database_metadata:
        database_metadata = _database_metadata_for_response(
            db,
            str(infrastructure.get("storage_account") or ""),
        )
        if database_metadata is not None:
            out["database_metadata"] = database_metadata
    if error_message:
        out["error"] = error_message
    if error_code:
        out["error_code"] = error_code
    return out


def _database_metadata_for_response(
    database: str,
    storage_account: str,
) -> dict[str, Any] | None:
    try:
        from api.services.blast_db_metadata import resolve_database_display_metadata

        return resolve_database_display_metadata(storage_account, database)
    except Exception as exc:
        LOGGER.info(
            "database metadata projection skipped db=%s: %s",
            database,
            type(exc).__name__,
        )
        return None


def _openapi_client_kwargs_from_cluster(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, str]:
    if not (subscription_id and resource_group and cluster_name):
        return {}
    import json
    import time as _time

    cache_key = json.dumps(
        {
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": cluster_name,
        },
        sort_keys=True,
    )
    now = _time.monotonic()
    with _EXTERNAL_JOBS_CACHE_LOCK:
        cached = _OPENAPI_CLIENT_KWARGS_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])
    try:
        from api.services import get_credential
        from api.services.k8s_monitoring import (
            k8s_get_deployment_env_value,
            k8s_get_service_ip,
        )

        credential = get_credential()
        ip = k8s_get_service_ip(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            "elb-openapi",
        )
        if not ip:
            return {}
        try:
            from api.services.openapi_runtime import save_openapi_base_url

            save_openapi_base_url(
                f"http://{ip}",
                metadata={
                    "subscription_id": subscription_id,
                    "resource_group": resource_group,
                    "cluster_name": cluster_name,
                    "service_name": "elb-openapi",
                },
            )
        except Exception as exc:
            LOGGER.debug("openapi runtime cache write skipped: %s", type(exc).__name__)
        api_token = os.environ.get("ELB_OPENAPI_API_TOKEN", "").strip()
        if not api_token:
            api_token = (
                k8s_get_deployment_env_value(
                    credential,
                    subscription_id,
                    resource_group,
                    cluster_name,
                    "elb-openapi",
                    "ELB_OPENAPI_API_TOKEN",
                    container_name="openapi",
                )
                or ""
            ).strip()
        kwargs = {"base_url": f"http://{ip}"}
        if api_token:
            kwargs["api_token"] = api_token
        with _EXTERNAL_JOBS_CACHE_LOCK:
            _OPENAPI_CLIENT_KWARGS_CACHE[cache_key] = (
                _time.monotonic() + _OPENAPI_CLIENT_KWARGS_CACHE_TTL_SECONDS,
                dict(kwargs),
            )
        return kwargs
    except Exception as exc:
        LOGGER.info("openapi cluster context unavailable: %s", type(exc).__name__)
        return {}


def _external_result_files(job: dict[str, Any]) -> list[dict[str, Any]]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    files = result.get("files") if isinstance(result, dict) else []
    if not isinstance(files, list):
        return []
    out: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or item.get("name") or "")
        file_id = str(item.get("file_id") or "")
        if not filename or not file_id:
            continue
        out.append(
            {
                "file_id": file_id,
                "name": filename,
                "size": item.get("size_bytes") or item.get("size"),
                "last_modified": item.get("last_modified"),
                "format": item.get("format"),
                "source": "external",
            }
        )
    return out

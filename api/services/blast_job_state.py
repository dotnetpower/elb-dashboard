"""BLAST job projection, file preview, and external OpenAPI helpers."""

from __future__ import annotations

import logging
import os
import threading as _threading
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

from api.auth import CallerIdentity

LOGGER = logging.getLogger(__name__)


def _payload_value(payload: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _queries_blob_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("az://"):
        raw = "https://" + raw.removeprefix("az://")
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        parts = parsed.path.lstrip("/").split("/", 1)
        if len(parts) == 2 and parts[0] == "queries":
            return parts[1]
        return ""
    raw = raw.lstrip("/")
    if raw.startswith("queries/"):
        raw = raw.removeprefix("queries/")
    return raw


def _job_query_blob_path(job_id: str, caller: CallerIdentity) -> str:
    try:
        from api.services.state_repo import JobStateRepository

        state = JobStateRepository().get(job_id)
    except Exception as exc:
        LOGGER.info("query preview state lookup failed job_id=%s: %s", job_id, type(exc).__name__)
        return ""
    if state is None:
        return ""
    if getattr(state, "owner_oid", None) and state.owner_oid != caller.object_id:
        raise HTTPException(403, "not owner")
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    return _queries_blob_path(_payload_value(payload, "query_file", "query_blob_url"))


def _blob_not_found(exc: BaseException) -> bool:
    from azure.core.exceptions import ResourceNotFoundError

    if isinstance(exc, ResourceNotFoundError):
        return True
    text = str(exc)
    return any(
        marker in text
        for marker in (
            "BlobNotFound",
            "ResourceNotFound",
            "The specified blob does not exist",
        )
    )


def _job_payload_for_file_preview(job_id: str, caller: CallerIdentity) -> dict[str, Any]:
    try:
        from api.services.state_repo import JobStateRepository

        state = JobStateRepository().get(job_id)
    except Exception as exc:
        if os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true":
            LOGGER.info(
                "file preview state lookup skipped job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )
            return {}
        LOGGER.warning("file preview state lookup failed job_id=%s: %s", job_id, type(exc).__name__)
        raise HTTPException(503, {"code": "state_lookup_unavailable"}) from exc
    if state is None:
        return {}
    if getattr(state, "owner_oid", None) and state.owner_oid != caller.object_id:
        raise HTTPException(403, "not owner")
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    return payload


def _config_preview_from_payload(
    *,
    job_id: str,
    storage_account: str,
    payload: dict[str, Any],
) -> str:
    from api.tasks.blast import _build_config_content

    options = dict(payload.get("options") if isinstance(payload.get("options"), dict) else {})
    for key in ("acr_resource_group", "acr_name"):
        value = payload.get(key)
        if value not in (None, ""):
            options.setdefault(key, value)
    return _build_config_content(
        job_id=job_id,
        resource_group=str(_payload_value(payload, "resource_group") or ""),
        cluster_name=str(_payload_value(payload, "cluster_name", "aks_cluster_name") or ""),
        storage_account=str(_payload_value(payload, "storage_account") or storage_account),
        program=str(_payload_value(payload, "program") or "blastn"),
        database=str(_payload_value(payload, "database", "db") or ""),
        query_file=str(_payload_value(payload, "query_file", "query_blob_url") or ""),
        options=options,
    )


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


# Reasons that mean "the external OpenAPI execution plane is intentionally not
# wired up yet" rather than "it failed at runtime". Surfacing these as
# `external_degraded` makes every `/api/blast/jobs` poll look like an error in
# the request inspector (constant red Degraded badge), which is misleading —
# the dashboard works fine on the local state repo alone. Skip them.
_EXTERNAL_NOT_ENABLED_REASONS = frozenset(
    {
        "openapi_not_configured",
        "openapi_not_enabled",
    }
)
_EXTERNAL_DETAIL_ENRICH_LIMIT = 20

# Short in-memory cache for the external OpenAPI `/v1/jobs` listing.
# Multiple dashboard components poll the jobs endpoint independently
# (Dashboard card, AKS pulse row, Jobs page), so without this cache the
# upstream HTTP call is fired ~4x per dashboard refresh cycle. 15s is
# well below the smallest polling interval (20s) and within the
# dashboard's tolerance for stale list rows.
_EXTERNAL_JOBS_CACHE_TTL_SECONDS = 15.0
_EXTERNAL_JOBS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_EXTERNAL_JOBS_CACHE_LOCK = _threading.Lock()


def _external_list_jobs_cached(external_kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    """Cached wrapper around ``external_blast.list_jobs(**kwargs)``.

    The cache key is the JSON-serialised kwargs (base_url + token), so
    different clusters / tokens never share an entry.
    """
    import json
    import time as _time

    from api.services import external_blast

    key = json.dumps(external_kwargs, sort_keys=True, default=str)
    now = _time.monotonic()
    with _EXTERNAL_JOBS_CACHE_LOCK:
        entry = _EXTERNAL_JOBS_CACHE.get(key)
        if entry and entry[0] > now:
            return entry[1]
    rows = external_blast.list_jobs(**external_kwargs).get("jobs", []) or []
    if not isinstance(rows, list):
        rows = []
    with _EXTERNAL_JOBS_CACHE_LOCK:
        _EXTERNAL_JOBS_CACHE[key] = (now + _EXTERNAL_JOBS_CACHE_TTL_SECONDS, rows)
        # Tiny LRU-ish trim. There are usually 1-2 keys at most (one per
        # active cluster scope); we cap defensively so the process never
        # accumulates entries from short-lived clusters.
        if len(_EXTERNAL_JOBS_CACHE) > 32:
            oldest = min(_EXTERNAL_JOBS_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _EXTERNAL_JOBS_CACHE.pop(oldest, None)
    return rows


def _reset_external_jobs_cache() -> None:
    """Test hook: clear the in-memory cache between assertions.

    Production callers never need to invoke this; the cache TTL is short
    enough that staleness is bounded. Tests that re-mock
    ``external_blast.list_jobs`` between cases must reset the cache so
    their new mock is not bypassed by a stale entry.
    """
    with _EXTERNAL_JOBS_CACHE_LOCK:
        _EXTERNAL_JOBS_CACHE.clear()


def _sync_external_jobs_to_table(
    external_jobs: list[dict[str, Any]],
    *,
    caller_oid: str,
    tenant_id: str = "",
) -> tuple[int, int, set[str]]:
    """Best-effort upsert of external OpenAPI jobs into Azure Table Storage.

    Returns ``(created, updated, tombstoned_ids)``. Failures are logged
    but never propagated — the caller already has the in-memory list and
    does not depend on the sync succeeding.

    ``tombstoned_ids`` is the set of external job_ids whose Table row has
    ``status='deleted'``. The caller MUST filter these out of any list it
    returns to the dashboard; otherwise the soft-deleted row reappears on
    every poll because the external plane still remembers it.

    Behaviour:

    * Unknown job_id → ``create`` (claims ownership for the current caller).
    * Known job_id whose status/phase has drifted in the external plane →
      ``update`` so the dashboard's list view reflects the latest state
      without waiting for a per-job detail fetch.
    * Known job_id with no status drift → left alone (avoids writing a
      new jobhistory row on every poll).
    * Tombstoned (``status='deleted'``) job_id → left alone AND added to
      the returned set so the caller drops it from the response.
    """
    if not external_jobs:
        return (0, 0, set())
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
                # Respect tombstone: the user already asked to delete
                # this row. Do not resurrect it on the next external
                # poll just because the upstream still remembers it.
                if cur_status == "deleted":
                    tombstoned.add(job_id)
                    continue
                # Only refresh status/phase if the external plane has
                # moved on. Avoids appending a jobhistory row per poll.
                if ext_status and (ext_status != cur_status or ext_phase != cur_phase):
                    try:
                        repo.update(
                            job_id,
                            status=ext_status,
                            phase=ext_phase,
                        )
                        updated += 1
                    except KeyError:
                        # Row vanished between batch lookup and update;
                        # fall through to the create path below.
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
            return max(0, int(value))
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
    job_id = str(row.get("job_id") or "").strip()
    if not job_id:
        return row
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
    return _merge_external_detail(row, detail)


def _external_to_blast_job(job: dict[str, Any]) -> dict[str, Any]:
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
    if error_message:
        out["error"] = error_message
    if error_code:
        out["error_code"] = error_code
    return out


def _openapi_client_kwargs_from_cluster(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, str]:
    if not (subscription_id and resource_group and cluster_name):
        return {}
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
        return kwargs
    except Exception as exc:
        LOGGER.info("openapi cluster context unavailable: %s", type(exc).__name__)
        return {}


def _split_child_summary_from_repo(repo: Any, parent_job_id: str) -> dict[str, Any] | None:
    try:
        children = list(repo.list_children(parent_job_id, limit=1000))
    except Exception as exc:
        LOGGER.info(
            "split child summary unavailable job_id=%s: %s", parent_job_id, type(exc).__name__
        )
        return None
    if not children:
        return None
    counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for child in children:
        status = str(getattr(child, "status", "") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        payload = child.payload if isinstance(getattr(child, "payload", None), dict) else {}
        items.append(
            {
                "job_id": getattr(child, "job_id", ""),
                "status": status,
                "phase": getattr(child, "phase", None),
                "group_id": payload.get("group_id"),
                "query_file": payload.get("query_file"),
                "effective_search_space": payload.get("effective_search_space"),
            }
        )
    return {"child_count": len(children), "children_by_status": counts, "children": items}


def _split_child_summary_from_children(children: list[Any]) -> dict[str, Any] | None:
    if not children:
        return None
    counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for child in children:
        status = str(getattr(child, "status", "") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        payload = child.payload if isinstance(getattr(child, "payload", None), dict) else {}
        items.append(
            {
                "job_id": getattr(child, "job_id", ""),
                "status": status,
                "phase": getattr(child, "phase", None),
                "group_id": payload.get("group_id"),
                "query_file": payload.get("query_file"),
                "effective_search_space": payload.get("effective_search_space"),
            }
        )
    return {"child_count": len(children), "children_by_status": counts, "children": items}


def _split_child_summaries_from_repo(
    repo: Any,
    owner_oid: str,
    parent_job_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not parent_job_ids:
        return {}
    try:
        grouped = repo.list_children_for_owner(owner_oid, parent_job_ids, limit=5000)
    except AttributeError:
        return {
            parent_job_id: summary
            for parent_job_id in parent_job_ids
            if (summary := _split_child_summary_from_repo(repo, parent_job_id)) is not None
        }
    except Exception as exc:
        LOGGER.info("split child summaries unavailable: %s", type(exc).__name__)
        return {}
    summaries: dict[str, dict[str, Any]] = {}
    for parent_job_id, children in grouped.items():
        summary = _split_child_summary_from_children(children)
        if summary is not None:
            summaries[parent_job_id] = summary
    return summaries


def _local_to_blast_job(state: Any, split_children: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = state.payload if isinstance(state.payload, dict) else {}
    progress = payload.get("_progress") if isinstance(payload.get("_progress"), dict) else None
    program = str(getattr(state, "program", None) or _payload_value(payload, "program") or "blast")
    db = str(getattr(state, "db", None) or _payload_value(payload, "db", "database") or "")
    infrastructure = {
        "subscription_id": getattr(state, "subscription_id", None)
        or _payload_value(payload, "subscription_id"),
        "resource_group": getattr(state, "resource_group", None)
        or _payload_value(payload, "resource_group"),
        "region": _payload_value(payload, "region"),
        "storage_account": getattr(state, "storage_account", None)
        or _payload_value(payload, "storage_account"),
        "acr_name": _payload_value(payload, "acr_name"),
        "cluster_name": getattr(state, "cluster_name", None)
        or _payload_value(payload, "aks_cluster_name", "cluster_name"),
    }
    out = {
        "job_id": state.job_id,
        "instance_id": state.task_id,
        "job_title": str(getattr(state, "job_title", None) or state.job_id),
        "program": program,
        "db": db,
        "status": state.status,
        "phase": state.phase or state.status,
        "task_id": state.task_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "error_code": state.error_code,
        "error": state.error_code,
        "payload": payload,
        "config_snapshot": payload.get("config_snapshot") if isinstance(payload, dict) else None,
        "infrastructure": {k: v for k, v in infrastructure.items() if v not in (None, "")},
        "source": "dashboard",
    }
    if progress is not None:
        out["custom_status"] = progress
        out["output"] = {
            "status": state.status,
            "phase": state.phase or state.status,
            "steps": progress.get("steps", {}),
        }
    # Optional dashboard-friendly query name (used by the cluster bento
    # Active jobs cell to show "BRCA1 - chr17.fa" rather than the raw uuid).
    query_label = getattr(state, "query_label", None) or _payload_value(
        payload,
        "query_file",
        "query_name",
        "queries",
    )
    if query_label:
        out["query_label"] = str(query_label)[:120]
    if getattr(state, "parent_job_id", None):
        out["parent_job_id"] = state.parent_job_id
    if split_children is not None:
        out["split_children"] = split_children
        # Derived progress fields — pre-computed server-side so every
        # SPA consumer (cluster bento, BlastJobs page, modal) renders
        # the same numbers without each rolling its own count loop.
        counts = split_children.get("children_by_status") or {}
        if isinstance(counts, dict):
            done_states = {"completed", "succeeded", "success"}
            failed_states = {"failed", "error"}
            total = int(split_children.get("child_count") or 0)
            done = sum(int(v) for k, v in counts.items() if str(k).lower() in done_states)
            failed = sum(int(v) for k, v in counts.items() if str(k).lower() in failed_states)
            out["splits_done"] = done
            out["splits_failed"] = failed
            out["splits_total"] = total
    return out


def _scope_value_matches(actual: object, expected: str) -> bool:
    if not expected:
        return True
    if actual in (None, ""):
        return False
    return str(actual).casefold() == expected.casefold()


def _local_state_matches_job_scope(
    state: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> bool:
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    return (
        _scope_value_matches(
            getattr(state, "subscription_id", None) or _payload_value(payload, "subscription_id"),
            subscription_id,
        )
        and _scope_value_matches(
            getattr(state, "resource_group", None) or _payload_value(payload, "resource_group"),
            resource_group,
        )
        and _scope_value_matches(
            getattr(state, "cluster_name", None)
            or _payload_value(payload, "aks_cluster_name", "cluster_name"),
            cluster_name,
        )
    )


def _refresh_running_blast_state(repo: Any, state: Any) -> Any:
    if getattr(state, "type", "") != "blast" or getattr(state, "status", "") != "running":
        return state
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    subscription_id = str(_payload_value(payload, "subscription_id") or "")
    resource_group = str(_payload_value(payload, "resource_group") or "")
    cluster_name = str(_payload_value(payload, "cluster_name", "aks_cluster_name") or "")
    storage_account = str(
        getattr(state, "storage_account", None) or _payload_value(payload, "storage_account") or ""
    )
    if not (subscription_id and resource_group and cluster_name):
        return state
    k8s_job_id = str(
        _payload_value(payload, "elastic_blast_job_id", "k8s_job_id")
        or _discover_elastic_blast_job_id(storage_account, str(state.job_id))
        or state.job_id
    )
    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_check_blast_status

        k8s = k8s_check_blast_status(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            namespace="default",
            job_id=k8s_job_id,
        )
    except Exception as exc:
        LOGGER.debug("blast k8s refresh skipped job_id=%s: %s", state.job_id, type(exc).__name__)
        return state
    k8s_status = str(k8s.get("status") or "")
    if k8s_status not in {"completed", "failed"}:
        return state
    if k8s_status == "completed" and not _state_has_parseable_result_artifact(state, payload):
        try:
            updated = repo.update(
                state.job_id,
                status="running",
                phase="results_pending",
                payload=_payload_with_refresh_progress(
                    payload,
                    phase="results_pending",
                    status="running",
                    k8s=k8s,
                ),
            )
            repo.append_history(
                state.job_id,
                "k8s_completed_results_pending",
                {"status": "running", "phase": "results_pending", "k8s": k8s},
            )
            return updated
        except Exception as exc:
            LOGGER.debug("blast results-pending update skipped job_id=%s: %s", state.job_id, exc)
            return state
    try:
        updated = repo.update(
            state.job_id,
            status=k8s_status,
            phase=k8s_status,
            payload=_payload_with_refresh_progress(
                payload,
                phase=k8s_status,
                status=k8s_status,
                k8s=k8s,
            ),
        )
        repo.append_history(
            state.job_id,
            "k8s_status_refreshed",
            {"status": k8s_status, "phase": k8s_status, "k8s": k8s},
        )
        return updated
    except Exception as exc:
        LOGGER.debug("blast k8s refresh update skipped job_id=%s: %s", state.job_id, exc)
        return state


def _payload_with_refresh_progress(
    payload: dict[str, Any],
    *,
    phase: str,
    status: str,
    k8s: dict[str, Any],
) -> dict[str, Any]:
    out = dict(payload)
    elastic_blast_job_id = str(k8s.get("job_id") or "")
    if elastic_blast_job_id.startswith("job-"):
        out["elastic_blast_job_id"] = elastic_blast_job_id
    progress = dict(out.get("_progress") if isinstance(out.get("_progress"), dict) else {})
    steps = dict(progress.get("steps") if isinstance(progress.get("steps"), dict) else {})
    step_key = "exporting_results" if phase == "results_pending" else phase
    step = dict(steps.get(step_key) if isinstance(steps.get(step_key), dict) else {})
    step.update({"phase": phase, "status": status, "k8s": k8s})
    if status == "completed":
        step["success"] = True
    steps[step_key] = step
    progress.update({"phase": phase, "status": status, "steps": steps})
    out["_progress"] = progress
    return out


def _state_has_parseable_result_artifact(state: Any, payload: dict[str, Any]) -> bool:
    storage_account = str(
        getattr(state, "storage_account", None) or _payload_value(payload, "storage_account") or ""
    )
    if not storage_account:
        return False
    try:
        from api.services.blast_result_analytics import list_parseable_result_blobs

        return bool(list_parseable_result_blobs(storage_account, str(state.job_id)))
    except Exception as exc:
        LOGGER.info(
            "blast result artifact check unavailable job_id=%s: %s",
            getattr(state, "job_id", ""),
            type(exc).__name__,
        )
        return False


def _discover_elastic_blast_job_id(storage_account: str, job_id: str) -> str:
    if not storage_account or not job_id:
        return ""
    try:
        from api.services import get_credential
        from api.services.storage_data import _blob_service

        container = _blob_service(get_credential(), storage_account).get_container_client("results")
        prefix = f"{job_id}/job-"
        for blob in container.list_blobs(name_starts_with=prefix):
            name = str(blob.name or "")
            parts = name.split("/", 2)
            if len(parts) >= 2 and parts[1].startswith("job-"):
                return parts[1]
    except Exception as exc:
        LOGGER.debug("elastic blast job id discovery skipped job_id=%s: %s", job_id, exc)
    return ""


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


def _ensure_job_read_allowed(job_id: str, caller: CallerIdentity) -> None:
    """Authorise read access to a job for ``caller``.

    Fails CLOSED on Storage outage when MSAL auth is enforced — researchers
    would otherwise be able to read each other's jobs the moment the Table
    Storage owner index becomes unreachable. In dev-bypass mode the synthetic
    identity has no real OID and there is no multi-tenant isolation, so we
    fall through (researcher-on-their-own-laptop case).
    """
    dev_bypass = os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true"
    try:
        from api.services.state_repo import JobStateRepository

        state = JobStateRepository().get(job_id)
    except Exception as exc:
        if dev_bypass:
            return
        LOGGER.warning("job authorisation lookup failed (failing closed): %s", type(exc).__name__)
        raise HTTPException(503, {"code": "auth_lookup_unavailable"}) from exc
    if state and state.owner_oid and state.owner_oid != caller.object_id:
        raise HTTPException(403, "not owner")

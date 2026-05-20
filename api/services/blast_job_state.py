"""BLAST job projection, file preview, and refresh helpers."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

from api.auth import CallerIdentity

LOGGER = logging.getLogger(__name__)

from api.services.blast_external_jobs import (  # noqa: E402
    _EXTERNAL_DETAIL_ENRICH_LIMIT as _EXTERNAL_DETAIL_ENRICH_LIMIT,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _EXTERNAL_NOT_ENABLED_REASONS as _EXTERNAL_NOT_ENABLED_REASONS,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _exception_reason as _exception_reason,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _external_job_detail_or_row as _external_job_detail_or_row,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _external_list_jobs_cached as _external_list_jobs_cached,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _external_result_files as _external_result_files,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _external_status_to_dashboard as _external_status_to_dashboard,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _external_to_blast_job as _external_to_blast_job,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _merge_external_detail as _merge_external_detail,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _openapi_client_kwargs_from_cluster as _openapi_client_kwargs_from_cluster,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _reset_external_jobs_cache as _reset_external_jobs_cache,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _short_external_db_name as _short_external_db_name,
)
from api.services.blast_external_jobs import (  # noqa: E402
    _sync_external_jobs_to_table as _sync_external_jobs_to_table,
)


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


_PROGRESS_STEP_ORDER = (
    "preparing",
    "warming_up",
    "configuring",
    "staging_db",
    "submitting",
    "running",
    "exporting_results",
    "completed",
)


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


def _local_to_blast_job(
    state: Any,
    split_children: dict[str, Any] | None = None,
    *,
    include_database_metadata: bool = False,
) -> dict[str, Any]:
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
    if include_database_metadata:
        database_metadata = _database_metadata_for_response(
            db,
            str(infrastructure.get("storage_account") or ""),
        )
        if database_metadata is not None:
            out["database_metadata"] = database_metadata
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
    from datetime import UTC, datetime

    updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    step.setdefault("started_at", str(step.get("updated_at") or updated_at))
    step.update({"phase": phase, "status": status, "updated_at": updated_at, "k8s": k8s})
    if status == "completed":
        step["success"] = True
        step.setdefault("completed_at", updated_at)
    steps[step_key] = step
    if status != "failed" and step_key in _PROGRESS_STEP_ORDER:
        current_idx = _PROGRESS_STEP_ORDER.index(step_key)
        for previous_key in _PROGRESS_STEP_ORDER[:current_idx]:
            previous = steps.get(previous_key)
            if not isinstance(previous, dict) or previous.get("status") != "running":
                continue
            normalised = dict(previous)
            normalised.setdefault("started_at", str(previous.get("updated_at") or updated_at))
            normalised.update(
                {
                    "status": "completed",
                    "updated_at": updated_at,
                    "completed_at": updated_at,
                    "success": True,
                }
            )
            steps[previous_key] = normalised
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

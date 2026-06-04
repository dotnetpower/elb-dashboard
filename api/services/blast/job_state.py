"""BLAST job projection, file preview, and refresh helpers.

Responsibility: BLAST job projection, file preview, and refresh helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_payload_value`, `_queries_blob_path`, `_job_query_blob_path`,
`_refresh_running_blast_state`, `_blocked_refresh_reasons`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import logging
import os
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

from api.auth import CallerIdentity
from api.services.response_contracts import build_target

LOGGER = logging.getLogger(__name__)

from api.services.blast.external_jobs import (  # noqa: E402
    _EXTERNAL_DETAIL_ENRICH_LIMIT as _EXTERNAL_DETAIL_ENRICH_LIMIT,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _EXTERNAL_NOT_ENABLED_REASONS as _EXTERNAL_NOT_ENABLED_REASONS,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _exception_reason as _exception_reason,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _external_job_detail_or_row as _external_job_detail_or_row,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _external_list_jobs_cached as _external_list_jobs_cached,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _external_result_files as _external_result_files,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _external_status_to_dashboard as _external_status_to_dashboard,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _external_to_blast_job as _external_to_blast_job,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _merge_external_detail as _merge_external_detail,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _openapi_client_kwargs_from_cluster as _openapi_client_kwargs_from_cluster,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _reset_external_jobs_cache as _reset_external_jobs_cache,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _short_external_db_name as _short_external_db_name,
)
from api.services.blast.external_jobs import (  # noqa: E402
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
    raw_payload = getattr(state, "payload", None)
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    return payload


def _config_preview_from_payload(
    *,
    job_id: str,
    storage_account: str,
    payload: dict[str, Any],
) -> str:
    from api.tasks.blast import _build_config_content

    raw_options = payload.get("options")
    options = dict(raw_options) if isinstance(raw_options, dict) else {}
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

_K8S_REFRESH_PHASES = frozenset(
    {
        "submitted",
        "running",
        "results_pending",
    }
)
# Per-phase throttle. `submitted` keeps the original 20 s floor because the K8s
# job may not exist yet immediately after `elastic-blast submit` returns and
# repeated misses are wasteful. `running` and `results_pending` are the hot
# phases where the BLAST container is either close to or already finished, so
# we tighten the floor to 5 s — that turns the perceived "K8s finished →
# dashboard catches up" latency from ~20 s (or 60 s via beat) into ~5 s.
_K8S_REFRESH_MIN_INTERVAL_SECONDS = 20.0
_K8S_REFRESH_FAST_INTERVAL_SECONDS = 5.0
_K8S_REFRESH_FAST_PHASES = frozenset({"running", "results_pending"})
_K8S_REFRESH_LAST_CHECK: dict[tuple[str, str, str, str], float] = {}
_NON_ERROR_RUNNING_ERROR_CODES = frozenset({"blast_submit_lock_busy"})


def _refresh_min_interval_seconds(phase: str) -> float:
    if phase in _K8S_REFRESH_FAST_PHASES:
        return _K8S_REFRESH_FAST_INTERVAL_SECONDS
    return _K8S_REFRESH_MIN_INTERVAL_SECONDS


def _maybe_reload_with_payload(repo: Any, state: Any) -> Any:
    """Reload a row from the repo when its payload was omitted (list path).

    The list endpoint pulls rows with ``include_payload=False`` to keep the
    response small, but mutating the row (transitioning to results_pending /
    completed / failed) needs the existing ``_progress`` to merge step history
    rather than clobber it. This helper returns the original state on any
    error — tests use simplified Repo doubles without ``.get()``.
    """
    payload = getattr(state, "payload", None)
    if isinstance(payload, dict) and payload:
        return state
    get = getattr(repo, "get", None)
    if get is None:
        return state
    try:
        full = get(state.job_id)
    except Exception as exc:
        LOGGER.debug(
            "blast refresh payload reload skipped job_id=%s: %s",
            getattr(state, "job_id", ""),
            type(exc).__name__,
        )
        return state
    return full if full is not None else state


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


def _job_error_for_response(state: Any) -> str:
    error_code = str(getattr(state, "error_code", "") or "")
    if not error_code:
        return ""
    status = str(getattr(state, "status", "") or "").strip().casefold()
    if status == "running" and error_code in _NON_ERROR_RUNNING_ERROR_CODES:
        return ""
    return error_code


def _local_to_blast_job(
    state: Any,
    split_children: dict[str, Any] | None = None,
    *,
    include_database_metadata: bool = False,
    refresh_blocked_reason: str | None = None,
    cluster_power_state: str | None = None,
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
        "job_id_kind": "dashboard",
        "dashboard_job_id": state.job_id,
        "openapi_job_id": _payload_value(payload, "openapi_job_id"),
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
        "error": _job_error_for_response(state),
        "payload": payload,
        "config_snapshot": payload.get("config_snapshot") if isinstance(payload, dict) else None,
        "infrastructure": {k: v for k, v in infrastructure.items() if v not in (None, "")},
        "source": "dashboard",
        "owner_upn": getattr(state, "owner_upn", None) or None,
    }
    out["target"] = build_target(
        resource_type="blast_job",
        job_id=str(state.job_id),
        job_id_kind="dashboard",
        dashboard_job_id=str(state.job_id),
        openapi_job_id=_payload_value(payload, "openapi_job_id"),
        links={
            "dashboard_status": f"/api/blast/jobs/{state.job_id}",
            "events": f"/api/blast/jobs/{state.job_id}/events",
            "results": f"/api/blast/jobs/{state.job_id}/results",
        },
    )
    if progress is not None:
        out["custom_status"] = progress
        out["output"] = {
            "status": state.status,
            "phase": state.phase or state.status,
            "steps": progress.get("steps", {}),
        }
    if include_database_metadata:
        from api.services.blast.db_metadata import extract_trusted_storage_account

        # Jobs synced from the sibling OpenAPI store their blob-URL database
        # under payload.external.db and leave infrastructure.storage_account
        # empty. Recover the account (gated to the trusted workload account) so
        # the Storage-backed resolver fills the sequence/letter counts and
        # snapshot date, matching dashboard jobs. The trust gate stops an
        # attacker-influenced db URL from leaking the MI Storage token.
        storage_account = str(infrastructure.get("storage_account") or "")
        if not storage_account:
            external = payload.get("external") if isinstance(payload.get("external"), dict) else {}
            storage_account = extract_trusted_storage_account(
                db
            ) or extract_trusted_storage_account(str(external.get("db") or ""))
        database_metadata = _database_metadata_for_response(
            db,
            storage_account,
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
    # Cluster-stopped / cluster-missing rows can't be refreshed against the
    # K8s API, so an active row would otherwise show a frozen "running" state
    # forever. Tag it as stale + surface the ARM power_state so the SPA can
    # render a "status frozen — cluster stopped" badge instead of a false
    # in-progress signal. Only meaningful for rows still in an active state.
    if refresh_blocked_reason and str(getattr(state, "status", "") or "").strip().casefold() in (
        "running",
        "submitted",
    ):
        out["stale"] = True
        out["refresh_blocked_reason"] = refresh_blocked_reason
        if cluster_power_state:
            out["cluster_power_state"] = cluster_power_state
    return out


def _database_metadata_for_response(
    database: str,
    storage_account: str,
) -> dict[str, Any] | None:
    try:
        from api.services.blast.db_metadata import resolve_database_display_metadata

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
    """Re-check a row's scope after the OData query, with cluster precedence.

    Matches the storage-layer semantics in
    :meth:`JobStateRepository.list_for_scope`: when the caller asks for a
    specific ``cluster_name`` the row's ``resource_group`` is allowed to
    differ. The dashboard's workspace RG (where Storage / ACR live) and
    the cluster's own RG (typically ``rg-elb-cluster``) are different
    concepts; treating RG as a hard filter would silently drop jobs whose
    row was saved with the cluster RG. RG only acts as a hard filter when
    the caller did NOT pass a ``cluster_name``.
    """
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    sub_ok = _scope_value_matches(
        getattr(state, "subscription_id", None) or _payload_value(payload, "subscription_id"),
        subscription_id,
    )
    cluster_ok = _scope_value_matches(
        getattr(state, "cluster_name", None)
        or _payload_value(payload, "aks_cluster_name", "cluster_name"),
        cluster_name,
    )
    if cluster_name:
        return sub_ok and cluster_ok
    rg_ok = _scope_value_matches(
        getattr(state, "resource_group", None) or _payload_value(payload, "resource_group"),
        resource_group,
    )
    return sub_ok and rg_ok and cluster_ok


def _refresh_running_blast_state(repo: Any, state: Any) -> Any:
    if getattr(state, "type", "") != "blast" or getattr(state, "status", "") != "running":
        return state
    phase = str(getattr(state, "phase", "") or "").strip().casefold()
    if phase not in _K8S_REFRESH_PHASES:
        return state
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    # Prefer the indexed top-level columns so the refresh works for rows
    # returned by `list_for_owner(include_payload=False)` (the list endpoint
    # avoids the payload column to keep responses small).
    subscription_id = str(
        getattr(state, "subscription_id", None)
        or _payload_value(payload, "subscription_id")
        or ""
    )
    resource_group = str(
        getattr(state, "resource_group", None)
        or _payload_value(payload, "resource_group")
        or ""
    )
    cluster_name = str(
        getattr(state, "cluster_name", None)
        or _payload_value(payload, "cluster_name", "aks_cluster_name")
        or ""
    )
    storage_account = str(
        getattr(state, "storage_account", None) or _payload_value(payload, "storage_account") or ""
    )
    if not (subscription_id and resource_group and cluster_name):
        return state
    k8s_job_id = str(
        _payload_value(payload, "elastic_blast_job_id", "k8s_job_id")
        or _discover_elastic_blast_job_id(storage_account, str(state.job_id))
    )
    if not k8s_job_id:
        return state
    refresh_key = (str(state.job_id), subscription_id, resource_group, cluster_name)
    now = monotonic()
    last_check = _K8S_REFRESH_LAST_CHECK.get(refresh_key)
    if last_check is not None and now - last_check < _refresh_min_interval_seconds(phase):
        return state
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
        _K8S_REFRESH_LAST_CHECK[refresh_key] = now
        return state
    k8s_status = str(k8s.get("status") or "")
    if k8s_status not in {"completed", "failed"}:
        _K8S_REFRESH_LAST_CHECK[refresh_key] = now
        return state
    _K8S_REFRESH_LAST_CHECK.pop(refresh_key, None)
    # We are about to rewrite `_progress` in the Table — if this row was
    # fetched without its payload (list endpoint uses include_payload=False),
    # pulling the full row first preserves the existing step history.
    state = _maybe_reload_with_payload(repo, state)
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
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


def _row_refresh_scope(state: Any) -> tuple[str, str, str]:
    """Extract (subscription_id, resource_group, cluster_name) for a job row.

    Prefers the indexed top-level columns so it works for list rows fetched
    with ``include_payload=False``, falling back to the payload when present.
    """
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    subscription_id = str(
        getattr(state, "subscription_id", None) or _payload_value(payload, "subscription_id") or ""
    )
    resource_group = str(
        getattr(state, "resource_group", None) or _payload_value(payload, "resource_group") or ""
    )
    cluster_name = str(
        getattr(state, "cluster_name", None)
        or _payload_value(payload, "cluster_name", "aks_cluster_name")
        or ""
    )
    return subscription_id, resource_group, cluster_name


def _blocked_refresh_reasons(rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Map ``job_id -> ClusterHealth`` for active rows whose AKS cluster is down.

    The list endpoint consults this so it can (a) SKIP the K8s refresh for a
    stopped/missing cluster — which would otherwise burn a ~10 s K8s API
    timeout per job — and (b) tag the affected active rows as ``stale`` so the
    SPA renders a "status frozen — cluster stopped" badge instead of a false
    "running" signal that never advances.

    Cost: one cached ARM ``ManagedClusters.get`` per distinct
    (sub, rg, cluster) via ``get_cluster_health`` (90 s TTL), so a fleet of
    stopped jobs costs one ARM call, not one per job. Returns ``{}`` when there
    are no active rows, no usable scope, or credentials are unavailable —
    the gate is best-effort and never blocks the list response.
    """
    active = [
        row
        for row in rows
        if str(getattr(row, "status", "") or "").strip().casefold() in ("running", "submitted")
        and str(getattr(row, "phase", "") or "").strip().casefold() in _K8S_REFRESH_PHASES
    ]
    if not active:
        return {}
    scopes: dict[tuple[str, str, str], list[str]] = {}
    for row in active:
        subscription_id, resource_group, cluster_name = _row_refresh_scope(row)
        if not (subscription_id and resource_group and cluster_name):
            continue
        scopes.setdefault((subscription_id, resource_group, cluster_name), []).append(
            str(row.job_id)
        )
    if not scopes:
        return {}
    try:
        from api.services import get_credential
        from api.services.cluster_health import get_cluster_health

        credential = get_credential()
    except Exception as exc:
        LOGGER.debug("blocked-refresh health gate skipped (no credential): %s", type(exc).__name__)
        return {}
    blocked: dict[str, dict[str, Any]] = {}
    for (subscription_id, resource_group, cluster_name), job_ids in scopes.items():
        try:
            health = get_cluster_health(
                credential, subscription_id, resource_group, cluster_name
            )
        except Exception as exc:
            LOGGER.debug(
                "blocked-refresh health probe skipped cluster=%s: %s",
                cluster_name,
                type(exc).__name__,
            )
            continue
        # `get_cluster_health` degrades open (healthy=True) when ARM is
        # unreachable, so we only block on a proven stopped/missing cluster.
        if health.get("healthy", True):
            continue
        for job_id in job_ids:
            blocked[job_id] = dict(health)
    return blocked


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
    _raw_progress = out.get("_progress")
    progress = dict(_raw_progress) if isinstance(_raw_progress, dict) else {}
    _raw_steps = progress.get("steps")
    steps = dict(_raw_steps) if isinstance(_raw_steps, dict) else {}
    step_key = "exporting_results" if phase == "results_pending" else phase
    _raw_step = steps.get(step_key)
    step = dict(_raw_step) if isinstance(_raw_step, dict) else {}
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
        from api.services.blast.result_analytics import list_parseable_result_blobs

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
        from api.services.storage.data import _blob_service

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


def blast_shared_visibility_enabled() -> bool:
    """Return True when per-owner BLAST job isolation is relaxed.

    Development-stage switch. With ``BLAST_JOBS_SHARED_VISIBILITY=true`` every
    authenticated caller may list and open every job regardless of the row's
    ``owner_oid`` (the Recent searches page then shows all submitters' jobs).
    Default OFF preserves the production per-user privacy boundary; the route
    layer still requires ``require_caller`` either way. Flip this off before
    any multi-tenant / shared-subscription use.
    """
    return os.environ.get("BLAST_JOBS_SHARED_VISIBILITY", "").lower() == "true"


def _assert_job_owner(owner_oid: str | None, caller: CallerIdentity) -> None:
    """Raise ``403 not owner`` unless ``caller`` owns the job.

    No-ops when :func:`blast_shared_visibility_enabled` is on (dev stage) or
    the row carries no concrete ``owner_oid`` (cluster-shared / external rows).
    Centralises the per-route owner gate so the dev visibility switch has one
    authority instead of a dozen drifting inline comparisons.
    """
    if blast_shared_visibility_enabled():
        return
    if owner_oid and owner_oid != caller.object_id:
        raise HTTPException(403, "not owner")


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
    if state:
        _assert_job_owner(state.owner_oid, caller)


def _resolve_job_storage_account(job_id: str, supplied: str) -> str:
    """Reject a ``storage_account`` query parameter that does not match the
    JobState row for ``job_id``.

    This closes a confused-deputy gap on the job-bound BLAST routes
    (``/api/blast/jobs/{job_id}/...``): the caller passes
    ``storage_account=<x>`` as a query parameter, the api authenticates the
    request with the shared MI, and the MI very likely has Reader on a
    different storage account the caller chose. Without this gate a
    legitimate user could read result blobs from any storage account the
    MI can reach by lying about which account holds their job.

    Behaviour:

    * Authoritative record exists (JobState row carries a non-empty
      ``storage_account``) and ``supplied`` does not match → ``403
      cross_account_mismatch``. The error reveals only that a mismatch
      occurred; the recorded value is **not** echoed.
    * Authoritative record exists and matches → return the recorded
      value (used so downstream code is byte-identical regardless of
      caller-supplied casing).
    * Authoritative record does not record the storage account (legacy
      row written before the field landed, external sync row, or a
      submit-then-poll race before the row reaches Table Storage) → log
      a one-liner and return ``supplied`` unchanged. The fallback is
      intentional: a hard failure here would break the legacy job list.
    * ``AUTH_DEV_BYPASS=true`` and the lookup raises → return
      ``supplied`` (dev loop without a real state backend).
    """
    if not supplied:
        return supplied
    dev_bypass = os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true"
    try:
        from api.services.state_repo import JobStateRepository

        state = JobStateRepository().get_summary(job_id)
    except Exception as exc:
        if dev_bypass:
            return supplied
        LOGGER.warning(
            "storage account cross-check lookup failed job_id=%s err=%s (failing closed)",
            job_id,
            type(exc).__name__,
        )
        raise HTTPException(503, {"code": "auth_lookup_unavailable"}) from exc
    if state is None:
        # Job genuinely absent from Table Storage. This happens legitimately
        # right after submit (the row is being written) and on external sync
        # rows that have not been re-projected yet. Log the fallback so an
        # operator inspecting access logs can spot a route that is being
        # called with a bogus job_id repeatedly.
        LOGGER.info(
            "storage account cross-check: no JobState row for job_id=%s; "
            "accepting supplied value",
            job_id,
        )
        return supplied
    recorded = (getattr(state, "storage_account", "") or "").strip()
    if not recorded:
        LOGGER.info(
            "storage account cross-check: JobState has no recorded account; "
            "accepting supplied value job_id=%s",
            job_id,
        )
        return supplied
    if recorded.lower() != supplied.strip().lower():
        # Do NOT echo the recorded value to the caller — that would leak
        # the correct account name to anyone probing job_ids.
        raise HTTPException(
            403,
            {
                "code": "cross_account_mismatch",
                "message": (
                    "supplied storage_account does not match the account "
                    "recorded when this job was submitted"
                ),
            },
        )
    return recorded

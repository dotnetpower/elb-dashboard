"""Shared helpers and compatibility re-exports for route modules.

Responsibility: Shared helpers and compatibility re-exports for route modules
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_stub_log`, `_maybe_open_local_storage_access`, `_safe_delay`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
import threading as _threading
from typing import Any

from fastapi import Body, Depends, HTTPException, Path, Query

from api.auth import require_caller
from api.services.blast.job_state import (
    _EXTERNAL_DETAIL_ENRICH_LIMIT as _EXTERNAL_DETAIL_ENRICH_LIMIT,
)
from api.services.blast.job_state import (
    _EXTERNAL_NOT_ENABLED_REASONS as _EXTERNAL_NOT_ENABLED_REASONS,
)
from api.services.blast.job_state import (
    _assert_job_owner as _assert_job_owner,
)
from api.services.blast.job_state import (
    _blob_not_found as _blob_not_found,
)
from api.services.blast.job_state import (
    _blocked_refresh_reasons as _blocked_refresh_reasons,
)
from api.services.blast.job_state import (
    _config_preview_from_payload as _config_preview_from_payload,
)
from api.services.blast.job_state import (
    _ensure_job_read_allowed as _ensure_job_read_allowed,
)
from api.services.blast.job_state import (
    _exception_reason as _exception_reason,
)
from api.services.blast.job_state import (
    _external_job_detail_or_row as _external_job_detail_or_row,
)
from api.services.blast.job_state import (
    _external_list_jobs_cached as _external_list_jobs_cached,
)
from api.services.blast.job_state import (
    _external_result_files as _external_result_files,
)
from api.services.blast.job_state import (
    _external_status_to_dashboard as _external_status_to_dashboard,
)
from api.services.blast.job_state import (
    _external_to_blast_job as _external_to_blast_job,
)
from api.services.blast.job_state import (
    _job_payload_for_file_preview as _job_payload_for_file_preview,
)
from api.services.blast.job_state import (
    _job_query_blob_path as _job_query_blob_path,
)
from api.services.blast.job_state import (
    _local_state_matches_job_scope as _local_state_matches_job_scope,
)
from api.services.blast.job_state import (
    _local_to_blast_job as _local_to_blast_job,
)
from api.services.blast.job_state import (
    _merge_external_detail as _merge_external_detail,
)
from api.services.blast.job_state import (
    _openapi_client_kwargs_from_cluster as _openapi_client_kwargs_from_cluster,
)
from api.services.blast.job_state import (
    _payload_value as _payload_value,
)
from api.services.blast.job_state import (
    _queries_blob_path as _queries_blob_path,
)
from api.services.blast.job_state import (
    _refresh_running_blast_state as _refresh_running_blast_state,
)
from api.services.blast.job_state import (
    _reset_external_jobs_cache as _reset_external_jobs_cache,
)
from api.services.blast.job_state import (
    _resolve_job_storage_account as _resolve_job_storage_account,
)
from api.services.blast.job_state import (
    _scope_value_matches as _scope_value_matches,
)
from api.services.blast.job_state import (
    _short_external_db_name as _short_external_db_name,
)
from api.services.blast.job_state import (
    _split_child_summaries_from_repo as _split_child_summaries_from_repo,
)
from api.services.blast.job_state import (
    _split_child_summary_from_children as _split_child_summary_from_children,
)
from api.services.blast.job_state import (
    _split_child_summary_from_repo as _split_child_summary_from_repo,
)
from api.services.blast.job_state import (
    _sync_external_jobs_to_table as _sync_external_jobs_to_table,
)
from api.services.blast.job_state import (
    blast_shared_visibility_enabled as blast_shared_visibility_enabled,
)
from api.services.blast.submit_payload import (
    _apply_web_blast_searchsp_default as _apply_web_blast_searchsp_default,
)
from api.services.blast.submit_payload import (
    _normalise_blast_submit_body as _normalise_blast_submit_body,
)
from api.services.blast.submit_payload import (
    _submit_options_from_body as _submit_options_from_body,
)

LOGGER = logging.getLogger(__name__)

# Per-(account, db) lock registry for /api/blast/databases/{db}/shard.
# A shard daemon holds the lock for the lifetime of its background work
# so a re-clicked chip returns 409 instead of starting a second writer.
# The guard mutex protects insertions into the registry itself; the
# registry never shrinks (one entry per ever-touched DB) which is fine
# at the scale we operate at (low-tens of DBs per deployment).
_SHARD_LOCK_REGISTRY: dict[str, _threading.Lock] = {}
_SHARD_LOCK_REGISTRY_GUARD = _threading.Lock()
# Older than this and we treat a leftover sharding_in_progress=true
# marker as a crashed previous daemon (and allow re-trigger). Picked to
# be much larger than the worst-case wall time for ensure_shard_sets
# against the largest known DB while still small enough that a real
# crash recovers quickly.
_SHARD_STALE_SECONDS = 30 * 60
_WARMUP_RELEASE_BODY = Body(...)
_WARMUP_RELEASE_CALLER = Depends(require_caller)
_TAXONOMY_SEARCH_QUERY = Query(..., min_length=1, max_length=120)
_TAXONOMY_SEARCH_LIMIT = Query(default=10, ge=1, le=20)
_TAXONOMY_DETAIL_PATH = Path(..., ge=1, le=10_000_000_000)
_TAXONOMY_IMAGE_NAME = Query(..., min_length=1, max_length=120)
_TAXONOMY_TREE_PATH = Path(..., ge=1, le=10_000_000_000)
_TAXONOMY_TREE_SIBLING_LIMIT = Query(default=3, ge=1, le=8)
_OPENAPI_PROXY_ALLOWED_HEADERS = frozenset({"accept", "content-type", "x-client-request-id"})


def _stub_log(name: str, **ctx: Any) -> None:
    LOGGER.warning("STUB_CALLED endpoint=%s ctx=%s", name, ctx)


def _maybe_open_local_storage_access(
    credential: Any,
    subscription_id: str,
    resource_group: str,
    storage_account: str,
    *,
    context: str,
) -> dict[str, Any]:
    """Best-effort local-debug Storage public access guard.

    This is a no-op unless LOCAL_DEBUG_AUTO_OPEN_STORAGE is truthy and the api
    process is not running inside Container Apps. It only runs when the route
    has the full ARM scope needed to update the Storage account firewall.
    """
    if not (subscription_id and resource_group and storage_account):
        return {"action": "noop", "reason": "missing storage ARM scope"}
    from api.services.storage.public_access import ensure_local_storage_access

    access = ensure_local_storage_access(
        credential,
        subscription_id,
        resource_group,
        storage_account,
    )
    if access.get("action") == "failed":
        LOGGER.warning(
            "%s: local-debug auto-open failed for %s: %s",
            context,
            storage_account,
            access.get("error"),
        )
    return access


def _safe_delay(task: Any, **kwargs: Any) -> Any:
    """Enqueue a Celery task, returning the AsyncResult. If the broker is
    unreachable (Redis down), raise 503 with a retryable hint instead of
    letting the OperationalError bubble as a 500."""
    try:
        return task.delay(**kwargs)
    except Exception as exc:
        if "Connection refused" in str(exc) or "OperationalError" in type(exc).__name__:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "broker_unavailable",
                    "message": (
                        "Task broker (Redis) is not reachable. The task cannot be queued right now."
                    ),
                    "retryable": True,
                    "retry_after_seconds": 30,
                },
            ) from exc
        raise


def _safe_send_task(task_name: str, *, queue: str | None = None, **kwargs: Any) -> Any:
    """Enqueue a Celery task through the configured app, not shared_task current_app."""
    try:
        from api.celery_app import celery_app

        return celery_app.send_task(task_name, kwargs=kwargs, queue=queue)
    except Exception as exc:
        if "Connection refused" in str(exc) or "OperationalError" in type(exc).__name__:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "broker_unavailable",
                    "message": (
                        "Task broker (Redis) is not reachable. The task cannot be queued right now."
                    ),
                    "retryable": True,
                    "retry_after_seconds": 30,
                },
            ) from exc
        raise

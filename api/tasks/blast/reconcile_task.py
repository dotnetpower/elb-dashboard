"""Celery beat task that reconciles stale BLAST rows against Celery / K8s / OpenAPI.

Responsibility: Bring Table Storage back in sync when a worker died
mid-flight by walking active rows and asking Celery, the K8s API, and
the external OpenAPI plane (in that order) for the latest status.
Edit boundaries: Reconcile-specific helpers (``_reconcile_row_k8s_status``,
``_celery_success_row_status``) live here; cross-cutting helpers
(``_external_reconcile_job_id``, ``_storage_account_from_row``,
``_has_parseable_result_artifact``, ``_enqueue_artifact_finalizer``,
``_snippet``, ``_exception_detail_snippet``) stay in ``api.tasks.blast``
and are called through the module attribute for monkeypatch safety.
Key entry points:
  - ``reconcile_stale_jobs`` (``@shared_task``
     ``name="api.tasks.blast.reconcile_stale_jobs"``, scheduled every
     60 s by Celery beat).
Risky contracts: Idempotent — calling twice is a no-op if the first
pass brought every active row to a terminal state. Public task name
must stay ``api.tasks.blast.reconcile_stale_jobs`` (referenced from
``api/celery_app.py`` beat schedule).
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC
from typing import Any

from celery import shared_task

from api.tasks import blast as _blast
from api.tasks.blast.progress import _merge_progress_payload

LOGGER = logging.getLogger(__name__)

__all__ = (
    "_celery_success_row_status",
    "_reconcile_row_k8s_status",
    "reconcile_stale_jobs",
)


def _reconcile_row_k8s_status(
    repo: Any,
    row: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    elastic_blast_job_id: str,
) -> str:
    if not (subscription_id and resource_group and cluster_name and elastic_blast_job_id):
        return ""
    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_check_blast_status

        k8s = k8s_check_blast_status(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            namespace="default",
            job_id=elastic_blast_job_id,
        )
    except Exception as exc:
        LOGGER.info(
            "reconcile_stale_jobs: k8s refresh skipped job_id=%s elastic_blast_job_id=%s: %s",
            row.job_id,
            elastic_blast_job_id,
            type(exc).__name__,
        )
        return ""

    k8s_status = str(k8s.get("status") or "")
    if k8s_status == "completed":
        if _blast._has_parseable_result_artifact(
            _blast._storage_account_from_row(row), str(row.job_id)
        ):
            status, phase, outcome = "completed", "completed", "completed"
        else:
            status, phase, outcome = "running", "results_pending", "results_pending"
    elif k8s_status == "failed":
        status, phase, outcome = "failed", "failed", "failed"
    elif k8s_status == "running":
        status, phase, outcome = "running", "running", "running"
    elif k8s_status == "creating":
        status, phase, outcome = "running", "submitted", "running"
    else:
        return ""

    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else None
    merged_payload = _merge_progress_payload(
        payload,
        phase=phase,
        status=status,
        error_code="",
        details={"k8s": k8s, "source": "k8s_reconcile"},
    )
    repo.update(row.job_id, status=status, phase=phase, payload=merged_payload)
    if status in {"completed", "failed"}:
        _blast._enqueue_artifact_finalizer(row.job_id, phase, status)
    return outcome


def _celery_success_row_status(row: Any, result: Any) -> tuple[str, str]:
    if not isinstance(result, Mapping):
        return "completed", "completed"
    status = str(result.get("status") or "").lower()
    phase = str(result.get("phase") or status or "completed")
    if status == "running":
        return "running", phase or "submitted"
    if status == "failed":
        return "failed", phase or "failed"
    if status == "completed" and not _blast._has_parseable_result_artifact(
        _blast._storage_account_from_row(row),
        str(row.job_id),
    ):
        return "running", "results_pending"
    return "completed", phase or "completed"


@shared_task(name="api.tasks.blast.reconcile_stale_jobs", bind=True)
def reconcile_stale_jobs(
    self: Any,
    *,
    stale_threshold_seconds: int = 600,
    limit: int = 200,
) -> dict[str, Any]:
    """Bring Table Storage back in sync when a worker died mid-flight.

    Scans all jobstate rows with an active status (``queued`` / ``pending``
    / ``running`` / ``reducing``) and refreshes them by:

     1. Asking Celery for the task result. ``FAILURE`` or revoked tasks
         become ``failed``; completed submit tasks continue into runtime
         reconciliation while terminal task results become ``completed``.
     2. Refreshing the Kubernetes runtime status for accepted ElasticBLAST
         jobs and waiting in ``results_pending`` until parseable result
         artifacts exist.
     3. Falling back to the external OpenAPI plane when Celery has no
         record (worker died, broker lost the message, etc.).
     4. Marking rows ``failed`` with ``error_code=worker_lost`` when no
       upstream still knows about the job and the row has been quiet for
       longer than ``stale_threshold_seconds``.

    Runs every minute via the beat schedule registered in
    ``api/celery_app.py``. Idempotent — calling it twice in a row is a
    no-op if the first pass already brought every row to a terminal
    state.
    """
    del self
    from datetime import datetime

    from celery.result import AsyncResult

    from api.celery_app import celery_app

    summary: dict[str, Any] = {
        "scanned": 0,
        "completed": 0,
        "failed": 0,
        "worker_lost": 0,
        "k8s_refreshed": 0,
        "results_pending": 0,
        "external_refreshed": 0,
        "untouched": 0,
        "errors": 0,
    }

    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
    except Exception as exc:
        LOGGER.warning("reconcile_stale_jobs: state repo unavailable: %s", exc)
        summary["errors"] = 1
        return summary

    try:
        active_rows = repo.list_active(job_type="blast", limit=limit)
    except Exception as exc:
        LOGGER.warning("reconcile_stale_jobs: list_active failed: %s", exc)
        summary["errors"] = 1
        return summary

    summary["scanned"] = len(active_rows)
    now = datetime.now(UTC)

    for row in active_rows:
        try:
            task_id = (row.task_id or "").strip()
            celery_status: str | None = None
            celery_result: Any = None
            if task_id:
                try:
                    async_result = AsyncResult(task_id, app=celery_app)
                    celery_status = str(async_result.status or "").upper()
                    if celery_status in {"SUCCESS", "FAILURE"}:
                        celery_result = async_result.result
                except Exception as exc:
                    LOGGER.debug(
                        "reconcile_stale_jobs: AsyncResult failed job_id=%s: %s",
                        row.job_id,
                        type(exc).__name__,
                    )

            submit_task_completed_active = False

            # 1) Celery reports a terminal state. A completed submit task can
            # still leave an active runtime job in AKS, so active rows continue
            # into the K8s/OpenAPI reconciliation path below.
            if celery_status == "SUCCESS":
                status, phase = _celery_success_row_status(row, celery_result)
                if status == "completed":
                    if row.status != status or row.phase != phase:
                        repo.update(row.job_id, status=status, phase=phase)
                    _blast._enqueue_artifact_finalizer(row.job_id, phase, status)
                    summary["completed"] += 1
                    continue
                if row.status != status or row.phase != phase:
                    repo.update(row.job_id, status=status, phase=phase)
                submit_task_completed_active = True
            if celery_status in {"FAILURE", "REVOKED"}:
                err = (
                    _blast._snippet(celery_result) if celery_result is not None else "task_failed"
                )
                # Go through `_update_state` (which runs `_merge_progress_payload`)
                # rather than `repo.update(...)` directly. The merge sweeps any
                # orphan `status: "running"` step entries that the crashed worker
                # left behind so the dashboard timeline does not keep spinning
                # on, e.g., `submitting` while the parent row is `failed`.
                _blast._update_state(
                    row.job_id,
                    "failed",
                    status="failed",
                    event="reconcile_celery_terminal",
                    error_code=err[:120],
                )
                summary["failed"] += 1
                continue

            # 2) External OpenAPI may know the latest status when the
            #    local worker died but the BLAST runtime in AKS is still
            #    making progress.
            payload = row.payload or {}
            sub = payload.get("subscription_id") or row.subscription_id or ""
            rg = payload.get("resource_group") or row.resource_group or ""
            cluster = (
                payload.get("cluster_name")
                or payload.get("aks_cluster_name")
                or row.cluster_name
                or ""
            )
            refreshed = False
            external_job_id = _blast._external_reconcile_job_id(row)
            k8s_outcome = _reconcile_row_k8s_status(
                repo,
                row,
                subscription_id=str(sub),
                resource_group=str(rg),
                cluster_name=str(cluster),
                elastic_blast_job_id=external_job_id,
            )
            if k8s_outcome:
                summary["k8s_refreshed"] += 1
                if k8s_outcome == "completed":
                    summary["completed"] += 1
                elif k8s_outcome == "failed":
                    summary["failed"] += 1
                elif k8s_outcome == "results_pending":
                    summary["results_pending"] += 1
                else:
                    summary["untouched"] += 1
                continue
            if sub and rg and cluster and external_job_id:
                try:
                    from api.routes._blast_shared import (
                        _external_to_blast_job,
                        _openapi_client_kwargs_from_cluster,
                    )
                    from api.services import external_blast

                    kwargs = _openapi_client_kwargs_from_cluster(sub, rg, cluster)
                    if kwargs:
                        detail = external_blast.get_job(external_job_id, **kwargs)
                        converted = _external_to_blast_job(detail)
                        ext_status = str(converted.get("status") or "")
                        ext_phase = str(converted.get("phase") or ext_status)
                        if ext_status and (ext_status != row.status or ext_phase != row.phase):
                            repo.update(
                                row.job_id,
                                status=ext_status,
                                phase=ext_phase,
                            )
                            summary["external_refreshed"] += 1
                            refreshed = True
                            if ext_status in {"completed", "failed"}:
                                _blast._enqueue_artifact_finalizer(
                                    row.job_id, ext_phase, ext_status
                                )
                                # Counted under external_refreshed; do not
                                # double-count under completed/failed.
                                pass
                except Exception as exc:
                    LOGGER.warning(
                        "reconcile_stale_jobs: external refresh failed job_id=%s "
                        "subscription_id=%s resource_group=%s cluster=%s error_type=%s "
                        "status_code=%s detail=%s",
                        row.job_id,
                        sub,
                        rg,
                        cluster,
                        type(exc).__name__,
                        getattr(exc, "status_code", ""),
                        _blast._exception_detail_snippet(exc),
                    )
            if refreshed:
                continue

            if submit_task_completed_active:
                summary["untouched"] += 1
                continue

            # 3) Nobody knows the job and it has been quiet for a while.
            try:
                updated_at = datetime.fromisoformat(
                    (row.updated_at or row.created_at or "").replace("Z", "+00:00")
                )
            except Exception:
                updated_at = now  # never mark recently-created rows lost
            quiet_seconds = (now - updated_at).total_seconds()
            if quiet_seconds >= stale_threshold_seconds:
                # Mirror the FAILURE/REVOKED branch above: route through
                # `_update_state` so orphan running step entries get demoted
                # to `failed` and the UI stops spinning.
                _blast._update_state(
                    row.job_id,
                    "worker_lost",
                    status="failed",
                    event="reconcile_worker_lost",
                    error_code="worker_lost",
                )
                summary["worker_lost"] += 1
            else:
                summary["untouched"] += 1
        except Exception as exc:
            LOGGER.warning(
                "reconcile_stale_jobs: row failed job_id=%s: %s",
                row.job_id,
                type(exc).__name__,
            )
            summary["errors"] += 1

    progress_made = (
        summary["completed"]
        or summary["failed"]
        or summary["worker_lost"]
        or summary["k8s_refreshed"]
        or summary["external_refreshed"]
    )
    if progress_made:
        LOGGER.info(
            "reconcile_stale_jobs: scanned=%(scanned)d completed=%(completed)d "
            "failed=%(failed)d worker_lost=%(worker_lost)d k8s_refreshed=%(k8s_refreshed)d "
            "results_pending=%(results_pending)d external_refreshed=%(external_refreshed)d "
            "errors=%(errors)d",
            summary,
        )
    return summary

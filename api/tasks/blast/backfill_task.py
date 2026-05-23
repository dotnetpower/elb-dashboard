"""Celery task that backfills K8s container runtime metrics for completed BLAST jobs.

Responsibility: Backfill ``blast_container_duration_ms`` /
``results_export_container_duration_ms`` onto completed BLAST rows whose
K8s job still exposes container termination timestamps.
Edit boundaries: Side-effect entry point + 5 helper functions that compose
runtime scope and payload merging. Cross-task helpers
(``_external_reconcile_job_id``, ``_storage_account_from_row``,
``_discover_elastic_blast_job_id``) live in ``api.tasks.blast`` and are
called through the module attribute so tests can monkeypatch them.
Key entry points:
  - ``backfill_completed_runtime_metrics`` (``@shared_task``
     ``name="api.tasks.blast.backfill_completed_runtime_metrics"``,
     scheduled every 5 minutes by Celery beat).
Risky contracts: Idempotent — rows that already carry container runtime
metrics are skipped before any K8s call. Public task name must stay
``api.tasks.blast.backfill_completed_runtime_metrics`` (referenced from
``api/celery_app.py`` beat schedule).
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from celery import shared_task

from api.tasks import blast as _blast

LOGGER = logging.getLogger(__name__)

__all__ = (
    "_backfill_completed_row_runtime_metrics",
    "_completed_row_runtime_job_id",
    "_completed_row_runtime_scope",
    "_payload_with_backfilled_runtime_metrics",
    "_row_has_container_runtime_metrics",
    "backfill_completed_runtime_metrics",
)


def _row_has_container_runtime_metrics(row: Any) -> bool:
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else {}
    progress = payload.get("_progress") if isinstance(payload, Mapping) else None
    steps = progress.get("steps") if isinstance(progress, Mapping) else None
    running = steps.get("running") if isinstance(steps, Mapping) else None
    k8s = running.get("k8s") if isinstance(running, Mapping) else None
    return isinstance(k8s, Mapping) and any(
        k8s.get(key) not in (None, "")
        for key in ("blast_container_duration_ms", "results_export_container_duration_ms")
    )


def _completed_row_runtime_job_id(row: Any) -> str:
    root_job_id = _blast._external_reconcile_job_id(row)
    if root_job_id:
        return root_job_id
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else {}
    progress = payload.get("_progress") if isinstance(payload, Mapping) else None
    steps = progress.get("steps") if isinstance(progress, Mapping) else None
    running = steps.get("running") if isinstance(steps, Mapping) else None
    k8s = running.get("k8s") if isinstance(running, Mapping) else None
    if isinstance(k8s, Mapping):
        runtime_job_id = str(k8s.get("job_id") or "").strip()
        if runtime_job_id.startswith("job-"):
            return runtime_job_id
    return _blast._discover_elastic_blast_job_id(
        _blast._storage_account_from_row(row), str(row.job_id)
    )


def _completed_row_runtime_scope(row: Any) -> tuple[str, str, str, str]:
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else {}
    subscription_id = str(
        payload.get("subscription_id") or getattr(row, "subscription_id", "") or ""
    )
    resource_group = str(
        payload.get("resource_group") or getattr(row, "resource_group", "") or ""
    )
    cluster_name = str(
        payload.get("cluster_name")
        or payload.get("aks_cluster_name")
        or getattr(row, "cluster_name", "")
        or ""
    )
    return subscription_id, resource_group, cluster_name, _completed_row_runtime_job_id(row)


def _backfill_completed_row_runtime_metrics(repo: Any, row: Any) -> str:
    if _row_has_container_runtime_metrics(row):
        return "skipped"
    subscription_id, resource_group, cluster_name, elastic_blast_job_id = (
        _completed_row_runtime_scope(row)
    )
    if not (subscription_id and resource_group and cluster_name and elastic_blast_job_id):
        return "skipped"
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
            "completed runtime backfill skipped job_id=%s elastic_blast_job_id=%s: %s",
            row.job_id,
            elastic_blast_job_id,
            type(exc).__name__,
        )
        return "error"
    if str(k8s.get("status") or "") != "completed":
        return "skipped"
    if not any(
        k8s.get(key) not in (None, "")
        for key in ("blast_container_duration_ms", "results_export_container_duration_ms")
    ):
        return "skipped"
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else None
    merged_payload = _payload_with_backfilled_runtime_metrics(payload, k8s)
    repo.update(
        row.job_id,
        status="completed",
        phase="completed",
        payload=merged_payload,
        updated_at=getattr(row, "updated_at", None),
    )
    try:
        repo.append_history(
            row.job_id,
            "k8s_completed_runtime_backfilled",
            {"status": "completed", "phase": "completed", "k8s": k8s},
        )
    except Exception as exc:
        LOGGER.debug(
            "completed runtime backfill history skipped job_id=%s: %s",
            row.job_id,
            type(exc).__name__,
        )
    return "backfilled"


def _payload_with_backfilled_runtime_metrics(
    payload: Mapping[str, Any] | None,
    k8s: Mapping[str, Any],
) -> dict[str, Any]:
    out = deepcopy(dict(payload or {}))
    progress = out.get("_progress") if isinstance(out.get("_progress"), dict) else {}
    steps = progress.get("steps") if isinstance(progress.get("steps"), dict) else {}
    running = steps.get("running") if isinstance(steps.get("running"), dict) else {}
    existing_k8s = running.get("k8s") if isinstance(running.get("k8s"), dict) else {}
    running["k8s"] = {**existing_k8s, **dict(k8s)}
    running.setdefault("phase", "running")
    running.setdefault("status", "completed")
    if not running.get("started_at") and k8s.get("started_at"):
        running["started_at"] = k8s["started_at"]
    if not running.get("completed_at") and k8s.get("completed_at"):
        running["completed_at"] = k8s["completed_at"]
    if k8s.get("started_at") and k8s.get("completed_at"):
        running.setdefault("duration_source", "k8s_runtime")
    steps["running"] = running
    progress["steps"] = steps
    progress.setdefault("phase", "completed")
    progress.setdefault("status", "completed")
    out["_progress"] = progress
    return out


@shared_task(name="api.tasks.blast.backfill_completed_runtime_metrics", bind=True)
def backfill_completed_runtime_metrics(
    self: Any,
    *,
    job_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Backfill K8s container runtime metrics for completed BLAST jobs.

    Side effects: updates completed dashboard job payloads when the K8s job
    still exposes container termination timestamps. Idempotent: rows that
    already carry container runtime metrics are skipped before any K8s call.
    """
    del self
    summary: dict[str, Any] = {
        "scanned": 0,
        "backfilled": 0,
        "skipped": 0,
        "errors": 0,
    }
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        if job_id:
            row = repo.get(job_id)
            rows = [row] if row is not None and row.status == "completed" else []
        else:
            rows = repo.list_completed(job_type="blast", limit=limit)
    except Exception as exc:
        LOGGER.warning("backfill_completed_runtime_metrics: list failed: %s", exc)
        summary["errors"] = 1
        return summary

    summary["scanned"] = len(rows)
    for row in rows:
        try:
            outcome = _backfill_completed_row_runtime_metrics(repo, row)
            if outcome == "backfilled":
                summary["backfilled"] += 1
            elif outcome == "error":
                summary["errors"] += 1
            else:
                summary["skipped"] += 1
        except Exception as exc:
            LOGGER.warning(
                "backfill_completed_runtime_metrics: row failed job_id=%s: %s",
                row.job_id,
                type(exc).__name__,
            )
            summary["errors"] += 1
    if summary["backfilled"] or summary["errors"]:
        LOGGER.info(
            "backfill_completed_runtime_metrics: scanned=%(scanned)d "
            "backfilled=%(backfilled)d skipped=%(skipped)d errors=%(errors)d",
            summary,
        )
    return summary

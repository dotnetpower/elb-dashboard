"""Celery task that cancels a BLAST job by deleting its labelled K8s Jobs.

Responsibility: Cancel a BLAST job (parent or children) via the direct
Kubernetes API and update Table Storage rows to ``cancelled``.
Edit boundaries: Side-effect entry point only — config building, state
schema, and retry shaping live in ``api.tasks.blast`` shared helpers
(``_update_state`` / ``_progress`` / ``_retry_or_fail`` / ``_snippet``).
Key entry points: ``cancel`` (``@shared_task name="api.tasks.blast.cancel"``).
Risky contracts: Children rows are marked ``cancelled`` only when the K8s
delete returns ``cancelled``/``unknown``; partial failures route through
``_retry_or_fail`` with ``error_code="cancel_retryable_failure"``. The
public task name must stay ``api.tasks.blast.cancel`` — it is referenced
from ``api/routes/blast/jobs.py`` and Celery routes.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

from api.tasks import blast as _blast

__all__ = ("cancel",)


@shared_task(name="api.tasks.blast.cancel", bind=True, max_retries=2)
def cancel(
    self: Any,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
) -> dict[str, Any]:
    """Cancel a BLAST job by deleting its labelled Kubernetes Jobs."""

    _blast._progress(self, "cancelling")
    _blast._update_state(job_id, "cancelling")

    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_cancel_blast_job
        from api.services.state_repo import JobStateRepository

        credential = get_credential()
        repo = JobStateRepository()
        children = list(repo.list_children(job_id, limit=1000))
        target_job_ids = [str(child.job_id) for child in children] or [job_id]
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for target_job_id in target_job_ids:
            result = k8s_cancel_blast_job(
                credential,
                subscription_id,
                resource_group,
                cluster_name,
                namespace="default",
                job_id=target_job_id,
            )
            results.append({"job_id": target_job_id, **result})
            if result.get("status") in {"cancelled", "unknown"}:
                if target_job_id != job_id:
                    repo.update(target_job_id, status="cancelled", phase="cancelled")
                    repo.append_history(
                        target_job_id,
                        "cancelled_by_parent",
                        {"parent_job_id": job_id},
                    )
                continue
            errors.append({"job_id": target_job_id, "errors": result.get("errors")})
    except Exception as exc:
        return _blast._retry_or_fail(
            self,
            job_id=job_id,
            phase="cancel_unavailable",
            exc=exc,
            error_code="cancel_unavailable",
        )

    if not errors:
        child_count = len(target_job_ids) if target_job_ids != [job_id] else 0
        _blast._update_state(
            job_id,
            "cancelled",
            status="cancelled",
            k8s={"targets": results},
            child_count=child_count,
            storage_account=storage_account,
        )
        return {"job_id": job_id, "status": "cancelled", "k8s": {"targets": results}}

    error = _blast._snippet(errors or "Kubernetes cancellation did not complete")
    return _blast._retry_or_fail(
        self,
        job_id=job_id,
        phase="cancel_retryable_failure",
        exc=RuntimeError(error),
        error_code="cancel_retryable_failure",
        retry_after_seconds=30,
    )

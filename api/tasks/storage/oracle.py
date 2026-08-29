"""Celery execution for DB order-oracle Kubernetes fan-out.

Responsibility: Revalidate one claimed oracle identity, dispatch/adopt its
    deterministic per-shard Jobs, bounded-poll them, validate exact parts, and
    publish or terminally fail the run with progress checkpoints.
Edit boundaries: Long-running task orchestration only; readiness calculation,
    durable Blob transitions, Job manifests, and runtime classification live
    in focused service modules.
Key entry points: `build_db_order_oracle` (Celery task
    `api.tasks.storage.build_db_order_oracle`).
Risky contracts: Domain failure raises after durable `failed` writes so Celery
    cannot report SUCCESS for a failed oracle; every publication revalidates
    source/layout identity; polling and K8s-error tolerance are bounded.
Validation: `uv run pytest -q api/tests/test_oracle_task.py`.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from celery import shared_task

import api.tasks.storage as _facade
from api.services.env import env_int
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

_BUILD_TIMEOUT_SECONDS = env_int("ORACLE_BUILD_TIMEOUT_SECONDS", 1800, minimum=60, maximum=7200)
_POLL_SECONDS = env_int("ORACLE_BUILD_POLL_SECONDS", 5, minimum=1, maximum=60)
_K8S_ERROR_LIMIT = env_int("ORACLE_BUILD_K8S_ERROR_LIMIT", 5, minimum=1, maximum=20)
_K8S_ERROR_GRACE_SECONDS = env_int(
    "ORACLE_BUILD_K8S_ERROR_GRACE_SECONDS", 60, minimum=5, maximum=300
)
_FAILED_JOB_LOG_TAIL_LINES = 20
_SOFT_TIME_LIMIT = _BUILD_TIMEOUT_SECONDS + 60
_HARD_TIME_LIMIT = _BUILD_TIMEOUT_SECONDS + 120


class OracleTaskFailed(RuntimeError):
    """Raised after a durable terminal failure has been recorded."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _checkpoint(
    task: Any,
    *,
    job_id: str,
    container: Any,
    db_name: str,
    run_id: str,
    owner_operation_id: str,
    phase: str,
    **extra: Any,
) -> None:
    from api.services.db.oracle_state import update_oracle_active, update_oracle_run

    _facade._record_task_progress(task, phase, run_id=run_id, **extra)
    _facade._update_state(job_id, phase, status="running", run_id=run_id, **extra)
    update_oracle_run(
        container,
        db_name=db_name,
        run_id=run_id,
        owner_operation_id=owner_operation_id,
        updates={"phase": phase, "status": "running", "updated_at": _now_iso(), **extra},
    )
    update_oracle_active(
        container,
        db_name=db_name,
        owner_operation_id=owner_operation_id,
        updates={"phase": phase, "status": "running", "updated_at": _now_iso(), **extra},
    )


def _terminal_failure(
    *,
    job_id: str,
    container: Any,
    db_name: str,
    run_id: str,
    owner_operation_id: str,
    error_code: str,
    message: str,
    automatic: bool,
    phase: str = "failed",
) -> NoReturn:
    from api.services.db.oracle_state import fail_oracle_run

    safe_message = sanitise(message)[:300]
    if automatic:
        try:
            from api.services.db.oracle_retry import record_automation_failure

            record_automation_failure(
                container,
                db_name=db_name,
                run_id=run_id,
                error_code=error_code,
            )
        except Exception as exc:
            LOGGER.warning(
                "oracle automation failure state skipped run_id=%s reason=%s",
                run_id,
                type(exc).__name__,
            )
            _facade._update_state(
                job_id,
                "failure_state_pending",
                status="running",
                run_id=run_id,
                error_code=error_code,
                error=safe_message,
            )
            raise OracleTaskFailed(
                f"oracle automation failure recovery pending: {error_code}"
            ) from exc
    fail_oracle_run(
        container,
        db_name=db_name,
        run_id=run_id,
        owner_operation_id=owner_operation_id,
        error_code=error_code,
        error=safe_message,
        finished_at=_now_iso(),
    )
    _facade._update_state(
        job_id,
        phase,
        status="failed",
        run_id=run_id,
        error_code=error_code,
        error=safe_message,
    )
    from api.services.feature_events import record_feature_event

    record_feature_event(
        "oracle_build",
        status="failed",
        job_id=job_id,
        run_id=run_id,
        database=db_name,
        phase=phase,
        error_code=error_code,
        automatic=automatic,
    )
    raise OracleTaskFailed(f"{error_code}: {safe_message}")


def _failed_job_message(
    credential: Any,
    *,
    subscription_id: str,
    cluster_resource_group: str,
    cluster_name: str,
    namespace: str,
    run_id: str,
    failed_jobs: tuple[str, ...],
) -> str:
    """Return a bounded diagnostic for the first failed Job before cleanup."""
    summary = f"failed Jobs: {', '.join(failed_jobs)}"
    if not failed_jobs:
        return summary
    failed_job = failed_jobs[0]
    try:
        from api.services.k8s.workload_ops import k8s_job_logs

        logs = k8s_job_logs(
            credential,
            subscription_id,
            cluster_resource_group,
            cluster_name,
            namespace,
            failed_job,
            tail_lines=_FAILED_JOB_LOG_TAIL_LINES,
        )
    except Exception as exc:
        LOGGER.warning(
            "oracle failed Job logs unavailable run_id=%s job=%s reason=%s",
            run_id,
            failed_job,
            type(exc).__name__,
        )
        return summary
    lines = [line.strip() for line in sanitise(logs).splitlines() if line.strip()]
    if not lines:
        return summary
    return f"{summary}; log tail: {' | '.join(lines[-4:])}"


@shared_task(
    name="api.tasks.storage.build_db_order_oracle",
    bind=True,
    soft_time_limit=_SOFT_TIME_LIMIT,
    time_limit=_HARD_TIME_LIMIT,
)
def build_db_order_oracle(
    self: Any,
    *,
    job_id: str,
    run_id: str,
    owner_operation_id: str,
    subscription_id: str,
    storage_resource_group: str,
    storage_account: str,
    cluster_resource_group: str,
    cluster_name: str,
    db_name: str,
    image: str,
    identity: str,
    requested_source_version: str = "",
    automatic: bool = False,
    dispatch_token: str = "",
) -> dict[str, Any]:
    """Build one complete, generation-bound DB order oracle.

    Side effects: reads Storage/AKS/Kubernetes state; creates and deletes
    run-scoped Kubernetes Jobs; updates run/current/active Blob documents and
    the pre-created JobState row.
    """
    from api.services import get_credential
    from api.services.db.oracle_build import (
        OracleBuildBlocked,
        resolve_oracle_build_context,
    )
    from api.services.db.oracle_runtime import (
        classify_oracle_jobs,
        cleanup_oracle_jobs,
        validate_oracle_parts,
    )
    from api.services.db.oracle_state import (
        OracleBuildOwnershipLost,
        claim_oracle_execution,
        oracle_container,
        promote_oracle_run,
        read_oracle_active,
        read_oracle_current,
        release_oracle_active,
    )
    from api.services.db.order_oracle import (
        build_db_order_oracle_job_plan,
        oracle_part_blob_path,
    )
    from api.services.k8s.monitoring import (
        k8s_ensure_job_manifests,
        k8s_get_jobs,
    )

    credential = get_credential()
    container = oracle_container(credential, storage_account)
    current = read_oracle_current(container, db_name)
    if isinstance(current, dict) and str(current.get("run_id") or "") == run_id:
        active = read_oracle_active(container, db_name)
        owns_active = (
            isinstance(active, dict)
            and str(active.get("run_id") or "") == run_id
            and str(active.get("owner_operation_id") or "") == owner_operation_id
        )
        if active is None or owns_active:
            try:
                from api.services.db.oracle_retry import record_automation_success

                record_automation_success(
                    container,
                    db_name=db_name,
                    run_id=run_id,
                    require_current_run=automatic,
                )
            except Exception as exc:
                LOGGER.warning(
                    "oracle adopted success state skipped run_id=%s reason=%s",
                    run_id,
                    type(exc).__name__,
                )
                if automatic:
                    raise OracleTaskFailed(
                        "oracle published automation recovery pending"
                    ) from exc
        if owns_active:
            try:
                release_oracle_active(
                    container,
                    db_name=db_name,
                    owner_operation_id=owner_operation_id,
                )
            except Exception as exc:
                LOGGER.warning(
                    "oracle published active cleanup skipped run_id=%s reason=%s",
                    run_id,
                    type(exc).__name__,
                )
                raise OracleTaskFailed("oracle published active release recovery pending") from exc
        _facade._update_state(job_id, "completed", status="completed", run_id=run_id)
        from api.services.feature_events import record_feature_event

        record_feature_event(
            "oracle_build",
            status="completed",
            job_id=job_id,
            run_id=run_id,
            database=db_name,
            automatic=automatic,
            adopted=True,
        )
        return {"status": "completed", "run_id": run_id, "adopted": True}
    active = read_oracle_active(container, db_name)
    if not isinstance(active, dict):
        raise OracleBuildOwnershipLost("oracle active claim no longer exists")
    if str(active.get("owner_operation_id") or "") != owner_operation_id:
        raise OracleBuildOwnershipLost("oracle active claim ownership changed")
    if str(active.get("run_id") or "") != run_id:
        raise OracleBuildOwnershipLost("oracle active run changed")
    execution_instance_id = uuid.uuid4().hex
    if not claim_oracle_execution(
        container,
        db_name=db_name,
        run_id=run_id,
        owner_operation_id=owner_operation_id,
        dispatch_token=dispatch_token,
        execution_instance_id=execution_instance_id,
        started_at=_now_iso(),
        deadline_at=(datetime.now(UTC) + timedelta(seconds=_HARD_TIME_LIMIT)).isoformat(
            timespec="seconds"
        ),
    ):
        return {
            "status": "superseded",
            "run_id": run_id,
            "reason": "stale_or_duplicate_delivery",
        }

    _checkpoint(
        self,
        job_id=job_id,
        container=container,
        db_name=db_name,
        run_id=run_id,
        owner_operation_id=owner_operation_id,
        phase="checking_prerequisites",
    )
    try:
        context = resolve_oracle_build_context(
            credential,
            subscription_id=subscription_id,
            storage_resource_group=storage_resource_group,
            storage_account=storage_account,
            cluster_resource_group=cluster_resource_group,
            cluster_name=cluster_name,
            db_name=db_name,
            requested_source_version=requested_source_version,
        )
    except OracleBuildBlocked as exc:
        _terminal_failure(
            job_id=job_id,
            container=container,
            db_name=db_name,
            run_id=run_id,
            owner_operation_id=owner_operation_id,
            error_code=exc.code,
            message=str(exc),
            automatic=automatic,
        )
    if context.identity != identity:
        _terminal_failure(
            job_id=job_id,
            container=container,
            db_name=db_name,
            run_id=run_id,
            owner_operation_id=owner_operation_id,
            error_code="oracle_identity_changed",
            message="database generation or shard layout changed before dispatch",
            automatic=automatic,
            phase="superseded",
        )

    plan = build_db_order_oracle_job_plan(
        db_name=db_name,
        storage_account=storage_account,
        run_id=run_id,
        shard_nodes=list(context.shard_nodes),
        image=image,
    )
    job_names = [str(job["metadata"]["name"]) for job in plan.jobs]

    def _cleanup_jobs() -> dict[str, Any]:
        cleanup: dict[str, Any] = cleanup_oracle_jobs(
            credential,
            subscription_id=subscription_id,
            cluster_resource_group=cluster_resource_group,
            cluster_name=cluster_name,
            namespace=plan.namespace,
            job_names=job_names,
        )
        return cleanup

    _checkpoint(
        self,
        job_id=job_id,
        container=container,
        db_name=db_name,
        run_id=run_id,
        owner_operation_id=owner_operation_id,
        phase="dispatching",
        expected_parts=context.expected_parts,
        namespace=plan.namespace,
        job_names=job_names,
    )
    try:
        dispatch = k8s_ensure_job_manifests(
            credential,
            subscription_id,
            cluster_resource_group,
            cluster_name,
            list(plan.jobs),
        )
    except Exception as exc:
        _cleanup_jobs()
        _terminal_failure(
            job_id=job_id,
            container=container,
            db_name=db_name,
            run_id=run_id,
            owner_operation_id=owner_operation_id,
            error_code="oracle_k8s_dispatch_unreachable",
            message=f"Kubernetes Job dispatch failed: {type(exc).__name__}",
            automatic=automatic,
        )
    if dispatch.get("error_count") or dispatch.get("errors"):
        _cleanup_jobs()
        _terminal_failure(
            job_id=job_id,
            container=container,
            db_name=db_name,
            run_id=run_id,
            owner_operation_id=owner_operation_id,
            error_code="oracle_dispatch_failed",
            message=str(dispatch.get("errors") or [])[:300],
            automatic=automatic,
        )

    deadline = time.monotonic() + _BUILD_TIMEOUT_SECONDS
    last_signature: object = None
    consecutive_k8s_errors = 0
    first_k8s_error_at: float | None = None
    while True:
        if time.monotonic() >= deadline:
            _cleanup_jobs()
            _terminal_failure(
                job_id=job_id,
                container=container,
                db_name=db_name,
                run_id=run_id,
                owner_operation_id=owner_operation_id,
                error_code="oracle_timeout",
                message=f"oracle build exceeded {_BUILD_TIMEOUT_SECONDS} seconds",
                automatic=automatic,
                phase="timeout",
            )
        try:
            jobs = k8s_get_jobs(
                credential,
                subscription_id,
                cluster_resource_group,
                cluster_name,
                plan.namespace,
            )
            progress = classify_oracle_jobs(job_names, jobs)
            consecutive_k8s_errors = 0
            first_k8s_error_at = None
        except Exception as exc:
            consecutive_k8s_errors += 1
            error_time = time.monotonic()
            if first_k8s_error_at is None:
                first_k8s_error_at = error_time
            error_elapsed = error_time - first_k8s_error_at
            if (
                consecutive_k8s_errors >= _K8S_ERROR_LIMIT
                and error_elapsed >= _K8S_ERROR_GRACE_SECONDS
            ):
                _cleanup_jobs()
                _terminal_failure(
                    job_id=job_id,
                    container=container,
                    db_name=db_name,
                    run_id=run_id,
                    owner_operation_id=owner_operation_id,
                    error_code="oracle_k8s_unreachable",
                    message=f"Kubernetes status failed: {type(exc).__name__}",
                    automatic=automatic,
                )
            if error_time >= deadline:
                continue
            error_sleep = min(
                30,
                _POLL_SECONDS * (2 ** min(consecutive_k8s_errors - 1, 3)),
            )
            time.sleep(min(error_sleep, max(0.0, deadline - error_time)))
            continue
        if progress is not None and progress.signature != last_signature:
            _checkpoint(
                self,
                job_id=job_id,
                container=container,
                db_name=db_name,
                run_id=run_id,
                owner_operation_id=owner_operation_id,
                phase="waiting_parts",
                ready_parts=len(progress.complete),
                failed_parts=len(progress.failed),
                missing_jobs=len(progress.missing),
            )
            last_signature = progress.signature
        if progress is not None and progress.status == "failed":
            failure_message = _failed_job_message(
                credential,
                subscription_id=subscription_id,
                cluster_resource_group=cluster_resource_group,
                cluster_name=cluster_name,
                namespace=plan.namespace,
                run_id=run_id,
                failed_jobs=progress.failed,
            )
            _cleanup_jobs()
            _terminal_failure(
                job_id=job_id,
                container=container,
                db_name=db_name,
                run_id=run_id,
                owner_operation_id=owner_operation_id,
                error_code="oracle_job_failed",
                message=failure_message,
                automatic=automatic,
            )
        if progress is not None and progress.status == "complete":
            break
        time.sleep(_POLL_SECONDS)

    expected_paths = [oracle_part_blob_path(db_name, run_id, shard) for shard in context.shards]
    part_prefix = str(active.get("part_prefix") or "")
    try:
        parts = validate_oracle_parts(
            container,
            expected_paths=expected_paths,
            part_prefix=part_prefix,
        )
    except Exception as exc:
        _cleanup_jobs()
        _terminal_failure(
            job_id=job_id,
            container=container,
            db_name=db_name,
            run_id=run_id,
            owner_operation_id=owner_operation_id,
            error_code="oracle_parts_validation_failed",
            message=f"Oracle part validation failed: {type(exc).__name__}",
            automatic=automatic,
        )
    if not parts["ready"]:
        _cleanup_jobs()
        _terminal_failure(
            job_id=job_id,
            container=container,
            db_name=db_name,
            run_id=run_id,
            owner_operation_id=owner_operation_id,
            error_code="oracle_parts_incomplete",
            message=(
                f"missing={parts['missing']} empty={parts['empty']} "
                f"unexpected={parts['unexpected']}"
            )[:300],
            automatic=automatic,
        )

    _checkpoint(
        self,
        job_id=job_id,
        container=container,
        db_name=db_name,
        run_id=run_id,
        owner_operation_id=owner_operation_id,
        phase="publishing",
        ready_parts=parts["ready_parts"],
    )
    try:
        latest_context = resolve_oracle_build_context(
            credential,
            subscription_id=subscription_id,
            storage_resource_group=storage_resource_group,
            storage_account=storage_account,
            cluster_resource_group=cluster_resource_group,
            cluster_name=cluster_name,
            db_name=db_name,
            requested_source_version=requested_source_version,
        )
    except OracleBuildBlocked as exc:
        _cleanup_jobs()
        _terminal_failure(
            job_id=job_id,
            container=container,
            db_name=db_name,
            run_id=run_id,
            owner_operation_id=owner_operation_id,
            error_code=exc.code,
            message=str(exc),
            automatic=automatic,
            phase="superseded",
        )
    if latest_context.identity != identity:
        _cleanup_jobs()
        _terminal_failure(
            job_id=job_id,
            container=container,
            db_name=db_name,
            run_id=run_id,
            owner_operation_id=owner_operation_id,
            error_code="oracle_identity_changed",
            message="database generation or shard layout changed before publication",
            automatic=automatic,
            phase="superseded",
        )

    terminal = promote_oracle_run(
        container,
        db_name=db_name,
        run_id=run_id,
        owner_operation_id=owner_operation_id,
        ready_document={
            "phase": "completed",
            "ready_parts": parts["ready_parts"],
            "expected_parts": parts["expected_parts"],
            "finished_at": _now_iso(),
        },
        release_active=False,
    )
    try:
        from api.services.db.oracle_retry import record_automation_success

        record_automation_success(
            container,
            db_name=db_name,
            run_id=run_id,
            require_current_run=automatic,
        )
    except Exception as exc:
        LOGGER.warning(
            "oracle automation success state skipped run_id=%s reason=%s",
            run_id,
            type(exc).__name__,
        )
        if automatic:
            raise OracleTaskFailed("oracle published automation recovery pending") from exc
    try:
        release_oracle_active(
            container,
            db_name=db_name,
            owner_operation_id=owner_operation_id,
        )
    except Exception as exc:
        LOGGER.warning(
            "oracle published active release skipped run_id=%s reason=%s",
            run_id,
            type(exc).__name__,
        )
        raise OracleTaskFailed("oracle published active release recovery pending") from exc
    cleanup = _cleanup_jobs()
    if cleanup["errors"]:
        LOGGER.warning(
            "oracle Job cleanup incomplete run_id=%s errors=%s",
            run_id,
            cleanup["errors"],
        )
    _facade._update_state(
        job_id,
        "completed",
        status="completed",
        run_id=run_id,
        ready_parts=parts["ready_parts"],
    )
    from api.services.feature_events import record_feature_event

    record_feature_event(
        "oracle_build",
        status="completed",
        job_id=job_id,
        run_id=run_id,
        database=db_name,
        automatic=automatic,
        cleanup_error_count=len(cleanup["errors"]),
    )
    return {
        "status": "completed",
        "run_id": run_id,
        "identity": identity,
        "ready_parts": parts["ready_parts"],
        "expected_parts": parts["expected_parts"],
        "cleanup_errors": cleanup["errors"],
        "current": terminal,
    }


__all__ = ["OracleTaskFailed", "build_db_order_oracle"]

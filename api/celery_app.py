"""Celery application configuration for worker and beat sidecars.

Responsibility: Celery app configuration, sidecar startup hooks, and terminal
task-failure visibility for worker and beat sidecars.
Edit boundaries: Keep changes scoped to Celery app wiring/signals and update
nearby tests.
Key entry points: `_start_reporter`, `_on_worker_init`,
`_on_worker_process_init`, `_on_beat_init`, `_on_task_failure`,
`_on_task_revoked`.
Risky contracts: Failure signal handlers must never raise; task crashes must
still leave a log entry and, when a JobState row can be found, a user-visible
failed/cancelled state.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from celery import Celery
from celery.signals import (
    beat_init,
    before_task_publish,
    task_failure,
    task_internal_error,
    task_revoked,
    worker_init,
    worker_process_init,
)

LOGGER = logging.getLogger(__name__)

# Mirror api.main's azure SDK silencer for worker/beat. Without this the
# Azure SDK http_logging_policy dumps full request/response headers on every
# Table/Blob/ARM call at INFO, drowning LAW (~750k lines / 24h observed in
# rg-elb-dashboard). Override with AZURE_LOG_LEVEL=DEBUG when debugging.
_azure_log_level = os.environ.get("AZURE_LOG_LEVEL", "WARNING").upper()
for _name in (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.identity._internal.decorators",
    "azure.identity._credentials.default",
    "urllib3.connectionpool",
    "httpx",
):
    logging.getLogger(_name).setLevel(_azure_log_level)

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")

celery_app = Celery(
    "elb_control_plane",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    set_as_current=True,
    include=[
        "api.tasks",
        "api.tasks.azure",
        "api.tasks.acr",
        "api.tasks.blast",
        "api.tasks.blast_artifacts",
        "api.tasks.storage",
        "api.tasks.openapi",
        "api.tasks.upgrade",
    ],
)
# Belt-and-braces: force this Celery instance to be both `default_app`
# and the top of `_task_stack` even if some other module instantiated a
# Celery app first. Without this, `shared_task.delay()` from the api
# sidecar would resolve `current_app` to a phantom default Celery app
# (broker=amqp://, queue="celery", routes={}) and the task would land in
# a queue the worker doesn't subscribe to -> tasks silently never run.
celery_app.set_default()
celery_app.set_current()

_TASK_SOFT_TIME_LIMIT = int(os.environ.get("CELERY_TASK_SOFT_TIME_LIMIT", "3300"))
_TASK_TIME_LIMIT = int(os.environ.get("CELERY_TASK_TIME_LIMIT", "3600"))
if _TASK_SOFT_TIME_LIMIT >= _TASK_TIME_LIMIT:
    raise ValueError("CELERY_TASK_SOFT_TIME_LIMIT must be < CELERY_TASK_TIME_LIMIT")
_RESULT_EXPIRES_SECONDS = int(os.environ.get("CELERY_RESULT_EXPIRES", "3600"))
if _RESULT_EXPIRES_SECONDS > 7200:
    raise ValueError("CELERY_RESULT_EXPIRES must be <= 7200 seconds")

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=max(
        1, int(os.environ.get("CELERY_WORKER_PREFETCH_MULTIPLIER", "4"))
    ),
    worker_max_tasks_per_child=int(
        os.environ.get("CELERY_WORKER_MAX_TASKS_PER_CHILD", "200")
    ),
    task_soft_time_limit=_TASK_SOFT_TIME_LIMIT,
    task_time_limit=_TASK_TIME_LIMIT,
    result_expires=_RESULT_EXPIRES_SECONDS,
    broker_connection_retry_on_startup=True,
    task_default_queue="default",
    task_routes={
        "api.tasks.azure.*": {"queue": "azure"},
        "api.tasks.acr.*": {"queue": "acr"},
        "api.tasks.blast.artifacts.*": {"queue": "blast-artifacts"},
        "api.tasks.blast.*": {"queue": "blast"},
        "api.tasks.storage.*": {"queue": "storage"},
        "api.tasks.openapi.*": {"queue": "azure"},
    },
    beat_schedule={
        "auto-warmup-reconcile": {
            "task": "api.tasks.storage.reconcile_auto_warmup",
            "schedule": float(os.environ.get("CELERY_BEAT_AUTO_WARMUP_SECONDS", "120")),
            "options": {"queue": "storage"},
        },
        "blast-reconcile-stale-jobs": {
            "task": "api.tasks.blast.reconcile_stale_jobs",
            "schedule": float(os.environ.get("CELERY_BEAT_BLAST_RECONCILE_SECONDS", "90")),
            "options": {"queue": "blast"},
        },
        "blast-backfill-completed-runtime-metrics": {
            "task": "api.tasks.blast.backfill_completed_runtime_metrics",
            "schedule": 300.0,
            "options": {"queue": "blast"},
        },
        "upgrade-check-latest": {
            "task": "api.tasks.upgrade.check_latest",
            "schedule": 1800.0,
            "options": {"queue": "default"},
        },
        "upgrade-reconcile-rolling-out": {
            "task": "api.tasks.upgrade.reconcile_rolling_out",
            "schedule": float(os.environ.get("CELERY_BEAT_UPGRADE_RECONCILE_SECONDS", "180")),
            "options": {"queue": "default"},
        },
        "upgrade-purge-orphan-tags": {
            "task": "api.tasks.upgrade.purge_orphan_acr_tags",
            "schedule": 24 * 60 * 60.0,
            "options": {"queue": "default"},
        },
        "upgrade-compact-history": {
            "task": "api.tasks.upgrade.compact_history",
            "schedule": 7 * 24 * 60 * 60.0,
            "options": {"queue": "default"},
        },
        "aks-reconcile-stale-provisions": {
            "task": "api.tasks.azure.reconcile_stale_aks_provisions",
            "schedule": float(os.environ.get("CELERY_BEAT_AKS_PROVISION_RECONCILE_SECONDS", "300")),
            "options": {"queue": "azure"},
        },
        "aks-idle-autostop-evaluate": {
            "task": "api.tasks.azure.evaluate_idle_clusters",
            "schedule": float(os.environ.get("CELERY_BEAT_AKS_IDLE_AUTOSTOP_SECONDS", "300")),
            "options": {"queue": "azure"},
        },
        "openapi-public-https-reconcile": {
            "task": "api.tasks.openapi.reconcile_public_https",
            "schedule": float(os.environ.get("CELERY_BEAT_OPENAPI_PUBLIC_HTTPS_SECONDS", "120")),
            "options": {"queue": "azure"},
        },
    },
    timezone="UTC",
    enable_utc=True,
)


def _start_reporter(sender_name: str) -> None:
    """Start the sidecar cgroup reporter for worker or beat."""
    if os.environ.get("SIDECAR_REPORTER_DISABLED", "").lower() == "true":
        return
    try:
        from api.services.cgroup_reporter import start_in_thread

        name = os.environ.get("SIDECAR_NAME", sender_name)
        start_in_thread(name)
    except Exception:
        LOGGER.warning(
            "cgroup reporter failed to start in %s", sender_name, exc_info=True
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _task_job_id(kwargs: dict[str, Any] | None) -> str:
    value = (kwargs or {}).get("job_id")
    return str(value) if value else ""


def _task_name(sender: Any) -> str:
    return str(getattr(sender, "name", None) or sender or "unknown")


def _record_task_terminal_state(
    *,
    task_id: str | None,
    task_name: str,
    status: str,
    phase: str,
    message: str,
    error_code: str,
    job_id: str = "",
) -> None:
    """Best-effort JobState update for Celery terminal signals."""
    if not task_id and not job_id:
        return
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(job_id) if job_id else None
        if state is None and task_id:
            state = repo.find_by_task_id(task_id)
        if state is None:
            return
        payload = dict(getattr(state, "payload", {}) or {})
        payload["terminal_task_event"] = {
            "task_id": task_id or "",
            "task_name": task_name,
            "phase": phase,
            "status": status,
            "message": message[:500],
            "error_code": error_code[:128],
            "recorded_at": _now_iso(),
        }
        repo.update(
            state.job_id,
            status=status,
            phase=phase,
            error_code=error_code[:128],
            payload=payload,
        )
        repo.append_history(
            state.job_id,
            phase,
            {
                "status": status,
                "task_id": task_id or "",
                "task_name": task_name,
                "message": message[:1000],
                "error_code": error_code[:128],
            },
        )
    except Exception as exc:
        LOGGER.warning(
            "celery terminal state record failed task_id=%s task=%s err=%s",
            task_id,
            task_name,
            type(exc).__name__,
        )


@worker_init.connect  # type: ignore[untyped-decorator]
def _on_worker_init(**_kwargs: object) -> None:
    _start_reporter("worker")


@worker_process_init.connect  # type: ignore[untyped-decorator]
def _on_worker_process_init(**_kwargs: object) -> None:
    try:
        from api.app.telemetry import init_telemetry

        init_telemetry(role="worker")
    except Exception:
        LOGGER.debug("worker telemetry init skipped", exc_info=True)


@beat_init.connect  # type: ignore[untyped-decorator]
def _on_beat_init(**_kwargs: object) -> None:
    try:
        from api.app.telemetry import init_telemetry

        init_telemetry(role="beat")
    except Exception:
        LOGGER.debug("beat telemetry init skipped", exc_info=True)
    _start_reporter("beat")


@task_failure.connect  # type: ignore[untyped-decorator]
def _on_task_failure(
    sender: Any = None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    kwargs: dict[str, Any] | None = None,
    einfo: Any = None,
    **_signal_kwargs: Any,
) -> None:
    task_name = _task_name(sender)
    job_id = _task_job_id(kwargs)
    exc_name = type(exception).__name__ if exception is not None else "TaskFailure"
    message = str(exception or exc_name)
    LOGGER.error(
        "celery_task_failed task_id=%s task=%s job_id=%s exc=%s message=%s",
        task_id,
        task_name,
        job_id or "-",
        exc_name,
        message[:500],
        exc_info=getattr(einfo, "exc_info", None),
    )
    _record_task_terminal_state(
        task_id=task_id,
        task_name=task_name,
        job_id=job_id,
        status="failed",
        phase="celery_task_failed",
        message=message,
        error_code=exc_name,
    )


@task_internal_error.connect  # type: ignore[untyped-decorator]
def _on_task_internal_error(
    sender: Any = None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    kwargs: dict[str, Any] | None = None,
    **_signal_kwargs: Any,
) -> None:
    task_name = _task_name(sender)
    job_id = _task_job_id(kwargs)
    exc_name = type(exception).__name__ if exception is not None else "TaskInternalError"
    message = str(exception or exc_name)
    LOGGER.error(
        "celery_task_internal_error task_id=%s task=%s job_id=%s exc=%s message=%s",
        task_id,
        task_name,
        job_id or "-",
        exc_name,
        message[:500],
    )
    _record_task_terminal_state(
        task_id=task_id,
        task_name=task_name,
        job_id=job_id,
        status="failed",
        phase="celery_internal_error",
        message=message,
        error_code=exc_name,
    )


@task_revoked.connect  # type: ignore[untyped-decorator]
def _on_task_revoked(
    sender: Any = None,
    request: Any = None,
    terminated: bool = False,
    expired: bool = False,
    signum: int | None = None,
    **_signal_kwargs: Any,
) -> None:
    task_id = str(getattr(request, "id", "") or "") or None
    task_name = _task_name(sender or getattr(request, "task", None))
    kwargs = getattr(request, "kwargs", None)
    job_id = _task_job_id(kwargs if isinstance(kwargs, dict) else None)
    status = "failed" if expired else "cancelled"
    phase = "celery_task_expired" if expired else "celery_task_revoked"
    message = f"Task revoked terminated={terminated} expired={expired} signum={signum}"
    LOGGER.warning(
        "celery_task_revoked task_id=%s task=%s job_id=%s terminated=%s expired=%s signum=%s",
        task_id,
        task_name,
        job_id or "-",
        terminated,
        expired,
        signum,
    )
    _record_task_terminal_state(
        task_id=task_id,
        task_name=task_name,
        job_id=job_id,
        status=status,
        phase=phase,
        message=message,
        error_code=phase,
    )


_PRODUCER_ROLE = os.environ.get("SIDECAR_NAME", "api")


@before_task_publish.connect  # type: ignore[untyped-decorator]
def _on_before_task_publish(**_kwargs: object) -> None:
    from api.services.event_emitter import ROW_ASYNC, ROW_SCHED, emit

    emit(ROW_SCHED if _PRODUCER_ROLE == "beat" else ROW_ASYNC)

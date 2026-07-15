"""Celery signal handlers for sidecar startup and task-failure visibility.

Responsibility: Register Celery signal handlers (worker/beat startup,
task failure / internal-error / revoked, before-task-publish row metrics)
that the api / worker / beat sidecars share. Keeps signal-handler logic
out of the Celery app-config module so each file owns one concern.
Edit boundaries: Signal handlers and their direct helpers only. Celery
app instantiation, queue routing, and beat schedule live in
`api.celery_app`. JobState row schema lives in
`api.services.state_repo`.
Key entry points: `_start_reporter`, `_is_background_consumer_worker`, `_on_worker_init`,
`_on_worker_process_init`, `_on_beat_init`, `_on_task_failure`,
`_on_task_internal_error`, `_on_task_revoked`, `_on_before_task_publish`,
`_record_task_terminal_state`.
Risky contracts: Failure signal handlers must never raise; task crashes
must still leave a log entry and, when a JobState row can be found, a
user-visible failed/cancelled state. Module is imported for its
import-time side effect (signal registration); never lazy-load it from a
worker task. Resident background consumers must start only in the dedicated
`worker-reconcile` parent, never once per Celery worker parent.
Validation: `uv run pytest -q api/tests/test_celery_failure_visibility.py
api/tests/test_telemetry_init.py`.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from celery.signals import (
    beat_init,
    before_task_publish,
    task_failure,
    task_internal_error,
    task_revoked,
    worker_init,
    worker_process_init,
    worker_shutdown,
)

LOGGER = logging.getLogger(__name__)


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


def _is_background_consumer_worker(sender: object | None) -> bool:
    """Return whether this Celery parent owns sidecar-level daemon consumers.

    `worker_init` fires once per parent. The worker sidecar now has three
    parents, so process-local singleton guards alone would start three Service
    Bus consumers. Unknown senders preserve the historical fail-open behaviour
    for direct test/dev invocation; named production workers are strict.
    """
    hostname = str(getattr(sender, "hostname", "") or "")
    if not hostname:
        return True
    return hostname.split("@", 1)[0] == "worker-reconcile"


def _reset_inherited_client_pools() -> None:
    """Drop network clients inherited across the Celery prefork boundary.

    ``requests.Session`` / urllib3 pools, Azure SDK pipelines, and credential
    token caches are process-local objects. Reusing a copy created before
    ``fork()`` produced corrupt TLS state under load (random X509/decrypt
    errors) and stale keep-alive sockets. Each reset is best-effort; a child
    must still boot when an optional client module is unavailable.
    """
    resets = (
        ("api.services", "reset_credential"),
        ("api.services.azure_clients", "reset_mgmt_client_pool"),
        ("api.services.k8s.monitoring", "reset_k8s_credential_cache"),
        ("api.services.k8s.monitoring", "reset_k8s_session_pool"),
        ("api.services.state_repo", "reset_state_repo_cache"),
    )
    for module_name, attr in resets:
        try:
            module = __import__(module_name, fromlist=[attr])
            getattr(module, attr)()
        except Exception as exc:
            LOGGER.debug("worker child %s reset skipped: %s", attr, type(exc).__name__)


@worker_init.connect  # type: ignore[untyped-decorator]
def _on_worker_init(sender: object | None = None, **_kwargs: object) -> None:
    _start_reporter("worker")
    if not _is_background_consumer_worker(sender):
        return
    # Optional resident Service Bus consumer (issue #36 Tier 3, default-OFF).
    # Starts a single daemon loop on the worker-reconcile parent when
    # SERVICEBUS_RESIDENT_CONSUMER is enabled, so SB-submitted jobs drain within
    # ~1 s instead of waiting the 30 s beat. No-op when the gate is off; the beat
    # drain task stays registered as the fallback either way.
    try:
        from api.services.blast.resident_consumer import start_resident_consumer

        start_resident_consumer()
    except Exception:
        LOGGER.debug("resident consumer start skipped", exc_info=True)
    # Optional demo external-completion consumer (default-OFF). Subscribes to the
    # completion topic on a dedicated subscription and records observations for
    # the Playground. Purely observational — never executes BLAST.
    try:
        from api.services.service_bus_external_consumer import start_external_consumer

        start_external_consumer()
    except Exception:
        LOGGER.debug("external completion consumer start skipped", exc_info=True)


@worker_shutdown.connect  # type: ignore[untyped-decorator]
def _on_worker_shutdown(**_kwargs: object) -> None:
    try:
        from api.services.blast.resident_consumer import stop_resident_consumer

        stop_resident_consumer()
    except Exception:
        LOGGER.debug("resident consumer stop skipped", exc_info=True)
    try:
        from api.services.service_bus_external_consumer import stop_external_consumer

        stop_external_consumer()
    except Exception:
        LOGGER.debug("external completion consumer stop skipped", exc_info=True)


@worker_process_init.connect  # type: ignore[untyped-decorator]
def _on_worker_process_init(**_kwargs: object) -> None:
    _reset_inherited_client_pools()
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

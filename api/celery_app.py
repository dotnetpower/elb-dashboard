"""Celery application configuration for worker and beat sidecars.

Responsibility: Celery application configuration for worker and beat sidecars
Edit boundaries: Keep changes scoped to this module responsibility and update nearby tests.
Key entry points: `_start_reporter`, `_on_worker_init`, `_on_beat_init`
Risky contracts: Keep imports lightweight and preserve existing public contracts.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import os

from celery import Celery

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
# a queue the worker doesn't subscribe to → tasks silently never run.
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
    # Recycle worker children every N tasks so allocator fragmentation +
    # one-shot leaks in long-running deps (xml parsers, gzip buffers, K8s
    # clients, Azure SDK pipelines) cannot accumulate into a GB-sized RSS
    # over thousands of BLAST submits. 200 tasks is a comfortable cadence —
    # high enough that the cold-start cost (Python interpreter + Celery
    # import) stays amortised, low enough that the steady-state RSS is
    # bounded. Override via env if a particular sidecar wants a tighter or
    # looser bound.
    worker_max_tasks_per_child=int(
        os.environ.get("CELERY_WORKER_MAX_TASKS_PER_CHILD", "200")
    ),
    # Hard ceiling on every task — guards against a stuck terminal_exec
    # stream, hung Storage call, or runaway Kubernetes wait blocking a
    # worker slot forever. Soft limit fires SoftTimeLimitExceeded so the
    # task can checkpoint / clean up; hard limit kills the worker child if
    # the soft handler hangs too. The 1-hour ceiling matches the longest
    # legit submit we have observed (sharded prepare-db); shorter tasks
    # (poll, reconcile) finish in seconds so this never trips for them.
    # The BLAST submit task itself is wrapped in its own retry loop so a
    # soft-timeout pushes the work to the retry, not a dead end.
    task_soft_time_limit=_TASK_SOFT_TIME_LIMIT,
    task_time_limit=_TASK_TIME_LIMIT,
    # Drop Celery result payloads after one hour so the result Redis db
    # does not retain GB of stale dicts. Routes that need a result poll
    # ``AsyncResult(...)`` within the first hour; anything older has been
    # reflected into ``state_repo`` already.
    result_expires=_RESULT_EXPIRES_SECONDS,
    # Silence the Celery 6.0 deprecation warning by opting in explicitly.
    # We want broker connection retries on startup so the worker comes up
    # gracefully even if Redis takes a few seconds to become reachable.
    broker_connection_retry_on_startup=True,
    task_default_queue="default",
    task_routes={
        "api.tasks.azure.*": {"queue": "azure"},
        "api.tasks.acr.*": {"queue": "acr"},
        "api.tasks.blast.artifacts.*": {"queue": "blast-artifacts"},
        "api.tasks.blast.*": {"queue": "blast"},
        "api.tasks.storage.*": {"queue": "storage"},
        # OpenAPI deploys talk to AKS / MSI / Authorization, same as
        # `provision_aks` — share the azure queue.
        "api.tasks.openapi.*": {"queue": "azure"},
    },
    beat_schedule={
        "auto-warmup-reconcile": {
            "task": "api.tasks.storage.reconcile_auto_warmup",
            "schedule": float(os.environ.get("CELERY_BEAT_AUTO_WARMUP_SECONDS", "120")),
            "options": {"queue": "storage"},
        },
        # Periodic stale-job reconciliation. Catches rows whose worker
        # died mid-flight, broker dropped the message, or external plane
        # silently advanced. See docstring on reconcile_stale_jobs for
        # the decision tree it follows.
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
        # Discover release tags on the configured `UPGRADE_GIT_REMOTE`.
        # Inert when the env is unset; bounded HTTP call otherwise.
        "upgrade-check-latest": {
            "task": "api.tasks.upgrade.check_latest",
            "schedule": 1800.0,
            "options": {"queue": "default"},
        },
        # Reconcile `rolling_out` to `succeeded`/`failed_rollout` on the
        # post-PATCH revision. Cheap when state != rolling_out.
        "upgrade-reconcile-rolling-out": {
            "task": "api.tasks.upgrade.reconcile_rolling_out",
            "schedule": float(os.environ.get("CELERY_BEAT_UPGRADE_RECONCILE_SECONDS", "180")),
            "options": {"queue": "default"},
        },
        # Retry orphan ACR tag deletes recorded by `_fail_pre` when the
        # MI didn't have `acrDelete` at the time of failure. Daily is
        # plenty — most operators add the role within hours of seeing
        # the audit row, and the retry is idempotent.
        "upgrade-purge-orphan-tags": {
            "task": "api.tasks.upgrade.purge_orphan_acr_tags",
            "schedule": 24 * 60 * 60.0,
            "options": {"queue": "default"},
        },
        # Compact the upgrade-history append blob weekly: drop events
        # older than the read-time age cap so the blob doesn't grow
        # unboundedly even if the deployment runs for years.
        "upgrade-compact-history": {
            "task": "api.tasks.upgrade.compact_history",
            "schedule": 7 * 24 * 60 * 60.0,
            "options": {"queue": "default"},
        },
    },
    timezone="UTC",
    enable_utc=True,
)


# ---------------------------------------------------------------------------
# Sidecar metrics reporter — fires from worker_init / beat_init signals so
# both Celery sidecars publish their cgroup CPU/MEM into Redis db 2 next to
# the api sidecar's snapshots. The /api/monitor/sidecars endpoint reads
# all six.
# ---------------------------------------------------------------------------
def _start_reporter(sender_name: str) -> None:
    if os.environ.get("SIDECAR_REPORTER_DISABLED", "").lower() == "true":
        return
    try:
        from api.services.cgroup_reporter import start_in_thread

        name = os.environ.get("SIDECAR_NAME", sender_name)
        start_in_thread(name)
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "cgroup reporter failed to start in %s", sender_name, exc_info=True
        )


from celery.signals import (  # noqa: E402 — keep near user
    beat_init,
    before_task_publish,
    worker_init,
)


@worker_init.connect  # type: ignore[untyped-decorator]
def _on_worker_init(**_kwargs: object) -> None:
    try:
        from api.app.telemetry import init_telemetry

        init_telemetry(role="worker")
    except Exception:
        import logging

        logging.getLogger(__name__).debug("worker telemetry init skipped", exc_info=True)
    _start_reporter("worker")


@beat_init.connect  # type: ignore[untyped-decorator]
def _on_beat_init(**_kwargs: object) -> None:
    try:
        from api.app.telemetry import init_telemetry

        init_telemetry(role="beat")
    except Exception:
        import logging

        logging.getLogger(__name__).debug("beat telemetry init skipped", exc_info=True)
    _start_reporter("beat")


# ---------------------------------------------------------------------------
# UI animation events — the SidecarsCard topology graph fires a particle
# along Row 2 (api → redis → worker) every time the api enqueues a task
# and along Row 3 (beat → redis) every time beat does. before_task_publish
# fires in the *producer* process, so the SIDECAR_NAME env decides which
# row gets the credit. Failures are swallowed inside event_emitter.emit.
# ---------------------------------------------------------------------------
_PRODUCER_ROLE = os.environ.get("SIDECAR_NAME", "api")


@before_task_publish.connect  # type: ignore[untyped-decorator]
def _on_before_task_publish(**_kwargs: object) -> None:
    from api.services.event_emitter import ROW_ASYNC, ROW_SCHED, emit

    emit(ROW_SCHED if _PRODUCER_ROLE == "beat" else ROW_ASYNC)

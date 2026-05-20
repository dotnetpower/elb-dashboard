"""Celery application factory for the worker and beat sidecars.

The worker and beat containers run the same image as the api sidecar but
override the entrypoint to launch `celery worker` / `celery beat` against
this `celery_app`. Tasks live under `api.tasks.*`.

Phase 1 ships the Celery infrastructure but no real task implementations.
Phases 2-3 will add the actual task handlers (BLAST submit / delete /
warmup, ACR builds, AKS provision, schedule reconciler, etc.) backed by the
Storage state repository.
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
        "api.tasks.storage",
        "api.tasks.openapi",
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

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Silence the Celery 6.0 deprecation warning by opting in explicitly.
    # We want broker connection retries on startup so the worker comes up
    # gracefully even if Redis takes a few seconds to become reachable.
    broker_connection_retry_on_startup=True,
    task_default_queue="default",
    task_routes={
        "api.tasks.azure.*": {"queue": "azure"},
        "api.tasks.acr.*": {"queue": "acr"},
        "api.tasks.blast.*": {"queue": "blast"},
        "api.tasks.storage.*": {"queue": "storage"},
        # OpenAPI deploys talk to AKS / MSI / Authorization, same as
        # `provision_aks` — share the azure queue.
        "api.tasks.openapi.*": {"queue": "azure"},
    },
    beat_schedule={
        "auto-warmup-reconcile": {
            "task": "api.tasks.storage.reconcile_auto_warmup",
            "schedule": 60.0,
            "options": {"queue": "storage"},
        },
        # Periodic stale-job reconciliation. Catches rows whose worker
        # died mid-flight, broker dropped the message, or external plane
        # silently advanced. See docstring on reconcile_stale_jobs for
        # the decision tree it follows.
        "blast-reconcile-stale-jobs": {
            "task": "api.tasks.blast.reconcile_stale_jobs",
            "schedule": 60.0,
            "options": {"queue": "blast"},
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


@worker_init.connect
def _on_worker_init(**_kwargs):
    _start_reporter("worker")


@beat_init.connect
def _on_beat_init(**_kwargs):
    _start_reporter("beat")


# ---------------------------------------------------------------------------
# UI animation events — the SidecarsCard topology graph fires a particle
# along Row 2 (api → redis → worker) every time the api enqueues a task
# and along Row 3 (beat → redis) every time beat does. before_task_publish
# fires in the *producer* process, so the SIDECAR_NAME env decides which
# row gets the credit. Failures are swallowed inside event_emitter.emit.
# ---------------------------------------------------------------------------
_PRODUCER_ROLE = os.environ.get("SIDECAR_NAME", "api")


@before_task_publish.connect
def _on_before_task_publish(**_kwargs):
    from api.services.event_emitter import ROW_ASYNC, ROW_SCHED, emit

    emit(ROW_SCHED if _PRODUCER_ROLE == "beat" else ROW_ASYNC)

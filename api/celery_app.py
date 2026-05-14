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
    include=[
        "api.tasks",
        "api.tasks.azure",
        "api.tasks.acr",
        "api.tasks.blast",
        "api.tasks.storage",
    ],
)

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
        "api.tasks.blast.*": {"queue": "blast"},
        "api.tasks.storage.*": {"queue": "storage"},
    },
    # Beat schedule lives in Storage state (loaded by the StorageScheduler in
    # phase 2). The default in-memory schedule is empty.
    beat_schedule={},
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
    except Exception:  # noqa: BLE001 — never crash the worker over telemetry
        import logging
        logging.getLogger(__name__).warning(
            "cgroup reporter failed to start in %s", sender_name, exc_info=True
        )


from celery.signals import beat_init, worker_init  # noqa: E402 — keep near user


@worker_init.connect
def _on_worker_init(**_kwargs):
    _start_reporter("worker")


@beat_init.connect
def _on_beat_init(**_kwargs):
    _start_reporter("beat")

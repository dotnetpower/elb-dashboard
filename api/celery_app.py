"""Celery application configuration for worker and beat sidecars.

Responsibility: Instantiate the single `celery_app` (broker, backend,
task discovery, queue routing, beat schedule) shared by the api / worker
/ beat sidecars. Import `api.celery_signals` for its registration
side-effect so handlers fire whenever this module is imported.
Edit boundaries: App-config wiring + queue routing + beat schedule only.
Signal handlers and JobState-recording helpers live in
`api.celery_signals`; legacy attribute names (`_on_task_failure`,
`_on_task_revoked`, `_on_worker_process_init`, `_start_reporter`, …)
are re-exported below so existing tests and monkeypatches keep working.
Key entry points: `celery_app`. Legacy re-exports: `_on_task_failure`,
`_on_task_internal_error`, `_on_task_revoked`, `_on_worker_init`,
`_on_worker_process_init`, `_on_beat_init`, `_on_before_task_publish`,
`_record_task_terminal_state`, `_start_reporter`, `_now_iso`,
`_task_job_id`, `_task_name`.
Risky contracts: Order matters — the `from api import celery_signals`
import at the bottom must run before any task fires, otherwise terminal
JobState rows are not recorded. `celery_app.set_default()` /
`set_current()` must run before any `shared_task.delay()` call from the
api sidecar resolves `current_app`.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import os

from celery import Celery

from api.services.blast.coordination import assert_coordination_invariants

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
        "api.tasks.servicebus",
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

# When BLAST_COORD_BACKEND=k8s, the submit Lease is released in a `finally` that
# only runs on a graceful exit; a hard Celery SIGKILL skips it and orphans the
# Lease until TTL. Assert the full ordering chain
# (submit_exec < soft < hard, submit_exec < lease_ttl) at worker startup so a
# misconfiguration fails fast instead of silently re-opening the concurrent-submit
# race. No-op unless the k8s backend is active (charter §12a Rule 4 default-OFF).
assert_coordination_invariants()

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
        "api.tasks.servicebus.*": {"queue": "reconcile"},
    },
    # The interactive task_routes above are deliberately left pointing at the
    # latency-critical queues (azure / blast / storage / default) so an
    # operator-triggered `.delay()` keeps its current routing. The periodic
    # beat-scheduled maintenance/reconcile tasks below are instead pinned to a
    # dedicated `reconcile` queue so a slow reconcile pass cannot queue behind
    # — or starve — an interactive BLAST submit. worker-main subscribes to
    # `reconcile` (see CELERY_MAIN_QUEUES in run_celery_workers.py), so the
    # isolation is logical today and lets a future deployment peel `reconcile`
    # onto a dedicated low-priority worker without code changes.
    beat_schedule={
        "auto-warmup-reconcile": {
            "task": "api.tasks.storage.reconcile_auto_warmup",
            "schedule": float(os.environ.get("CELERY_BEAT_AUTO_WARMUP_SECONDS", "120")),
            "options": {"queue": "reconcile"},
        },
        "prepare-db-orphan-reconcile": {
            "task": "api.tasks.storage.reconcile_orphaned_prepare_db",
            "schedule": float(
                os.environ.get("CELERY_BEAT_PREPARE_DB_ORPHAN_SECONDS", "300")
            ),
            "options": {"queue": "reconcile"},
        },
        # Terminalise warmup / prepare_db_* / shard / oracle jobstate rows stuck
        # active after a worker crash (or a legacy synchronous audit row). The
        # orphan reconciler above only fixes {db}-metadata.json; this one fixes
        # the Table rows so the job list / auto-stop no longer see phantom work.
        "stale-dbops-reconcile": {
            "task": "api.tasks.storage.reconcile_stale_dbops_jobs",
            "schedule": float(
                os.environ.get("CELERY_BEAT_STALE_DBOPS_SECONDS", "300")
            ),
            "options": {"queue": "reconcile"},
        },
        "blast-reconcile-stale-jobs": {
            "task": "api.tasks.blast.reconcile_stale_jobs",
            "schedule": float(os.environ.get("CELERY_BEAT_BLAST_RECONCILE_SECONDS", "90")),
            "options": {"queue": "reconcile"},
        },
        "blast-backfill-completed-runtime-metrics": {
            "task": "api.tasks.blast.backfill_completed_runtime_metrics",
            "schedule": 300.0,
            "options": {"queue": "reconcile"},
        },
        "upgrade-check-latest": {
            "task": "api.tasks.upgrade.check_latest",
            "schedule": 1800.0,
            "options": {"queue": "reconcile"},
        },
        "upgrade-reconcile-rolling-out": {
            "task": "api.tasks.upgrade.reconcile_rolling_out",
            "schedule": float(os.environ.get("CELERY_BEAT_UPGRADE_RECONCILE_SECONDS", "180")),
            "options": {"queue": "reconcile"},
        },
        "upgrade-purge-orphan-tags": {
            "task": "api.tasks.upgrade.purge_orphan_acr_tags",
            "schedule": 24 * 60 * 60.0,
            "options": {"queue": "reconcile"},
        },
        "upgrade-compact-history": {
            "task": "api.tasks.upgrade.compact_history",
            "schedule": 7 * 24 * 60 * 60.0,
            "options": {"queue": "reconcile"},
        },
        "aks-reconcile-stale-provisions": {
            "task": "api.tasks.azure.reconcile_stale_aks_provisions",
            "schedule": float(os.environ.get("CELERY_BEAT_AKS_PROVISION_RECONCILE_SECONDS", "300")),
            "options": {"queue": "reconcile"},
        },
        "aks-idle-autostop-evaluate": {
            "task": "api.tasks.azure.evaluate_idle_clusters",
            "schedule": float(os.environ.get("CELERY_BEAT_AKS_IDLE_AUTOSTOP_SECONDS", "300")),
            "options": {"queue": "reconcile"},
        },
        "openapi-public-https-reconcile": {
            "task": "api.tasks.openapi.reconcile_public_https",
            "schedule": float(os.environ.get("CELERY_BEAT_OPENAPI_PUBLIC_HTTPS_SECONDS", "120")),
            "options": {"queue": "reconcile"},
        },
        # Optional Service Bus BLAST integration. All three no-op unless
        # SERVICEBUS_ENABLED=true AND the saved config opts in, so leaving them
        # scheduled on every deployment is free (one cheap guard check per tick).
        "servicebus-drain-and-resubmit": {
            "task": "api.tasks.servicebus.drain_and_resubmit",
            "schedule": float(os.environ.get("CELERY_BEAT_SERVICEBUS_DRAIN_SECONDS", "30")),
            "options": {"queue": "reconcile"},
        },
        "servicebus-publish-transitions": {
            "task": "api.tasks.servicebus.publish_transitions",
            "schedule": float(os.environ.get("CELERY_BEAT_SERVICEBUS_PUBLISH_SECONDS", "30")),
            "options": {"queue": "reconcile"},
        },
        "servicebus-dlq-cleanup": {
            "task": "api.tasks.servicebus.dlq_cleanup",
            "schedule": float(os.environ.get("CELERY_BEAT_SERVICEBUS_DLQ_CLEANUP_SECONDS", "3600")),
            "options": {"queue": "reconcile"},
        },
    },
    timezone="UTC",
    enable_utc=True,
)


# Import the signal-handler module *after* `celery_app` exists so the
# @worker_init / @task_failure / @before_task_publish decorators register
# at import time. Tests and other modules also rely on the re-exports
# below to monkeypatch handlers by their legacy `api.celery_app.<name>`
# path.
from api import celery_signals as _signals  # noqa: E402

# Legacy re-exports — keep the public surface stable for tests
# (`test_celery_failure_visibility.py`, `test_telemetry_init.py`) and any
# external code that imports the old names directly.
_start_reporter = _signals._start_reporter
_now_iso = _signals._now_iso
_task_job_id = _signals._task_job_id
_task_name = _signals._task_name
_record_task_terminal_state = _signals._record_task_terminal_state
_on_worker_init = _signals._on_worker_init
_on_worker_process_init = _signals._on_worker_process_init
_on_beat_init = _signals._on_beat_init
_on_task_failure = _signals._on_task_failure
_on_task_internal_error = _signals._on_task_internal_error
_on_task_revoked = _signals._on_task_revoked
_on_before_task_publish = _signals._on_before_task_publish
_PRODUCER_ROLE = _signals._PRODUCER_ROLE

__all__ = [
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "celery_app",
]

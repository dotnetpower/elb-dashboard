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
from typing import Any

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
    "azure.monitor.opentelemetry.exporter.export._base",
    "azure.servicebus",
    "azure.servicebus._pyamqp",
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
        "api.tasks.webhooks",
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
# billiard's prefork master forks a child, then waits this many seconds for the
# child to send its "UP" readiness message before declaring it lost, SIGKILLing
# it, and forking a replacement. The default (~4s) is too tight for the worker
# sidecar's 0.5 vCPU budget: per-child boot work (Azure Monitor OpenTelemetry
# init in worker_process_init) can exceed 4s, so the master SIGKILL'd every
# child as "Timed out waiting for UP message" and respawned it — a permanent
# crash loop that killed in-flight BLAST tasks with WorkerLostError. Give child
# boot real headroom; raising the ceiling only delays detection of a genuinely
# stuck child, which does not happen in practice.
_WORKER_PROC_ALIVE_TIMEOUT = float(
    os.environ.get("CELERY_WORKER_PROC_ALIVE_TIMEOUT", "30.0")
)
if _WORKER_PROC_ALIVE_TIMEOUT <= 0:
    raise ValueError("CELERY_WORKER_PROC_ALIVE_TIMEOUT must be > 0")
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
    worker_proc_alive_timeout=_WORKER_PROC_ALIVE_TIMEOUT,
    task_soft_time_limit=_TASK_SOFT_TIME_LIMIT,
    task_time_limit=_TASK_TIME_LIMIT,
    result_expires=_RESULT_EXPIRES_SECONDS,
    broker_connection_retry_on_startup=True,
    # Bound the per-attempt TCP connect for the result backend writes (PROGRESS
    # state updates, task results). Default is the OS-level connect timeout
    # (75-120 s on Linux), which on a host where the broker port is *filtered*
    # (test environments without a Redis container, WSL2 mirrored networking,
    # a stopped sidecar that left the LB rule in place) makes every
    # `self.update_state(...)` block until the per-test 60 s `pytest-timeout`
    # alarm fires. The same fail-fast philosophy applies to production: a
    # genuinely-down result backend should surface in a few seconds, not
    # tarpit every progress checkpoint. 5 s is generous for a healthy
    # localhost Redis (sub-millisecond response) and matches the budget
    # `_gate_broker` already uses for the broker probe.
    result_backend_transport_options={
        "socket_connect_timeout": float(
            os.environ.get("CELERY_RESULT_BACKEND_CONNECT_TIMEOUT", "5")
        )
    },
    # Belt-and-braces for the redis result backend: the keyword above is
    # consumed by kombu-style backends, but Celery's redis backend reads
    # these top-level keys directly (see celery.backends.redis). Without
    # them every `self.update_state(state="PROGRESS", ...)` would block on
    # the OS socket timeout when the result backend is unreachable.
    # Both bounds are env-tunable so ops can relax them without a redeploy
    # if a deployment ever puts the worker on a high-latency link.
    redis_socket_connect_timeout=float(
        os.environ.get("CELERY_REDIS_SOCKET_CONNECT_TIMEOUT", "5")
    ),
    redis_socket_timeout=float(os.environ.get("CELERY_REDIS_SOCKET_TIMEOUT", "30")),
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
    # — or starve — an interactive BLAST submit. worker-reconcile exclusively
    # consumes it (see run_celery_workers.py); worker-main never subscribes.
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
        # Self-heal DB volume/shard drift: prune ghost volumes left when NCBI
        # shrinks a DB + regenerate the shard layout for the true volume set, so
        # a DB can never rot into the 3-way generation mismatch that fails BLAST
        # with "vol does not match lmdb vol". The task is a no-op unless
        # DB_CONSISTENCY_RECONCILE_ENABLED is set (it DELETES Storage blobs), so
        # scheduling it while dormant is harmless (charter §12a Rule 4).
        "db-consistency-reconcile": {
            "task": "api.tasks.storage.reconcile_db_consistency",
            "schedule": float(
                os.environ.get("CELERY_BEAT_DB_CONSISTENCY_SECONDS", "1800")
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
        # Auto-resubmit transient-failed BLAST jobs. The task itself is a no-op
        # unless BLAST_AUTO_RETRY_ENABLED is set, so scheduling it is harmless
        # while the feature is dormant (charter section 12a Rule 4, default-OFF).
        "blast-auto-retry-failed-jobs": {
            "task": "api.tasks.blast.auto_retry_failed_jobs",
            "schedule": float(os.environ.get("CELERY_BEAT_BLAST_AUTO_RETRY_SECONDS", "180")),
            "options": {"queue": "reconcile"},
        },
        # POST terminal-job notifications to a configured webhook. No-op unless
        # WEBHOOK_NOTIFICATIONS_ENABLED is set AND a webhook is configured
        # (charter section 12a Rule 4, default-OFF).
        "dispatch-job-webhooks": {
            "task": "api.tasks.webhooks.dispatch_job_webhooks",
            "schedule": float(os.environ.get("CELERY_BEAT_WEBHOOK_SECONDS", "60")),
            "options": {"queue": "reconcile"},
        },
        "blast-backfill-completed-runtime-metrics": {
            "task": "api.tasks.blast.backfill_completed_runtime_metrics",
            "schedule": 300.0,
            "options": {"queue": "reconcile"},
        },
        # Heal the jobstate time-ordered index (#50): re-run the idempotent
        # backfill upserts so a job whose in-line best-effort _index_put failed
        # is re-added and stops being omitted from the indexed /api/blast/jobs
        # listing. No-op unless JOBSTATE_TIME_INDEX_ENABLED is set (the task
        # returns early before touching Storage), so leaving it scheduled on
        # every deployment is free — one cheap env check per tick.
        "blast-reconcile-time-index": {
            "task": "api.tasks.blast.reconcile_time_index",
            "schedule": float(
                os.environ.get("CELERY_BEAT_TIME_INDEX_RECONCILE_SECONDS", "3600")
            ),
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
        # Keep the IP-based OpenAPI runtime endpoint durable cache fresh so the
        # Service Bus drain readiness gate resolves it after a revision restart
        # without a manual ELB_OPENAPI_BASE_URL pin. No-op unless SERVICEBUS is
        # enabled AND a cluster context is resolvable (one cheap guard per tick).
        "openapi-runtime-endpoint-reconcile": {
            "task": "api.tasks.openapi.reconcile_runtime_endpoint",
            "schedule": float(
                os.environ.get("CELERY_BEAT_OPENAPI_RUNTIME_ENDPOINT_SECONDS", "300")
            ),
            "options": {"queue": "reconcile"},
        },
        # Optional Service Bus BLAST integration. All three no-op unless
        # SERVICEBUS_ENABLED=true AND the saved config opts in, so leaving them
        # scheduled on every deployment is free (one cheap guard check per tick).
        "servicebus-drain-and-resubmit": {
            "task": "api.tasks.servicebus.drain_and_resubmit",
            "schedule": float(os.environ.get("CELERY_BEAT_SERVICEBUS_DRAIN_SECONDS", "10")),
            # A tick older than 30 s is obsolete: the next periodic tick will
            # inspect the same queue/bridge state. Expiry sheds stale backlog
            # after a slow upstream call instead of replaying every missed tick.
            "options": {
                "queue": "reconcile",
                "expires": float(
                    os.environ.get("CELERY_BEAT_SERVICEBUS_EXPIRES_SECONDS", "30")
                ),
            },
        },
        "servicebus-publish-transitions": {
            "task": "api.tasks.servicebus.publish_transitions",
            "schedule": float(os.environ.get("CELERY_BEAT_SERVICEBUS_PUBLISH_SECONDS", "10")),
            "options": {
                "queue": "reconcile",
                "expires": float(
                    os.environ.get("CELERY_BEAT_SERVICEBUS_EXPIRES_SECONDS", "30")
                ),
            },
        },
        "servicebus-dlq-cleanup": {
            "task": "api.tasks.servicebus.dlq_cleanup",
            "schedule": float(os.environ.get("CELERY_BEAT_SERVICEBUS_DLQ_CLEANUP_SECONDS", "3600")),
            "options": {"queue": "reconcile"},
        },
        # Age-based result retention. No-op every tick unless STORAGE_DFS_ENABLED
        # is on AND BLAST_RESULT_RETENTION_DAYS > 0 (default 0 = disabled), so
        # leaving it scheduled is one cheap guard check per day.
        "blast-retention-purge": {
            "task": "api.tasks.storage.purge_aged_results",
            "schedule": float(os.environ.get("CELERY_BEAT_RETENTION_SECONDS", str(24 * 60 * 60))),
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


def fast_probe_connection(socket_connect_timeout: float = 2.0) -> Any:
    """Return a kombu broker connection with a bounded TCP connect timeout.

    The default kombu/redis transport inherits the OS-level connect timeout
    (75-120 s on Linux) for each connect attempt. ``ensure_connection(timeout=N)``
    only bounds the overall *retry loop*, not the individual socket connect, so
    on a host where the broker port is *filtered* rather than *refused* (WSL2
    mirrored networking, a stopped sidecar that left the LB rule in place,
    pytest with no Redis container) a single probe blocks past any caller
    deadline. Readiness, pre-flight, and the BLAST submit gate all want a
    crisp pass/fail in a few seconds — this helper bolts that contract on.

    Use only for **probes**. Production workers/producers must keep the
    long, retrying connect that lets the worker ride out a broker restart.
    """
    conn = celery_app.connection()
    # Use getattr so test doubles that omit `transport_options` (the kombu
    # `Connection` attribute) still work; the dict spread accepts the empty
    # fallback unchanged.
    existing = getattr(conn, "transport_options", None) or {}
    try:
        conn.transport_options = {**existing, "socket_connect_timeout": socket_connect_timeout}
    except AttributeError:
        # Test double exposes only the methods the probe needs; the timeout
        # cap is best-effort here — the real broker connection always honours it.
        pass
    return conn


__all__ = [
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "celery_app",
    "fast_probe_connection",
]

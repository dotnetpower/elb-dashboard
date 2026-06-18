"""BLAST ``submit`` Celery task — preparing → warming → splitting → configuring → submitting.

Responsibility: Drive the end-to-end ``elastic-blast submit`` pipeline as a single Celery
task. Coordinate the warmup / oracle-upload fan-out, decide on storage-query split parent
submission, build + persist the configuration, acquire the cluster submit lock, stream the
submit command via the terminal sidecar, and finalise the job state with a phase/status
that's gated on visible result artifacts.
Edit boundaries: Every call to a sibling helper or constant (``_progress``, ``_update_state``,
``_snippet``, ``_stream_submit_command``, ``_ensure_terminal_azure_cli_login``, etc.) goes
through ``_blast.X`` so test ``monkeypatch.setattr(blast, …)`` calls propagate. Decorator
and signature MUST stay byte-identical to the previous
``@shared_task(name="api.tasks.blast.submit", …)`` contract — Celery clients dispatch by
that exact name.
Key entry points: ``submit`` (registered as ``api.tasks.blast.submit``).
Risky contracts: The task is idempotent in the sense that retries re-stage everything from
scratch, but it relies on ``acquire_submit_lock`` for cluster-level mutual exclusion
(``TTL=BLAST_SUBMIT_LOCK_TTL_SECONDS``). The 30s retry on lock contention is observable in
worker logs; widening that changes the failure-recovery latency the dashboard surfaces.
Storage uploads for the config preview are best-effort: failures degrade to a
``config_upload_error`` payload field but do not abort submission.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

from celery import shared_task

from api.services.blast.coordination import (
    SUBMIT_COORDINATION_NAMESPACE,
    capacity_wait_max_seconds,
    coordination_backend,
    submit_slot_wait_max_seconds,
)
from api.services.blast.db_metadata import extract_db_name
from api.services.blast.oracles import (
    upload_db_order_oracle_pointer_if_available,
    upload_tie_order_oracle_if_present,
)
from api.services.blast.task_config import WarmupNotReadyError
from api.services.terminal_exec import TerminalExecError
from api.tasks import blast as _blast
from api.tasks.blast.cli_parsing import ELASTIC_BLAST_CFG_FILE
from api.tasks.blast.poll_tasks import (
    _POLL_RUNNING_ELIGIBLE_PHASES,
    POLL_RUNNING_START_DELAY,
    poll_running_status,
)
from api.tasks.blast.submit_lock import (
    acquire_submit_lock,
    release_submit_lock,
    submit_lock_key,
)
from api.tasks.blast.submit_logs import persist_submit_log_events

# Single source for the k8s-gate deny requeue cadence. ``retry_after_seconds``
# (the informational state-row field) carries the nominal base; the actual
# Celery ``countdown`` adds bounded jitter so a herd of submitters denied at the
# same instant (Lease busy / ceiling full) do not re-poll the apiserver in
# lock-step (critique H6/L26).
_GATE_REQUEUE_BASE_COUNTDOWN_SECONDS = 30
_GATE_REQUEUE_JITTER_SECONDS = 10


def _gate_requeue_countdown() -> float:
    """Base requeue countdown plus +0..jitter seconds of de-sync jitter."""
    return _GATE_REQUEUE_BASE_COUNTDOWN_SECONDS + random.uniform(  # noqa: S311 - jitter, not crypto
        0, _GATE_REQUEUE_JITTER_SECONDS
    )


def _capacity_gate_enabled() -> bool:
    """Stage 3 feature flag — default OFF preserves the existing submit lock path.

    Per Charter §12a Rule 4, a new admission control must ship behaviour-
    equivalent to today (existing per-cluster Redis lock + ``max_slots=1``).
    Flipping ``BLAST_GATE_ENABLED=true`` swaps to the capacity-gate path.
    """
    raw = os.environ.get("BLAST_GATE_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}

LOGGER = logging.getLogger(__name__)


def _warmup_max_wait_seconds() -> int:
    """Upper bound on how long a submit may wait for node-local warmup.

    The ``waiting_for_warmup`` re-enqueue loop is otherwise unbounded — a
    permanently-stuck warmup (e.g. a node that never leaves ``Loading`` or a
    DB generation marker that never lands) would keep re-enqueuing forever and
    never surface as ``Failed``. This deadline lets the job fail after a
    generous wait so the dashboard shows a real terminal state. Override with
    ``BLAST_WARMUP_MAX_WAIT_SECONDS``; default 45 minutes.
    """
    raw = os.environ.get("BLAST_WARMUP_MAX_WAIT_SECONDS", "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2700
    return value if value > 0 else 2700


def _database_max_wait_seconds() -> int:
    """Upper bound on how long a submit may wait for the BLAST DB to finish
    copying / updating.

    The ``waiting_for_database`` re-enqueue loop (transient
    ``database_not_ready`` / ``database_updating`` states) is otherwise
    unbounded — a prepare-db copy that never reports ``completed`` would keep
    re-enqueuing forever. This deadline lets the job fail after a generous wait
    so the dashboard shows a real terminal state instead of an endless queue.
    Override with ``BLAST_DATABASE_MAX_WAIT_SECONDS``; default 45 minutes
    (matches the warmup deadline — a large DB download can take a while).
    """
    raw = os.environ.get("BLAST_DATABASE_MAX_WAIT_SECONDS", "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2700
    return value if value > 0 else 2700


# Transient ``BlastDatabaseAvailabilityError`` codes — the DB exists but the
# prepare-db pipeline is still writing it (download in progress) or a version
# update is mid-flight. These are wait-and-retry states, mirroring the
# ``waiting_for_warmup`` loop: re-enqueue the submit until the copy/update
# settles rather than failing a job that would succeed minutes later. Every
# other code (missing DB, invalid reference, persistent Storage error) is
# permanent and fails fast.
_DATABASE_TRANSIENT_CODES = frozenset({"database_not_ready", "database_updating"})



@shared_task(
    name="api.tasks.blast.submit",
    bind=True,
    max_retries=12,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def submit(
    self: Any,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
    program: str,
    database: str,
    query_file: str,
    options: dict[str, Any] | None = None,
    caller_oid: str = "",
    caller_tenant_id: str = "",
    warmup_wait_deadline_ts: float | None = None,
    submit_slot_wait_deadline_ts: float | None = None,
    capacity_wait_deadline_ts: float | None = None,
    database_wait_deadline_ts: float | None = None,
) -> dict[str, Any]:
    """Submit a BLAST search via the terminal sidecar.

    Side effects: writes ``elastic-blast.ini`` in the terminal sidecar workdir,
    executes ``elastic-blast submit --cfg elastic-blast.ini``, and updates
    Table-backed job state.

    ``warmup_wait_deadline_ts`` is an internal re-enqueue knob: the first
    ``waiting_for_warmup`` re-enqueue stamps ``now + _warmup_max_wait_seconds()``
    and every subsequent re-enqueue forwards it unchanged, so the warmup wait
    loop fails once the deadline passes instead of looping forever. External
    callers never pass it.

    ``submit_slot_wait_deadline_ts`` / ``capacity_wait_deadline_ts`` are the
    matching internal knobs for the ``BLAST_COORD_BACKEND=k8s`` admission gate:
    a Lease-contended (Gate A) or ceiling-full (Gate B) submit re-enqueues with
    a deadline so neither wait loops forever. External callers never pass them.

    ``database_wait_deadline_ts`` is the matching internal knob for the
    ``waiting_for_database`` loop: a transient DB state (copy still in
    progress / version update mid-flight) re-enqueues the submit with a
    deadline so it waits for the prepare-db pipeline to finish instead of
    failing a job that would run minutes later. External callers never pass it.
    """

    _blast._progress(self, "preparing")
    _blast._update_state(job_id, "preparing")
    effective_options = _blast._suppress_sharding_for_unsharded_database(
        storage_account=storage_account,
        database=database,
        options=options,
    )
    effective_options = _blast._expand_strict_tie_order_candidate_pool(effective_options)
    db_name_for_warmup = extract_db_name(database)
    try:
        _blast._validate_blast_database_ready(
            storage_account=storage_account,
            database=database,
        )
    except _blast.BlastDatabaseAvailabilityError as exc:
        # A transient DB state (the prepare-db copy is still running, or a
        # version update is mid-flight) is wait-and-retry, NOT a failure: the
        # DB exists and will be ready soon. Re-enqueue the submit on the
        # ``waiting_for_database`` phase — the same proven pattern the
        # ``waiting_for_warmup`` loop below uses — instead of failing a job
        # that would run once the copy settles. This closes the gap where a
        # submit accepted before the DB finished warming (notably the OpenAPI
        # path, which has no submit-time readiness gate) would BLAST against
        # incomplete volumes or fail outright. The waiting row keeps
        # ``status="running"`` so the reconciler treats it as active (a
        # ``"queued"`` result would be reconciled to ``completed``). The loop
        # is bounded by ``database_wait_deadline_ts`` so a copy that never
        # completes eventually fails instead of looping forever. Permanent
        # codes (missing DB, invalid reference, persistent Storage error) fall
        # through to the fail-fast path.
        if exc.code in _DATABASE_TRANSIENT_CODES:
            now = time.time()
            deadline = database_wait_deadline_ts or (now + _database_max_wait_seconds())
            if now < deadline:
                _blast._progress(self, "waiting_for_database", database=db_name_for_warmup)
                _blast._update_state(
                    job_id,
                    "waiting_for_database",
                    status="running",
                    event="database_not_ready",
                    error_code=exc.code,
                    retry_after_seconds=30,
                    last_output=_blast._snippet(exc),
                )
                try:
                    submit.apply_async(
                        kwargs={
                            "job_id": job_id,
                            "subscription_id": subscription_id,
                            "resource_group": resource_group,
                            "cluster_name": cluster_name,
                            "storage_account": storage_account,
                            "program": program,
                            "database": database,
                            "query_file": query_file,
                            "options": options,
                            "caller_oid": caller_oid,
                            "caller_tenant_id": caller_tenant_id,
                            "database_wait_deadline_ts": deadline,
                        },
                        countdown=30,
                        queue="blast",
                    )
                except Exception as enq_exc:
                    return _blast._retry_or_fail(
                        self,
                        job_id=job_id,
                        phase="waiting_for_database",
                        exc=enq_exc,
                        error_code="blast_submit_requeue_failed",
                    )
                return {
                    "job_id": job_id,
                    "status": "running",
                    "phase": "waiting_for_database",
                    "requeued": True,
                    "error_code": exc.code,
                }
            LOGGER.warning(
                "blast_database_wait_deadline_exceeded job_id=%s cluster=%s database=%s",
                job_id,
                cluster_name,
                database,
            )
        error = _blast._snippet(exc)
        _blast._progress(self, "database_unavailable", database=db_name_for_warmup)
        _blast._update_state(
            job_id,
            "database_unavailable",
            status="failed",
            error_code=exc.code,
            output=error,
            last_output=error,
        )
        return {
            "job_id": job_id,
            "status": "failed",
            "phase": "database_unavailable",
            "error": error,
            "error_code": exc.code,
        }

    from concurrent.futures import Future, ThreadPoolExecutor

    from api.services.terminal_exec import run as terminal_run

    will_split_parent = _blast._requires_split_parent_submission(effective_options)

    _blast._progress(self, "warming_up", database=db_name_for_warmup)
    _blast._update_state(job_id, "warming_up", database=db_name_for_warmup)

    # Run the ~8s K8s warmup poll alongside the small Azure-side prep work
    # (Azure CLI login warmup + best-effort oracle blob uploads). The warmup
    # result is required to finalise effective_options, but the prep tasks
    # are independent — fan them out so warming_up wall time is the cap.
    warmup_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="blast-submit-prep")
    warmup_ready: dict[str, Any] | None = None
    tie_order_oracle: dict[str, Any] | None = None
    db_order_oracle: dict[str, Any] | None = None
    try:
        warmup_future = warmup_pool.submit(
            _blast._ensure_node_warmup_ready_for_submit,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            database=database,
            storage_account=storage_account,
            options=effective_options,
        )
        az_login_future = warmup_pool.submit(
            _blast._ensure_terminal_azure_cli_login, terminal_run
        )
        tie_oracle_future: Future[Any] | None = None
        db_oracle_future: Future[Any] | None = None
        if not will_split_parent:
            tie_oracle_future = warmup_pool.submit(
                upload_tie_order_oracle_if_present,
                storage_account=storage_account,
                job_id=job_id,
                options=effective_options,
            )
            db_oracle_future = warmup_pool.submit(
                upload_db_order_oracle_pointer_if_available,
                storage_account=storage_account,
                job_id=job_id,
                database=database,
                options=effective_options,
            )

        try:
            warmup_ready = warmup_future.result()
        except WarmupNotReadyError as exc:
            # A retryable warmup state (Loading/Pending/Starting, no DB
            # generation marker yet, or the warmup status read failed) is
            # transient: the node-local warmup is still settling. Re-enqueue
            # the submit instead of failing it — the SAME pattern the
            # capacity-gate / submit-lock waits below use. Re-enqueueing (not
            # ``task.retry``) means the wait does not consume the task's
            # ``max_retries`` budget and has no ~6-minute ceiling, so a search
            # against a still-warming sharded DB keeps its place in line until
            # the shards report warm. The waiting row keeps ``status="running"``
            # on the ``waiting_for_warmup`` phase — exactly like the capacity
            # gate's ``waiting_for_capacity`` row — because the reconciler's
            # ``_celery_success_row_status`` only treats ``"running"`` as still
            # active; the original task returns SUCCESS once it re-enqueues, so a
            # ``"queued"`` result would be reconciled to ``completed`` and the
            # artifact finalizer would fire on a job that has not run yet. A
            # non-retryable error (e.g. a missing database name) fails
            # immediately. The re-enqueue loop is bounded by a generous
            # ``warmup_wait_deadline_ts`` carried in the message so a
            # permanently-stuck warmup eventually fails instead of looping
            # forever (see ``_warmup_max_wait_seconds``).
            if getattr(exc, "retryable", False):
                now = time.time()
                deadline = warmup_wait_deadline_ts or (now + _warmup_max_wait_seconds())
                if now >= deadline:
                    error = _blast._snippet(exc)
                    LOGGER.warning(
                        "blast_warmup_wait_deadline_exceeded job_id=%s cluster=%s "
                        "database=%s",
                        job_id,
                        cluster_name,
                        database,
                    )
                    _blast._update_state(
                        job_id,
                        "warmup_not_ready",
                        status="failed",
                        event="warmup_wait_deadline_exceeded",
                        error_code="node_warmup_wait_deadline_exceeded",
                        output=error,
                        last_output=error,
                    )
                    return {
                        "job_id": job_id,
                        "status": "failed",
                        "phase": "warmup_not_ready",
                        "error": error,
                        "error_code": "node_warmup_wait_deadline_exceeded",
                    }
                _blast._update_state(
                    job_id,
                    "waiting_for_warmup",
                    status="running",
                    event="warmup_not_ready",
                    error_code="node_warmup_not_ready",
                    retry_after_seconds=30,
                    last_output=_blast._snippet(exc),
                )
                try:
                    submit.apply_async(
                        kwargs={
                            "job_id": job_id,
                            "subscription_id": subscription_id,
                            "resource_group": resource_group,
                            "cluster_name": cluster_name,
                            "storage_account": storage_account,
                            "program": program,
                            "database": database,
                            "query_file": query_file,
                            "options": options,
                            "caller_oid": caller_oid,
                            "caller_tenant_id": caller_tenant_id,
                            "warmup_wait_deadline_ts": deadline,
                        },
                        countdown=30,
                        queue="blast",
                    )
                except Exception as enq_exc:
                    # If re-enqueue itself fails (broker gone), fall back to
                    # the bounded retry path so the broker error surfaces.
                    return _blast._retry_or_fail(
                        self,
                        job_id=job_id,
                        phase="waiting_for_warmup",
                        exc=enq_exc,
                        error_code="blast_submit_requeue_failed",
                    )
                return {
                    "job_id": job_id,
                    "status": "running",
                    "phase": "waiting_for_warmup",
                    "requeued": True,
                }
            error = _blast._snippet(exc)
            _blast._update_state(
                job_id,
                "warmup_not_ready",
                status="failed",
                error_code="node_warmup_not_ready",
                output=error,
                last_output=error,
            )
            return {
                "job_id": job_id,
                "status": "failed",
                "phase": "warmup_not_ready",
                "error": error,
                "error_code": "node_warmup_not_ready",
            }

        if warmup_ready is not None:
            effective_options = dict(effective_options or {})
            effective_options["skip_warmed_ssd_init"] = True
            _blast._progress(
                self,
                "warmup_ready",
                database=db_name_for_warmup,
                warmup=warmup_ready,
            )
            _blast._update_state(
                job_id,
                "warmup_ready",
                status="running",
                warmup=warmup_ready,
            )

        try:
            az_login_future.result()
        except _blast.TerminalAzureLoginError as exc:
            return _blast._retry_or_fail(
                self,
                job_id=job_id,
                phase="terminal_az_login_failed",
                exc=exc,
                error_code="terminal_az_login_failed",
            )
        except TerminalExecError as exc:
            return _blast._retry_or_fail(
                self,
                job_id=job_id,
                phase="terminal_unavailable",
                exc=exc,
                error_code="terminal_exec_unavailable",
            )

        if tie_oracle_future is not None:
            try:
                tie_order_oracle = tie_oracle_future.result()
            except Exception as exc:
                LOGGER.warning(
                    "tie_order_oracle upload failed job_id=%s: %s",
                    job_id,
                    type(exc).__name__,
                )
                tie_order_oracle = None
        if db_oracle_future is not None:
            try:
                db_order_oracle = db_oracle_future.result()
            except Exception as exc:
                LOGGER.warning(
                    "db_order_oracle upload failed job_id=%s: %s",
                    job_id,
                    type(exc).__name__,
                )
                db_order_oracle = None
    finally:
        warmup_pool.shutdown(wait=False, cancel_futures=False)

    if will_split_parent:
        _blast._progress(self, "splitting_queries")
        try:
            return _blast._run_storage_query_split_parent_submission(
                parent_job_id=job_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
                storage_account=storage_account,
                program=program,
                database=database,
                query_file=query_file,
                query_effective_search_spaces=(effective_options or {}).get(
                    "query_effective_search_spaces"
                ),
                options=effective_options,
                owner_oid=caller_oid,
                tenant_id=caller_tenant_id,
                subscription_id=subscription_id,
            )
        except ValueError as exc:
            error = _blast._snippet(exc)
            _blast._update_state(
                job_id, "split_submit_invalid", status="failed", error_code=error
            )
            return {
                "job_id": job_id,
                "status": "failed",
                "phase": "split_submit_invalid",
                "error": error,
            }
        except Exception as exc:
            return _blast._retry_or_fail(
                self,
                job_id=job_id,
                phase="split_submit_unavailable",
                exc=exc,
                error_code="split_submit_unavailable",
            )

    if tie_order_oracle is not None:
        _blast._progress(self, "tie_order_oracle_uploaded", tie_order_oracle=tie_order_oracle)
        _blast._update_state(
            job_id,
            "tie_order_oracle_uploaded",
            status="running",
            tie_order_oracle=tie_order_oracle,
        )
    if db_order_oracle is not None:
        _blast._progress(self, "db_order_oracle_attached", db_order_oracle=db_order_oracle)
        _blast._update_state(
            job_id,
            "db_order_oracle_attached",
            status="running",
            db_order_oracle=db_order_oracle,
        )

    try:
        config_content = _blast._build_config_content(
            job_id=job_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            storage_account=storage_account,
            program=program,
            database=database,
            query_file=query_file,
            options=effective_options,
        )
        config_blob_path = f"{job_id}/{ELASTIC_BLAST_CFG_FILE}"
        try:
            from api.services import get_credential
            from api.services.storage.data import upload_blob_text

            config_url = upload_blob_text(
                get_credential(),
                storage_account,
                "queries",
                config_blob_path,
                config_content,
            )
            _blast._progress(
                self,
                "configuring",
                config_blob_path=f"queries/{config_blob_path}",
                config_url=config_url,
            )
            _blast._update_state(
                job_id,
                "configuring",
                status="running",
                config_blob_path=f"queries/{config_blob_path}",
                config_url=config_url,
            )
        except Exception as exc:
            LOGGER.warning(
                "config preview upload failed job_id=%s: %s", job_id, type(exc).__name__
            )
            _blast._update_state(
                job_id,
                "configuring",
                status="running",
                config_blob_path=f"queries/{config_blob_path}",
                config_upload_error=type(exc).__name__,
            )
    except Exception as exc:  # configuration errors are caller/actionable, not retryable
        error = _blast._snippet(exc)
        _blast._update_state(job_id, "config_invalid", status="failed", error_code=error)
        return {"job_id": job_id, "status": "failed", "phase": "config_invalid", "error": error}

    requires_node_warmup = _blast._submit_requires_node_warmup(effective_options)
    reuses_warmed_ssd = requires_node_warmup and bool(
        (effective_options or {}).get("skip_warmed_ssd_init")
    )
    if requires_node_warmup:
        if reuses_warmed_ssd:
            _blast._progress(
                self,
                "staging_db",
                skipped=True,
                decision="warmed_ssd_reused",
            )
            _blast._update_state(
                job_id,
                "staging_db",
                status="completed",
                skipped=True,
                decision="warmed_ssd_reused",
                skip_reason="node_local_ssd_warmup_ready",
                output="Node-local DB warmup is ready; ElasticBLAST SSD initialization is skipped.",
            )
            _blast._progress(self, "submitting")
            _blast._update_state(job_id, "submitting")
        else:
            _blast._progress(self, "staging_db")
            _blast._update_state(job_id, "staging_db")
    else:
        _blast._progress(self, "submitting")
        _blast._update_state(job_id, "submitting")

    try:
        gate_enabled = _capacity_gate_enabled()
        submit_lock: tuple[Any, str] | None = None
        capacity_reservation = None
        k8s_lease = None
        k8s_gate_credential: Any | None = None
        # §2a precedence: BLAST_COORD_BACKEND=k8s wins over BLAST_GATE_ENABLED.
        # In k8s mode the cluster-backed Lease (Gate A) + job-count ceiling
        # (Gate B) are authoritative; the Redis capacity gate / submit lock are
        # bypassed entirely (reserve_slot is never called).
        backend = coordination_backend()
        if backend == "k8s":
            from api.services import get_credential
            from api.services.blast import k8s_gate

            credential = get_credential()
            # Captured so the ``finally`` release reuses the SAME credential
            # instead of re-calling get_credential() (which, if it raised in
            # finally, would mask the original submit exception — round-3 M-D).
            k8s_gate_credential = credential
            admission = k8s_gate.acquire_k8s_admission(
                credential,
                subscription_id,
                resource_group,
                cluster_name,
                namespace=SUBMIT_COORDINATION_NAMESPACE,
                job_id=job_id,
                source="dashboard",
            )
            if not admission.admitted:
                if admission.error:
                    # A genuine apiserver failure — surface via the bounded
                    # retry path, NOT a silent forever-requeue.
                    return _blast._retry_or_fail(
                        self,
                        job_id=job_id,
                        phase="submit_coordination_unavailable",
                        exc=RuntimeError(admission.reason or "lease_api_error"),
                        error_code="blast_submit_lease_api_error",
                    )
                now = time.time()
                if admission.reason == k8s_gate.REASON_SUBMIT_SLOT_BUSY:
                    phase_name = "waiting_for_submit_slot"
                    deadline = submit_slot_wait_deadline_ts or (
                        now + submit_slot_wait_max_seconds()
                    )
                    error_code = "blast_submit_slot_busy"
                    deadline_code = "blast_submit_slot_wait_deadline_exceeded"
                    # Carry BOTH deadlines forward as explicit locals so an
                    # oscillation between submit_slot_busy and capacity_full
                    # cannot reset either bound. This branch refreshes the
                    # submit-slot deadline; the capacity deadline passes through
                    # untouched (critique M11 — no dict-key-overwrite reliance).
                    next_submit_slot_deadline = deadline
                    next_capacity_deadline = capacity_wait_deadline_ts
                else:
                    # capacity_full or capacity_count_error (fail-closed)
                    phase_name = "waiting_for_capacity"
                    deadline = capacity_wait_deadline_ts or (
                        now + capacity_wait_max_seconds()
                    )
                    error_code = f"blast_{admission.reason}"
                    deadline_code = "blast_capacity_wait_deadline_exceeded"
                    next_submit_slot_deadline = submit_slot_wait_deadline_ts
                    next_capacity_deadline = deadline
                if now >= deadline:
                    LOGGER.warning(
                        "blast_k8s_gate_wait_deadline_exceeded job_id=%s "
                        "cluster=%s phase=%s reason=%s",
                        job_id,
                        cluster_name,
                        phase_name,
                        admission.reason,
                    )
                    _blast._update_state(
                        job_id,
                        phase_name,
                        status="failed",
                        event="submit_wait_deadline_exceeded",
                        error_code=deadline_code,
                    )
                    return {
                        "job_id": job_id,
                        "status": "failed",
                        "phase": phase_name,
                        "error_code": deadline_code,
                    }
                # Compute the jittered requeue delay ONCE so the informational
                # ``retry_after_seconds`` written to the state row matches the
                # actual Celery ``countdown`` the task is re-enqueued with
                # (round-3 L-A: previously the row advertised the bare base 30s
                # while the task slept 30-40s).
                requeue_countdown = _gate_requeue_countdown()
                _blast._update_state(
                    job_id,
                    phase_name,
                    status="running",
                    event="k8s_gate_deny",
                    error_code=error_code,
                    retry_after_seconds=requeue_countdown,
                )
                try:
                    submit.apply_async(
                        kwargs={
                            "job_id": job_id,
                            "subscription_id": subscription_id,
                            "resource_group": resource_group,
                            "cluster_name": cluster_name,
                            "storage_account": storage_account,
                            "program": program,
                            "database": database,
                            "query_file": query_file,
                            "options": options,
                            "caller_oid": caller_oid,
                            "caller_tenant_id": caller_tenant_id,
                            # Both wait deadlines carried forward via explicit
                            # locals computed above (critique M11): the active
                            # branch's bound is refreshed, the other is preserved.
                            "submit_slot_wait_deadline_ts": next_submit_slot_deadline,
                            "capacity_wait_deadline_ts": next_capacity_deadline,
                        },
                        countdown=requeue_countdown,
                        queue="blast",
                    )
                except Exception as enq_exc:
                    return _blast._retry_or_fail(
                        self,
                        job_id=job_id,
                        phase=phase_name,
                        exc=enq_exc,
                        error_code="blast_submit_requeue_failed",
                    )
                return {
                    "job_id": job_id,
                    "status": "running",
                    "phase": phase_name,
                    "requeued": True,
                }
            k8s_lease = admission.lease
        elif gate_enabled:
            from api.services import get_credential
            from api.services.blast import capacity_gate, capacity_signals

            credential = get_credential()
            try:
                snap = capacity_signals.resolve_capacity_signals(
                    credential, subscription_id, resource_group, cluster_name
                )
            except Exception as exc:  # pragma: no cover - safety net
                LOGGER.warning(
                    "capacity gate signal resolve failed job_id=%s: %s",
                    job_id,
                    type(exc).__name__,
                )
                snap = capacity_signals.CapacitySignals(
                    pressure=None, top_nodes=None, pending_pods=0
                )
            active = capacity_gate.list_active_reservations(cluster_name)
            demand = capacity_gate.predict_demand(program=program, database=database)
            decision = capacity_gate.evaluate_capacity_gate(
                pressure=snap.pressure,
                top_nodes=snap.top_nodes,
                pending_pods_count=snap.pending_pods,
                predicted_demand=demand,
                active_reservations=active,
            )
            if not decision.admit:
                phase_name = (
                    "waiting_for_capacity"
                    if decision.retryable
                    else "rejected_capacity"
                )
                status_name = "running" if decision.retryable else "failed"
                error_code = f"capacity_gate_{decision.reason or 'denied'}"
                retry_after = 30 if decision.retryable else 600
                LOGGER.info(
                    "blast_gate_deny cluster=%s job_id=%s reason=%s "
                    "retryable=%s slots=%s measured_pct=%s",
                    cluster_name,
                    job_id,
                    decision.reason,
                    decision.retryable,
                    decision.slots_in_use,
                    decision.measured_pct,
                )
                capacity_gate.bump_deny(cluster_name, decision.reason)
                _blast._update_state(
                    job_id,
                    phase_name,
                    status=status_name,
                    event="capacity_gate_deny",
                    error_code=error_code,
                    retry_after_seconds=retry_after,
                )
                if not decision.retryable:
                    return {
                        "job_id": job_id,
                        "status": "failed",
                        "phase": phase_name,
                        "error_code": error_code,
                    }
                try:
                    submit.apply_async(
                        kwargs={
                            "job_id": job_id,
                            "subscription_id": subscription_id,
                            "resource_group": resource_group,
                            "cluster_name": cluster_name,
                            "storage_account": storage_account,
                            "program": program,
                            "database": database,
                            "query_file": query_file,
                            "options": options,
                            "caller_oid": caller_oid,
                            "caller_tenant_id": caller_tenant_id,
                        },
                        countdown=retry_after,
                        queue="blast",
                    )
                except Exception as enq_exc:
                    return _blast._retry_or_fail(
                        self,
                        job_id=job_id,
                        phase=phase_name,
                        exc=enq_exc,
                        error_code="blast_submit_requeue_failed",
                    )
                return {
                    "job_id": job_id,
                    "status": "running",
                    "phase": phase_name,
                    "requeued": True,
                }
            capacity_reservation = capacity_gate.reserve_slot(
                cluster_name, job_id, demand
            )
            if capacity_reservation is None:
                # Lost the atomic reserve race after admit (another worker
                # took the last slot). Treat the same as a retryable deny.
                LOGGER.info(
                    "blast_gate_reserve_lost cluster=%s job_id=%s",
                    cluster_name,
                    job_id,
                )
                capacity_gate.bump_reserve_lost(cluster_name)
                _blast._update_state(
                    job_id,
                    "waiting_for_capacity",
                    status="running",
                    event="capacity_reserve_lost",
                    error_code="capacity_reserve_lost",
                    retry_after_seconds=30,
                )
                try:
                    submit.apply_async(
                        kwargs={
                            "job_id": job_id,
                            "subscription_id": subscription_id,
                            "resource_group": resource_group,
                            "cluster_name": cluster_name,
                            "storage_account": storage_account,
                            "program": program,
                            "database": database,
                            "query_file": query_file,
                            "options": options,
                            "caller_oid": caller_oid,
                            "caller_tenant_id": caller_tenant_id,
                        },
                        countdown=30,
                        queue="blast",
                    )
                except Exception as enq_exc:
                    return _blast._retry_or_fail(
                        self,
                        job_id=job_id,
                        phase="waiting_for_capacity",
                        exc=enq_exc,
                        error_code="blast_submit_requeue_failed",
                    )
                return {
                    "job_id": job_id,
                    "status": "running",
                    "phase": "waiting_for_capacity",
                    "requeued": True,
                }
            LOGGER.info(
                "blast_gate_admit cluster=%s job_id=%s cpu_m=%s mem_mib=%s",
                cluster_name,
                job_id,
                capacity_reservation.cpu_m,
                capacity_reservation.mem_mib,
            )
            capacity_gate.bump_admit(cluster_name)
        else:
            lock_key = submit_lock_key(cluster_name, "default")
            submit_lock = acquire_submit_lock(job_id, lock_key=lock_key)
            if submit_lock is None:
                # Lock contention is expected when two submits target the same
                # (cluster, namespace). Treat it as a wait, not an error — do
                # NOT consume the task's max_retries budget. The current task
                # finishes "successfully" with a queued state row, and a
                # fresh submit task is enqueued for the same job after the
                # cooldown so the dashboard keeps the row visible the whole
                # time the contender is waiting in line.
                _blast._update_state(
                    job_id,
                    "waiting_for_submit_slot",
                    status="running",
                    event="submit_lock_busy",
                    error_code="blast_submit_lock_busy",
                    retry_after_seconds=30,
                )
                try:
                    submit.apply_async(
                        kwargs={
                            "job_id": job_id,
                            "subscription_id": subscription_id,
                            "resource_group": resource_group,
                            "cluster_name": cluster_name,
                            "storage_account": storage_account,
                            "program": program,
                            "database": database,
                            "query_file": query_file,
                            "options": options,
                            "caller_oid": caller_oid,
                            "caller_tenant_id": caller_tenant_id,
                        },
                        countdown=30,
                        queue="blast",
                    )
                except Exception as enq_exc:
                    # If re-enqueue itself fails (broker gone), fall back to
                    # the retry path so we surface the broker error properly.
                    return _blast._retry_or_fail(
                        self,
                        job_id=job_id,
                        phase="waiting_for_submit_slot",
                        exc=enq_exc,
                        error_code="blast_submit_requeue_failed",
                    )
                return {
                    "job_id": job_id,
                    "status": "running",
                    "phase": "waiting_for_submit_slot",
                    "requeued": True,
                }
        try:
            # Azure CLI login was warmed up alongside the warmup poll; retry
            # here only if the cached identity expired between then and now.
            _blast._ensure_terminal_azure_cli_login(terminal_run)
            # Refresh the terminal sidecar's kubeconfig for the target cluster.
            # A stale default context (e.g. previously deleted AKS) silently
            # makes elastic-blast exit 0 while kubectl fails NXDOMAIN.
            _blast._ensure_terminal_kubeconfig_context(
                terminal_run,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
            )
            result = _blast._stream_submit_command(
                job_id=job_id,
                task=self,
                config_content=config_content,
                progress_phase="submitting",
            )
        finally:
            if submit_lock is not None:
                lock_client, lock_token = submit_lock
                release_submit_lock(lock_client, lock_token, lock_key=lock_key)
            if capacity_reservation is not None:
                from api.services.blast import capacity_gate

                capacity_gate.release_slot(cluster_name, job_id)
                capacity_gate.bump_release(cluster_name)
                LOGGER.info(
                    "blast_gate_release cluster=%s job_id=%s",
                    cluster_name,
                    job_id,
                )
            if k8s_lease is not None:
                from api.services.blast import k8s_gate

                # Reuse the credential captured at acquire time and never let a
                # best-effort release raise out of ``finally`` \u2014 that would mask
                # the real submit exception this block is unwinding (round-3 M-D).
                try:
                    k8s_gate.release_k8s_admission(
                        k8s_gate_credential,
                        subscription_id,
                        resource_group,
                        cluster_name,
                        k8s_lease,
                    )
                    LOGGER.info(
                        "blast_k8s_gate_release cluster=%s job_id=%s",
                        cluster_name,
                        job_id,
                    )
                except Exception:  # best-effort release, must not mask
                    LOGGER.warning(
                        "blast_k8s_gate_release_failed cluster=%s job_id=%s",
                        cluster_name,
                        job_id,
                        exc_info=True,
                    )
    except TerminalExecError as exc:
        return _blast._retry_or_fail(
            self,
            job_id=job_id,
            phase="terminal_unavailable",
            exc=exc,
            error_code="terminal_exec_unavailable",
        )
    except _blast.TerminalAzureLoginError as exc:
        return _blast._retry_or_fail(
            self,
            job_id=job_id,
            phase="terminal_az_login_failed",
            exc=exc,
            error_code="terminal_az_login_failed",
        )
    except _blast.TerminalKubeconfigError as exc:
        return _blast._retry_or_fail(
            self,
            job_id=job_id,
            phase="terminal_kubeconfig_failed",
            exc=exc,
            error_code="terminal_kubeconfig_failed",
        )

    submit_log_events = result.pop("_log_events", [])
    if isinstance(submit_log_events, list):
        persist_submit_log_events(
            job_id=job_id,
            progress_phase="submitting",
            events=submit_log_events,
        )
    if result.get("stdout") or result.get("stderr"):
        _blast._update_state(
            job_id,
            "submitting",
            status="running",
            event="submit_log",
            last_output=_blast._tail_text(
                [str(line) for line in (result.get("stdout"), result.get("stderr")) if line]
            ),
            log_line_count=result.get("log_line_count"),
        )

    payload = _blast._last_json(str(result.get("stdout", "")))
    exit_code = int(result.get("exit_code", 1) or 0)
    submit_output = "\n".join(
        str(value) for value in (result.get("stdout"), result.get("stderr")) if value
    )
    elastic_blast_job_id = _blast._extract_elastic_blast_job_id(
        result.get("stdout")
    ) or _blast._discover_elastic_blast_job_id(
        storage_account,
        job_id,
    )
    if exit_code == 0:
        if requires_node_warmup and not reuses_warmed_ssd:
            _blast._update_state(
                job_id,
                "staging_db",
                status="completed",
                output=_blast._snippet(submit_output, _blast.LIVE_OUTPUT_SNIPPET_CHARS),
                last_output=_blast._snippet(submit_output, _blast.LIVE_OUTPUT_SNIPPET_CHARS),
                log_line_count=result.get("log_line_count"),
                exit_code=exit_code,
                terminal_duration_ms=result.get("duration_ms"),
                timed_out=result.get("timed_out"),
            )
        _blast._update_state(
            job_id,
            "submitting",
            status="completed",
            output=_blast._snippet(submit_output, _blast.LIVE_OUTPUT_SNIPPET_CHARS),
            last_output=_blast._snippet(submit_output, _blast.LIVE_OUTPUT_SNIPPET_CHARS),
            log_line_count=result.get("log_line_count"),
            exit_code=exit_code,
            terminal_duration_ms=result.get("duration_ms"),
            timed_out=result.get("timed_out"),
        )
        phase, status = _blast._submit_success_status(payload)
        if status == "running":
            phase, status, k8s_status = _blast._refresh_submit_terminal_status(
                job_id=job_id,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
                k8s_job_id=elastic_blast_job_id or None,
            )
        else:
            k8s_status = None
        phase, status = _blast._gate_completed_submit_on_results(
            job_id=job_id,
            storage_account=storage_account,
            phase=phase,
            status=status,
        )
        _blast._update_state(
            job_id,
            phase,
            status=status,
            decision=(payload or {}).get("decision"),
            cluster_name=(payload or {}).get("cluster_name"),
            elastic_blast_job_id=elastic_blast_job_id or None,
            k8s=k8s_status,
            output=_blast._snippet(submit_output, _blast.STDOUT_SNIPPET_CHARS),
            exit_code=exit_code,
            elastic_blast_submit_duration_ms=result.get("duration_ms"),
            timed_out=result.get("timed_out"),
        )
        # Kick off the per-job poller so the dashboard catches the K8s →
        # completed transition within ~10 s instead of waiting up to 60 s
        # for the next beat reconcile tick. The poller self-throttles via
        # the shared K8s refresh interval and self-stops on terminal phases.
        if status == "running" and phase in _POLL_RUNNING_ELIGIBLE_PHASES:
            try:
                poll_running_status.apply_async(
                    kwargs={"job_id": job_id, "iteration": 0},
                    countdown=POLL_RUNNING_START_DELAY,
                    queue="blast",
                )
            except Exception as exc:
                LOGGER.warning(
                    "submit: poll_running_status enqueue failed job_id=%s: %s",
                    job_id,
                    type(exc).__name__,
                )
        return {
            "job_id": job_id,
            "status": status,
            "phase": phase,
            "decision": (payload or {}).get("decision", "accepted"),
            "k8s": k8s_status,
            "output": _blast._snippet(submit_output, _blast.STDOUT_SNIPPET_CHARS),
        }

    if elastic_blast_job_id:
        phase, status, k8s_status = _blast._refresh_submit_terminal_status(
            job_id=job_id,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            k8s_job_id=elastic_blast_job_id,
        )
        if status in {"running", "completed"}:
            phase, status = _blast._gate_completed_submit_on_results(
                job_id=job_id,
                storage_account=storage_account,
                phase=phase,
                status=status,
            )
            _blast._update_state(
                job_id,
                phase,
                status=status,
                elastic_blast_job_id=elastic_blast_job_id,
                k8s=k8s_status,
                output=_blast._snippet(submit_output, _blast.STDOUT_SNIPPET_CHARS),
                warning=_blast._snippet(submit_output, _blast.ERROR_SNIPPET_CHARS),
                exit_code=exit_code,
                elastic_blast_submit_duration_ms=result.get("duration_ms"),
                timed_out=result.get("timed_out"),
            )
            return {
                "job_id": job_id,
                "status": status,
                "phase": phase,
                "decision": "accepted",
                "k8s": k8s_status,
                "output": _blast._snippet(submit_output, _blast.STDOUT_SNIPPET_CHARS),
            }

    error = _blast._result_error(result, payload)
    if _blast._is_retryable_result(result, payload):
        return _blast._retry_or_fail(
            self,
            job_id=job_id,
            phase="submit_retryable_failure",
            exc=RuntimeError(error),
            error_code=str((payload or {}).get("category") or "submit_retryable_failure"),
            retry_after_seconds=_blast._retry_after(payload, default=30),
        )

    # Enrich known non-retryable failures with an actionable remediation hint.
    # ElasticBLAST's full-DB memory rejection is accurate but opaque to a
    # dashboard user who cannot edit the generated INI — point them at the
    # "Sharded throughput" profile or a larger-SKU cluster instead.
    guidance = _blast._submit_failure_guidance(error)
    if guidance:
        error = f"{error}\n\n{guidance}"
    # Emit an explicit worker-log record for the non-retryable submit failure.
    # Without this the task only writes the failure into job state and returns,
    # so the worker container log carried NO line explaining why a submit
    # failed — a "no output captured" job looked silent in Log Analytics. Log
    # the authoritative signals (exit code, timeout, captured-output size) plus
    # an error snippet so the failure is greppable by job_id.
    LOGGER.error(
        "blast_submit_failed job_id=%s cluster=%s exit_code=%s timed_out=%s "
        "log_lines=%s error=%s",
        job_id,
        cluster_name,
        exit_code,
        result.get("timed_out"),
        result.get("log_line_count"),
        _blast._snippet(error, _blast.ERROR_SNIPPET_CHARS),
    )
    # Persist the full submit console output (not just the 500-char error_code
    # tail) on the failed ``submitting`` step so the Run details page can render
    # the complete stdout/stderr the operator needs to diagnose the failure. The
    # frontend's submit-step builder reads ``output`` / ``last_output`` first and
    # only falls back to the short error text when they are empty, so without
    # this a non-retryable submit failure showed the truncated tail (or, when the
    # parsed error was empty, "No detailed error was recorded").
    _blast._update_state(
        job_id,
        "submit_failed",
        status="failed",
        error_code=error,
        output=_blast._snippet(submit_output, _blast.LIVE_OUTPUT_SNIPPET_CHARS),
        last_output=_blast._snippet(submit_output, _blast.LIVE_OUTPUT_SNIPPET_CHARS),
        exit_code=exit_code,
        log_line_count=result.get("log_line_count"),
        terminal_duration_ms=result.get("duration_ms"),
        timed_out=result.get("timed_out"),
    )
    return {"job_id": job_id, "status": "failed", "phase": "submit_failed", "error": error}


__all__ = ("submit",)

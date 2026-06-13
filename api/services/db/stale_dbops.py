"""Reconcile stale DB-ops / warmup jobstate rows to a terminal status.

Responsibility: Drive ``warmup`` and ``prepare_db_*`` / ``shard`` / ``oracle``
    jobstate rows that are stuck in an active status (``queued`` / ``running``)
    to a terminal status when the work that owned them is provably gone. A
    crashed worker (or a synchronous route whose audit row was born ``queued``)
    leaves these rows active forever; ``api.tasks.blast.reconcile_stale_jobs``
    only scans ``job_type="blast"``, and the metadata reconciler
    (``reconcile_orphaned_prepare_db``) only fixes ``{db}-metadata.json``, never
    the Table row. This module closes that gap with a careful, per-type
    decision so an in-flight DB op is never killed mid-flight.
Edit boundaries: ``classify_dbops_row`` is a PURE decision function (no IO) so
    every branch is unit-testable; the orchestrator ``reconcile_stale_dbops``
    does the Table scan, the Celery ``AsyncResult`` probe, and the terminal
    write through ``state_repo``. No Azure SDK, no Kubernetes, no Storage reads
    — the authoritative signal is the Celery task result plus a generous
    per-type quiet threshold. Do NOT re-dispatch any work from here.
Key entry points: ``classify_dbops_row`` (pure), ``reconcile_dbops_decision``
    (per-row IO glue), ``reconcile_dbops`` (orchestrator).
Risky contracts:
    * Synchronous-by-design ops (``prepare_db_cancel`` / ``prepare_db_delete``)
      are terminalised to ``completed`` regardless of age — they finished
      inside their request, so an active row is a stale audit artefact, never a
      live op. New rows are born terminal at the source (``record_db_op
      status="completed"``); this only mops up pre-existing rows.
    * Asynchronous ops are terminalised only when Celery reports a terminal
      task state (``SUCCESS`` → ``completed``, ``FAILURE`` / ``REVOKED`` →
      ``failed``) OR the row has been quiet longer than the per-type threshold
      AND Celery has no live record. The threshold MUST exceed the task's own
      hard time limit (``prepare_db_aks`` ≈ 4 h 45 m) so a genuinely-running
      download is never aged out — see ``_PREPARE_DB_STALE_SECONDS``.
    * Idempotent: a row already terminal is skipped; running twice is a no-op.
Validation: ``uv run pytest -q api/tests/test_stale_dbops_reconcile.py``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)

# Active statuses a row can be stuck in. Mirrors
# ``JobStateRepository.list_active`` so the scan and the gate agree.
_ACTIVE_STATUSES = frozenset({"queued", "pending", "running", "reducing"})

# Celery task states that are terminal and authoritative.
_CELERY_SUCCESS = "SUCCESS"
_CELERY_TERMINAL_FAILED = frozenset({"FAILURE", "REVOKED"})


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# Quiet threshold for warmup rows. The warmup orchestrator is bound by the
# Celery hard time limit (default 3600 s); its node-warm K8s Jobs are short
# (~10-15 min). 2 h (2x the hard limit) provably exceeds any live warmup, so a
# row untouched that long is a crashed-worker zombie. Aligned with the
# auto-stop evaluator's ``_ACTIVE_ROW_STALE_SECONDS`` default for consistency.
_WARMUP_STALE_SECONDS = _env_int("STALE_DBOPS_WARMUP_SECONDS", 7200)

# Quiet threshold for prepare-db / shard / oracle rows. A real ``nt`` /
# ``core_nt`` download legitimately runs for hours; ``prepare_db_aks`` carries a
# task time limit of roughly ``_JOB_POLL_MAX_SECONDS + 30 min`` ≈ 4 h 45 m. The
# threshold must comfortably exceed that so a live download is never aged out.
_PREPARE_DB_STALE_SECONDS = _env_int("STALE_DBOPS_PREPARE_DB_SECONDS", 21600)

# Per-type reconciliation policy.
#   synchronous=True  → the op finished inside its request; an active row is a
#                       stale audit artefact, terminalise to ``completed``
#                       regardless of age / task state.
#   synchronous=False → asynchronous op; terminalise only on a terminal Celery
#                       state or after ``stale_seconds`` of quiet with no live
#                       task. The lost terminal is ``failed`` (``worker_lost``).
@dataclass(frozen=True)
class _TypePolicy:
    synchronous: bool
    stale_seconds: int


_TYPE_POLICY: dict[str, _TypePolicy] = {
    "warmup": _TypePolicy(synchronous=False, stale_seconds=_WARMUP_STALE_SECONDS),
    "prepare_db": _TypePolicy(synchronous=False, stale_seconds=_PREPARE_DB_STALE_SECONDS),
    "prepare_db_aks": _TypePolicy(
        synchronous=False, stale_seconds=_PREPARE_DB_STALE_SECONDS
    ),
    "shard": _TypePolicy(synchronous=False, stale_seconds=_PREPARE_DB_STALE_SECONDS),
    "oracle": _TypePolicy(synchronous=False, stale_seconds=_PREPARE_DB_STALE_SECONDS),
    # Synchronous-by-design ops: born terminal at the source now, but mop up any
    # pre-existing rows that leaked while the source still wrote ``queued``.
    "prepare_db_cancel": _TypePolicy(synchronous=True, stale_seconds=0),
    "prepare_db_delete": _TypePolicy(synchronous=True, stale_seconds=0),
}

#: The jobstate ``type`` values this reconciler is responsible for.
RECONCILE_TYPES: tuple[str, ...] = tuple(_TYPE_POLICY)


@dataclass(frozen=True)
class DbopsDecision:
    """Outcome of classifying one active dbops/warmup row.

    ``action`` is ``"terminalize"`` (write ``status`` / ``phase`` / optional
    ``error_code``) or ``"skip"`` (leave the row alone). ``reason`` is a short
    machine-stable tag for logs / tests.
    """

    action: str  # "terminalize" | "skip"
    status: str  # terminal status when action == "terminalize"
    phase: str
    error_code: str
    reason: str


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def classify_dbops_row(
    *,
    row_type: str,
    status: str,
    updated_at: str | None,
    created_at: str | None,
    has_task_id: bool,
    celery_state: str | None,
    now: datetime,
) -> DbopsDecision:
    """Decide whether a single active dbops/warmup row should be terminalised.

    Pure function (no IO). ``celery_state`` is the upper-cased Celery task
    state (``"SUCCESS"`` / ``"FAILURE"`` / ``"REVOKED"`` / ``"PENDING"`` / …)
    or ``None`` when the row carries no task id or the lookup was skipped.
    ``has_task_id`` distinguishes "no task was ever dispatched" (synchronous /
    daemon-thread work) from "task existed but Celery forgot it" — both yield a
    ``None`` ``celery_state``, but only the latter can ever flip to a terminal
    Celery state on a later tick.
    """
    if status not in _ACTIVE_STATUSES:
        # Already terminal (or an unexpected status) — never touch it.
        return DbopsDecision("skip", status, "", "", "already-terminal")

    policy = _TYPE_POLICY.get(row_type)
    if policy is None:
        return DbopsDecision("skip", status, "", "", "type-not-managed")

    # Synchronous-by-design ops: the work completed inside the originating
    # request, so an active row is a stale audit artefact. Drive it to
    # ``completed`` without consulting Celery or age — there is no live op to
    # protect and no later writer.
    if policy.synchronous:
        return DbopsDecision(
            "terminalize", "completed", "completed", "", "synchronous-op-completed"
        )

    # Asynchronous ops: Celery is the authoritative terminal signal.
    if celery_state == _CELERY_SUCCESS:
        return DbopsDecision(
            "terminalize", "completed", "completed", "", "celery-success"
        )
    if celery_state in _CELERY_TERMINAL_FAILED:
        return DbopsDecision(
            "terminalize", "failed", "failed", "task_failed", "celery-terminal-failed"
        )

    # Celery is non-terminal (PENDING / STARTED / RETRY) or unknown. Fall back
    # to a quiet-age check. A row untouched longer than the per-type threshold,
    # with no live Celery record, is a crashed-worker zombie.
    last = _parse_iso(updated_at) or _parse_iso(created_at)
    if last is None:
        # No parseable timestamp → fail safe, keep it (a future tick with a
        # parseable row, or a Celery terminal state, will resolve it).
        return DbopsDecision("skip", status, "", "", "no-timestamp")

    quiet_seconds = (now - last).total_seconds()
    if quiet_seconds >= policy.stale_seconds:
        return DbopsDecision(
            "terminalize", "failed", "failed", "worker_lost", "aged-out-worker-lost"
        )

    if has_task_id and celery_state:
        return DbopsDecision("skip", status, "", "", "task-live")
    return DbopsDecision("skip", status, "", "", "within-threshold")


def reconcile_dbops_decision(repo: Any, row: Any, *, celery_app: Any, now: datetime) -> str:
    """Classify one row (probing Celery as needed) and apply the terminal write.

    Returns the decision ``reason`` (or ``"error"`` on an unexpected failure)
    so the orchestrator can tally outcomes. Never raises.
    """
    try:
        row_type = str(getattr(row, "type", "") or "")
        status = str(getattr(row, "status", "") or "")
        task_id = str(getattr(row, "task_id", "") or "").strip()
        if not task_id:
            payload = getattr(row, "payload", None)
            if isinstance(payload, dict):
                task_id = str(payload.get("task_id") or "").strip()

        celery_state: str | None = None
        if task_id:
            try:
                from celery.result import AsyncResult

                celery_state = str(
                    AsyncResult(task_id, app=celery_app).status or ""
                ).upper() or None
            except Exception as exc:
                LOGGER.debug(
                    "reconcile_dbops: AsyncResult failed job_id=%s: %s",
                    getattr(row, "job_id", "?"),
                    type(exc).__name__,
                )

        decision = classify_dbops_row(
            row_type=row_type,
            status=status,
            updated_at=getattr(row, "updated_at", None),
            created_at=getattr(row, "created_at", None),
            has_task_id=bool(task_id),
            celery_state=celery_state,
            now=now,
        )
        if decision.action != "terminalize":
            return decision.reason

        job_id = str(getattr(row, "job_id", "") or "")
        try:
            repo.update(
                job_id,
                status=decision.status,
                phase=decision.phase,
                error_code=decision.error_code or None,
            )
            repo.append_history(
                job_id,
                decision.status,
                {
                    "source": "reconcile_stale_dbops",
                    "reason": decision.reason,
                    "error_code": decision.error_code,
                },
            )
        except KeyError:
            # Row vanished between scan and write (deleted) — nothing to do.
            return "row-gone"
        return decision.reason
    except Exception as exc:
        LOGGER.warning(
            "reconcile_dbops: row failed job_id=%s: %s",
            getattr(row, "job_id", "?"),
            type(exc).__name__,
        )
        return "error"


def reconcile_dbops(*, limit: int = 200, enabled: bool | None = None) -> dict[str, Any]:
    """Scan active dbops/warmup rows and terminalise the provably-dead ones.

    Beat-scheduled. Idempotent. Never raises — every failure is logged and
    folded into the returned summary so a transient Table / broker hiccup does
    not crash the worker.
    """
    summary: dict[str, Any] = {
        "scanned": 0,
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
    }

    if enabled is None:
        enabled = os.environ.get("STALE_DBOPS_RECONCILE_ENABLED", "true").lower() not in {
            "false",
            "0",
            "no",
        }
    if not enabled:
        summary["disabled"] = True
        return summary

    try:
        from api.celery_app import celery_app
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
    except Exception as exc:
        LOGGER.warning("reconcile_dbops: setup failed: %s", exc)
        summary["errors"] = 1
        return summary

    now = datetime.now(UTC)
    for row_type in RECONCILE_TYPES:
        try:
            rows = repo.list_active(job_type=row_type, limit=limit)
        except Exception as exc:
            LOGGER.warning(
                "reconcile_dbops: list_active failed type=%s: %s", row_type, exc
            )
            summary["errors"] += 1
            continue
        for row in rows:
            summary["scanned"] += 1
            reason = reconcile_dbops_decision(
                repo, row, celery_app=celery_app, now=now
            )
            if reason == "error":
                summary["errors"] += 1
            elif reason in {"celery-success", "synchronous-op-completed"}:
                summary["completed"] += 1
            elif reason in {"celery-terminal-failed", "aged-out-worker-lost"}:
                summary["failed"] += 1
            else:
                summary["skipped"] += 1

    if summary["completed"] or summary["failed"] or summary["errors"]:
        LOGGER.info(
            "reconcile_dbops: scanned=%(scanned)d completed=%(completed)d "
            "failed=%(failed)d skipped=%(skipped)d errors=%(errors)d",
            summary,
        )
    return summary


__all__ = [
    "RECONCILE_TYPES",
    "DbopsDecision",
    "classify_dbops_row",
    "reconcile_dbops",
    "reconcile_dbops_decision",
]

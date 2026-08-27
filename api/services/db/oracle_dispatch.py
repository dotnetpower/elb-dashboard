"""Claim and enqueue DB order-oracle builds.

Responsibility: Resolve current readiness, create/adopt the durable active/run
    claim, seed JobState, enqueue the storage task, and terminally roll back a
    broker failure.
Edit boundaries: Pre-task dispatch transaction only; Kubernetes execution,
    preference reconciliation, HTTP shaping, and retention belong to their
    owning modules.
Key entry points: `start_oracle_build`, `OracleDispatchResult`.
Risky contracts: JobState exists before broker send; broker failure marks only
    the new run failed and releases its active claim; identical ready/current
    identities are no-ops; adopted claims retain their original owner token.
Validation: `uv run pytest -q api/tests/test_oracle_dispatch.py`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from api.services.db.oracle_build import resolve_oracle_build_context
from api.services.db.oracle_state import (
    OracleClaimResult,
    claim_oracle_build,
    fail_oracle_run,
    oracle_container,
    read_oracle_active,
    read_oracle_current,
    release_oracle_active,
    update_oracle_active,
    update_oracle_run,
)
from api.services.db.order_oracle import ORACLE_PARTS_DIR, ORACLE_PREFIX_ROOT
from api.services.env import env_int

_BUILD_TIMEOUT_SECONDS = env_int("ORACLE_BUILD_TIMEOUT_SECONDS", 1800, minimum=60, maximum=7200)
_UNCLAIMED_REDELIVERY_SECONDS = env_int(
    "ORACLE_UNCLAIMED_REDELIVERY_SECONDS", 120, minimum=30, maximum=900
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OracleDispatchResult:
    accepted: bool
    status: str
    db_name: str
    run_id: str
    job_id: str
    task_id: str
    identity: str
    expected_parts: int
    status_blob: str
    part_prefix: str
    adopted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(UTC)


def _job_id(storage_account: str, db_name: str, run_id: str) -> str:
    return f"dbops:oracle:{storage_account}:{db_name}:{run_id}"


def _create_job_state(
    *,
    document: dict[str, Any],
    owner_oid: str,
    tenant_id: str,
) -> None:
    from api.services.state.job_state import JobState
    from api.services.state_repo import get_state_repo

    payload = dict(document)
    now = str(document["started_at"])
    get_state_repo().create(
        JobState(
            job_id=str(document["job_id"]),
            type="oracle",
            status="queued",
            phase="queued",
            owner_oid=owner_oid or None,
            tenant_id=tenant_id or None,
            created_at=now,
            updated_at=now,
            payload=payload,
            job_title=f"Build DB order oracle - {document['db_name']}",
            db=str(document["db_name"]),
            subscription_id=str(document["subscription_id"]),
            resource_group=str(document["cluster_resource_group"]),
            cluster_name=str(document["cluster_name"]),
            storage_account=str(document["storage_account"]),
        )
    )


def _attach_task_id(job_id: str, task_id: str) -> None:
    from api.services.state_repo import get_state_repo

    get_state_repo().update(job_id, task_id=task_id)


def _mark_enqueue_failed(
    *,
    container: Any,
    document: dict[str, Any],
    message: str,
) -> None:
    finished_at = _now().isoformat(timespec="seconds")
    automation_recorded = True
    if bool(document.get("automatic")):
        try:
            from api.services.db.oracle_retry import record_automation_failure

            record_automation_failure(
                container,
                db_name=str(document["db_name"]),
                run_id=str(document["run_id"]),
                error_code="oracle_enqueue_failed",
            )
        except Exception as exc:
            automation_recorded = False
            LOGGER.warning(
                "oracle automation enqueue failure state skipped run_id=%s reason=%s",
                document.get("run_id"),
                type(exc).__name__,
            )
    if not automation_recorded:
        try:
            from api.services.state_repo import get_state_repo

            get_state_repo().update(
                str(document["job_id"]),
                status="running",
                phase="enqueue_state_pending",
                error_code="oracle_enqueue_failed",
            )
        except Exception as exc:
            LOGGER.warning(
                "oracle enqueue pending JobState update skipped job_id=%s reason=%s",
                document.get("job_id"),
                type(exc).__name__,
            )
        return
    terminalized = False
    try:
        fail_oracle_run(
            container,
            db_name=str(document["db_name"]),
            run_id=str(document["run_id"]),
            owner_operation_id=str(document["owner_operation_id"]),
            error_code="oracle_enqueue_failed",
            error=message,
            finished_at=finished_at,
        )
        terminalized = True
    except Exception as exc:
        LOGGER.warning(
            "oracle enqueue rollback state failed run_id=%s reason=%s",
            document.get("run_id"),
            type(exc).__name__,
        )
    try:
        from api.services.state_repo import get_state_repo

        if terminalized:
            get_state_repo().update(
                str(document["job_id"]),
                status="failed",
                phase="enqueue_failed",
                error_code="oracle_enqueue_failed",
            )
        else:
            get_state_repo().update(
                str(document["job_id"]),
                status="running",
                phase="enqueue_terminal_pending",
                error_code="oracle_enqueue_failed",
            )
    except Exception as exc:
        LOGGER.warning(
            "oracle JobState enqueue failure update skipped job_id=%s reason=%s",
            document.get("job_id"),
            type(exc).__name__,
        )


def _task_kwargs(document: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "job_id",
        "run_id",
        "owner_operation_id",
        "subscription_id",
        "storage_resource_group",
        "storage_account",
        "cluster_resource_group",
        "cluster_name",
        "db_name",
        "image",
        "identity",
        "requested_source_version",
        "automatic",
        "dispatch_token",
    )
    return {key: document.get(key, "") for key in keys}


def _recover_terminal_active_claim(
    credential: Any,
    container: Any,
    *,
    db_name: str,
    now: datetime,
) -> str:
    """Release published leftovers or fail a run whose hard deadline elapsed."""
    active = read_oracle_active(container, db_name)
    if not isinstance(active, dict):
        return "none"
    run_id = str(active.get("run_id") or "")
    owner = str(active.get("owner_operation_id") or "")
    current = read_oracle_current(container, db_name)
    if (
        isinstance(current, dict)
        and current.get("status") == "ready"
        and str(current.get("run_id") or "") == run_id
        and owner
    ):
        try:
            repair_updates = {
                key: current[key]
                for key in (
                    "status",
                    "phase",
                    "ready_parts",
                    "expected_parts",
                    "finished_at",
                    "previous_run_id",
                )
                if key in current
            }
            update_oracle_run(
                container,
                db_name=db_name,
                run_id=run_id,
                owner_operation_id=owner,
                updates=repair_updates,
            )
        except Exception as exc:
            LOGGER.warning(
                "oracle published run history repair skipped run_id=%s reason=%s",
                run_id,
                type(exc).__name__,
            )
            return "published_pending"
        try:
            from api.services.db.oracle_retry import record_automation_success

            record_automation_success(
                container,
                db_name=db_name,
                run_id=run_id,
                require_current_run=bool(active.get("automatic")),
            )
        except Exception as exc:
            LOGGER.warning(
                "oracle published automation repair skipped run_id=%s reason=%s",
                run_id,
                type(exc).__name__,
            )
            if bool(active.get("automatic")):
                return "published_pending"
        release_oracle_active(
            container,
            db_name=db_name,
            owner_operation_id=owner,
        )
        try:
            from api.services.state_repo import get_state_repo

            get_state_repo().update(
                str(active.get("job_id") or ""),
                status="completed",
                phase="completed",
                error_code="",
            )
        except Exception as exc:
            LOGGER.warning(
                "oracle published JobState repair skipped run_id=%s reason=%s",
                run_id,
                type(exc).__name__,
            )
        return "published"
    deadline_raw = str(active.get("deadline_at") or "")
    try:
        deadline = datetime.fromisoformat(deadline_raw.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
    except ValueError:
        return "active"
    if now <= deadline or not run_id or not owner:
        return "active"
    job_names = [
        str(name) for name in active.get("job_names", []) if isinstance(name, str) and name
    ]
    if job_names:
        from api.services.db.oracle_runtime import cleanup_oracle_jobs

        cleanup = cleanup_oracle_jobs(
            credential,
            subscription_id=str(active.get("subscription_id") or ""),
            cluster_resource_group=str(active.get("cluster_resource_group") or ""),
            cluster_name=str(active.get("cluster_name") or ""),
            namespace=str(active.get("namespace") or "default"),
            job_names=job_names,
        )
        if cleanup["errors"]:
            LOGGER.warning(
                "oracle expired Job cleanup incomplete run_id=%s errors=%d",
                run_id,
                len(cleanup["errors"]),
            )
    if bool(active.get("automatic")):
        try:
            from api.services.db.oracle_retry import record_automation_failure

            record_automation_failure(
                container,
                db_name=db_name,
                run_id=run_id,
                error_code="oracle_execution_deadline_exceeded",
            )
        except Exception as exc:
            LOGGER.warning(
                "oracle expired automation state skipped run_id=%s reason=%s",
                run_id,
                type(exc).__name__,
            )
            return "expired_pending"
    fail_oracle_run(
        container,
        db_name=db_name,
        run_id=run_id,
        owner_operation_id=owner,
        error_code="oracle_execution_deadline_exceeded",
        error="oracle execution did not finish before its durable deadline",
        finished_at=now.isoformat(timespec="seconds"),
    )
    try:
        from api.services.state_repo import get_state_repo

        get_state_repo().update(
            str(active.get("job_id") or ""),
            status="failed",
            phase="failed",
            error_code="oracle_execution_deadline_exceeded",
        )
    except Exception as exc:
        LOGGER.warning(
            "oracle expired JobState update skipped run_id=%s reason=%s",
            run_id,
            type(exc).__name__,
        )
    return "expired"


def start_oracle_build(
    credential: Any,
    *,
    subscription_id: str,
    storage_resource_group: str,
    storage_account: str,
    cluster_resource_group: str,
    cluster_name: str,
    db_name: str,
    image: str,
    requested_source_version: str = "",
    owner_oid: str = "",
    tenant_id: str = "",
    automatic: bool = False,
    send_task: Any | None = None,
) -> OracleDispatchResult:
    """Resolve, claim, seed, and enqueue one idempotent oracle build."""
    now = _now()
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
    container = oracle_container(credential, storage_account)
    recovered = _recover_terminal_active_claim(
        credential,
        container,
        db_name=db_name,
        now=now,
    )
    if recovered == "expired" and automatic:
        from api.services.db.oracle_build import OracleBuildBlocked

        raise OracleBuildBlocked(
            "oracle_previous_run_expired",
            "the previous automatic oracle run expired; retry backoff is active",
            status_code=409,
        )
    run_id = now.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    owner_operation_id = uuid.uuid4().hex
    job_id = _job_id(storage_account, db_name, run_id)
    part_prefix = f"{ORACLE_PREFIX_ROOT}/{db_name}/{ORACLE_PARTS_DIR}/{run_id}/"
    document: dict[str, Any] = {
        "schema_version": 1,
        "status": "queued",
        "phase": "queued",
        "db_name": db_name,
        "run_id": run_id,
        "job_id": job_id,
        "task_id": "",
        "owner_operation_id": owner_operation_id,
        "requested_by": owner_oid,
        "automatic": automatic,
        "source_version": context.source_version,
        "requested_source_version": requested_source_version,
        "layout_schema": context.layout_schema,
        "layout_fingerprint": context.layout_fingerprint,
        "identity": context.identity,
        "expected_parts": context.expected_parts,
        "expected_shards": list(context.shards),
        "ready_parts": 0,
        "part_prefix": part_prefix,
        "subscription_id": subscription_id,
        "storage_resource_group": storage_resource_group,
        "storage_account": storage_account,
        "cluster_resource_group": cluster_resource_group,
        "cluster_name": cluster_name,
        "image": image,
        "started_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "deadline_at": (now + timedelta(seconds=_BUILD_TIMEOUT_SECONDS)).isoformat(
            timespec="seconds"
        ),
    }
    claim: OracleClaimResult = claim_oracle_build(
        container,
        db_name=db_name,
        document=document,
    )
    if claim.outcome == "ready":
        ready = claim.document
        try:
            from api.services.db.oracle_retry import record_automation_success

            record_automation_success(
                container,
                db_name=db_name,
                run_id=str(ready.get("run_id") or ""),
                require_current_run=automatic,
            )
        except Exception as exc:
            LOGGER.warning(
                "oracle ready no-op automation state skipped run_id=%s reason=%s",
                ready.get("run_id"),
                type(exc).__name__,
            )
        return OracleDispatchResult(
            accepted=False,
            status="ready",
            db_name=db_name,
            run_id=str(ready.get("run_id") or ""),
            job_id=str(ready.get("job_id") or ""),
            task_id=str(ready.get("task_id") or ""),
            identity=context.identity,
            expected_parts=int(ready.get("expected_parts") or context.expected_parts),
            status_blob=f"{ORACLE_PREFIX_ROOT}/{db_name}/status.json",
            part_prefix=str(ready.get("part_prefix") or ""),
            adopted=True,
        )

    active = claim.document
    adopted = claim.outcome == "adopted"
    if adopted:
        document = dict(active)
        existing_task_id = str(document.get("task_id") or "")
        if existing_task_id:
            last_dispatched = str(document.get("last_dispatched_at") or "")
            try:
                previous_dispatched_at = datetime.fromisoformat(
                    last_dispatched.replace("Z", "+00:00")
                )
                if previous_dispatched_at.tzinfo is None:
                    previous_dispatched_at = previous_dispatched_at.replace(tzinfo=UTC)
                dispatch_age = (now - previous_dispatched_at).total_seconds()
            except ValueError:
                dispatch_age = float("inf")
            if (
                str(document.get("execution_instance_id") or "")
                or dispatch_age < _UNCLAIMED_REDELIVERY_SECONDS
            ):
                return OracleDispatchResult(
                    accepted=False,
                    status=str(document.get("status") or "running"),
                    db_name=db_name,
                    run_id=str(document["run_id"]),
                    job_id=str(document.get("job_id") or ""),
                    task_id=existing_task_id,
                    identity=str(document["identity"]),
                    expected_parts=int(document.get("expected_parts") or 0),
                    status_blob=f"{ORACLE_PREFIX_ROOT}/{db_name}/status.json",
                    part_prefix=str(document.get("part_prefix") or ""),
                    adopted=True,
                )

    try:
        _create_job_state(
            document=document,
            owner_oid=str(document.get("requested_by") or owner_oid),
            tenant_id=tenant_id,
        )
    except Exception as exc:
        _mark_enqueue_failed(
            container=container,
            document=document,
            message=f"JobState create failed: {type(exc).__name__}",
        )
        raise

    if send_task is None:
        from api.celery_app import celery_app

        send_task = celery_app.send_task
    owner_operation_id = str(document["owner_operation_id"])
    task_id = uuid.uuid4().hex
    dispatch_token = uuid.uuid4().hex
    dispatched_at_iso = _now().isoformat(timespec="seconds")
    document.update(
        {
            "task_id": task_id,
            "dispatch_token": dispatch_token,
            "last_dispatched_at": dispatched_at_iso,
            "dispatch_attempt": int(document.get("dispatch_attempt") or 0) + 1,
            "execution_instance_id": "",
            "execution_started_at": "",
        }
    )
    try:
        update_oracle_run(
            container,
            db_name=db_name,
            run_id=str(document["run_id"]),
            owner_operation_id=owner_operation_id,
            updates={
                "task_id": task_id,
                "dispatch_token": dispatch_token,
                "last_dispatched_at": dispatched_at_iso,
                "dispatch_attempt": document["dispatch_attempt"],
                "execution_instance_id": "",
                "execution_started_at": "",
            },
        )
        update_oracle_active(
            container,
            db_name=db_name,
            owner_operation_id=owner_operation_id,
            updates={
                "task_id": task_id,
                "dispatch_token": dispatch_token,
                "last_dispatched_at": dispatched_at_iso,
                "dispatch_attempt": document["dispatch_attempt"],
                "execution_instance_id": "",
                "execution_started_at": "",
            },
        )
        if bool(document.get("automatic")):
            from api.services.db.oracle_retry import record_automation_dispatch

            record_automation_dispatch(
                container,
                db_name=db_name,
                run_id=str(document["run_id"]),
            )
        _attach_task_id(str(document["job_id"]), task_id)
    except Exception as exc:
        _mark_enqueue_failed(
            container=container,
            document=document,
            message=f"dispatch state prepare failed: {type(exc).__name__}",
        )
        raise

    try:
        task = send_task(
            "api.tasks.storage.build_db_order_oracle",
            kwargs=_task_kwargs(document),
            queue="storage",
            task_id=task_id,
        )
        published_task_id = str(getattr(task, "id", "") or task_id)
        if published_task_id != task_id:
            raise RuntimeError("broker returned a different task id")
    except Exception as exc:
        _mark_enqueue_failed(
            container=container,
            document=document,
            message=f"broker enqueue failed: {type(exc).__name__}",
        )
        raise
    return OracleDispatchResult(
        accepted=True,
        status="queued",
        db_name=db_name,
        run_id=str(document["run_id"]),
        job_id=str(document["job_id"]),
        task_id=task_id,
        identity=str(document["identity"]),
        expected_parts=int(document["expected_parts"]),
        status_blob=f"{ORACLE_PREFIX_ROOT}/{db_name}/status.json",
        part_prefix=str(document["part_prefix"]),
        adopted=adopted,
    )


__all__ = ["OracleDispatchResult", "start_oracle_build"]

"""BLAST submit/cancel Celery tasks.

Side effects: Invokes `elastic-blast submit` / `elastic-blast delete` via
the terminal sidecar. Job state is tracked in Azure Table Storage.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _update_state(job_id: str, phase: str, status: str = "running", **extra: Any) -> None:
    """Best-effort state update to the job state repo."""
    try:
        from api.services.state_repo import JobStateRepository
        repo = JobStateRepository()
        state = repo.get(job_id)
        if state:
            state.status = status
            state.phase = phase
            state.updated_at = _now_iso()
            for k, v in extra.items():
                if hasattr(state, k):
                    setattr(state, k, v)
            repo.update(state)
            repo.append_history(job_id, {"phase": phase, "status": status, **extra})
    except Exception as exc:
        LOGGER.warning("state update failed for %s: %s", job_id, exc)


@shared_task(
    name="api.tasks.blast.submit",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def submit(
    self,
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
) -> dict[str, Any]:
    """Submit a BLAST search via elastic-blast CLI in the terminal sidecar.

    Calls `elastic-blast submit` with the appropriate configuration, then
    monitors job progress via periodic status checks (driven by beat).
    """
    _update_state(job_id, "preparing")

    # Build elastic-blast config
    from api.services.blast_config import generate_config
    config_content = generate_config({
        "subscription_id": subscription_id,
        "resource_group": resource_group,
        "cluster_name": cluster_name,
        "storage_account": storage_account,
        "program": program,
        "db": database,
        "queries": query_file,
        **(options or {}),
    })

    _update_state(job_id, "submitting")

    try:
        from api.services.terminal_exec import run as terminal_run

        # Write config to a temp file in the terminal sidecar
        write_result = terminal_run(
            argv=["bash", "-c", f"cat > /tmp/elb-{job_id}.ini << 'ELBEOF'\n{config_content}\nELBEOF"],
            timeout_seconds=10,
        )

        # Run elastic-blast submit
        result = terminal_run(
            argv=["elastic-blast", "submit", "--cfg", f"/tmp/elb-{job_id}.ini"],
            timeout_seconds=600,  # 10 min max for submit
            env={"ELB_RESULTS": f"az://{storage_account}/results/{job_id}"},
        )

        if result.get("exit_code", 1) == 0:
            _update_state(job_id, "submitted", status="running")
            return {
                "job_id": job_id,
                "status": "submitted",
                "output": result.get("stdout", "")[:1000],
            }
        else:
            error = result.get("stderr", result.get("stdout", ""))[:500]
            _update_state(job_id, "submit_failed", status="failed", error_code=error)
            return {"job_id": job_id, "status": "failed", "error": error}

    except Exception as exc:
        error_msg = str(exc)[:500]
        _update_state(job_id, "submit_error", status="failed", error_code=error_msg)
        return {"job_id": job_id, "status": "failed", "error": error_msg}


@shared_task(name="api.tasks.blast.cancel", bind=True, max_retries=1)
def cancel(
    self,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
) -> dict[str, Any]:
    """Cancel a running BLAST job via elastic-blast delete."""
    _update_state(job_id, "cancelling")

    try:
        from api.services.terminal_exec import run as terminal_run

        result = terminal_run(
            argv=["elastic-blast", "delete", "--cfg", f"/tmp/elb-{job_id}.ini"],
            timeout_seconds=300,
            env={"ELB_RESULTS": f"az://{storage_account}/results/{job_id}"},
        )

        if result.get("exit_code", 1) == 0:
            _update_state(job_id, "cancelled", status="cancelled")
            return {"job_id": job_id, "status": "cancelled"}
        else:
            error = result.get("stderr", "")[:500]
            _update_state(job_id, "cancel_failed", status="failed", error_code=error)
            return {"job_id": job_id, "status": "failed", "error": error}

    except Exception as exc:
        error_msg = str(exc)[:500]
        _update_state(job_id, "cancel_error", status="failed", error_code=error_msg)
        return {"job_id": job_id, "status": "failed", "error": error_msg}


@shared_task(name="api.tasks.blast.check_status", bind=True)
def check_status(
    self,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
) -> dict[str, Any]:
    """Check the status of a running BLAST job.

    Scheduled by beat for periodic monitoring of active jobs. Updates the
    job state in Table Storage.
    """
    try:
        from api.services.terminal_exec import run as terminal_run

        result = terminal_run(
            argv=["elastic-blast", "status", "--cfg", f"/tmp/elb-{job_id}.ini"],
            timeout_seconds=60,
            env={"ELB_RESULTS": f"az://{storage_account}/results/{job_id}"},
        )

        stdout = result.get("stdout", "")
        exit_code = result.get("exit_code", 1)

        if exit_code == 0:
            # Parse status from output
            if "COMPLETED" in stdout.upper():
                _update_state(job_id, "completed", status="completed")
                return {"job_id": job_id, "status": "completed"}
            elif "FAILURE" in stdout.upper() or "FAILED" in stdout.upper():
                _update_state(job_id, "failed", status="failed")
                return {"job_id": job_id, "status": "failed"}
            else:
                _update_state(job_id, "running", status="running")
                return {"job_id": job_id, "status": "running", "output": stdout[:500]}

        return {"job_id": job_id, "status": "unknown", "exit_code": exit_code}

    except Exception as exc:
        LOGGER.warning("status check failed for %s: %s", job_id, exc)
        return {"job_id": job_id, "status": "unknown", "error": str(exc)[:500]}

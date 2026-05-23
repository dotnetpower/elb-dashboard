"""Shared helpers and constants for storage/warmup Celery tasks.

Responsibility: Provide stateless helpers and the canonical BLAST database catalog used by
    the warmup / update-check / reconcile tasks in this package.
Edit boundaries: Pure helpers + constants only. Long-running side effects belong in the
    sibling task modules (`warmup.py`, `update_check.py`, `reconcile.py`).
Key entry points: `BLAST_DATABASES`, `now_iso`, `publish_db_metadata_invalidate`,
    `update_state`, `record_task_progress`, `wait_for_warmup_jobs`.
Risky contracts: `update_state` and `record_task_progress` must stay best-effort (never
    raise) so task progress logging cannot crash the surrounding Celery task.
Validation: `uv run pytest -q api/tests/test_warmup_jobs.py api/tests/test_auto_warmup.py`.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)

# Standard BLAST databases available from NCBI
BLAST_DATABASES: dict[str, dict[str, str]] = {
    "nt": {"description": "Nucleotide collection (nt)", "size_hint": "~200 GB"},
    "nr": {"description": "Non-redundant protein sequences", "size_hint": "~150 GB"},
    "refseq_protein": {"description": "RefSeq protein", "size_hint": "~40 GB"},
    "refseq_rna": {"description": "RefSeq RNA", "size_hint": "~20 GB"},
    "swissprot": {"description": "Swiss-Prot", "size_hint": "~500 MB"},
    "pdbnt": {"description": "PDB nucleotide", "size_hint": "~500 MB"},
    "pdbaa": {"description": "PDB protein", "size_hint": "~200 MB"},
    "16S_ribosomal_RNA": {"description": "16S ribosomal RNA", "size_hint": "~50 MB"},
    "core_nt": {"description": "Core nucleotide collection", "size_hint": "~700 MB"},
    "ref_viruses_rep_genomes": {
        "description": "RefSeq representative virus genomes",
        "size_hint": "~2 GB",
    },
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def publish_db_metadata_invalidate(storage_account: str, database_name: str) -> None:
    """Best-effort cross-sidecar cache invalidation.

    The api sidecar's display-metadata cache is keyed by
    ``(storage_account, database_name)``. The warmup worker rewrites the
    underlying blob, so publish on the Redis channel that api sidecars
    subscribe to. Never raises — see ``publish_blast_db_metadata_invalidate``.
    """
    try:
        from api.services.blast.db_metadata import publish_blast_db_metadata_invalidate

        publish_blast_db_metadata_invalidate(storage_account, database_name)
    except Exception as exc:
        LOGGER.debug(
            "warmup_database invalidate publish skipped db=%s: %s",
            database_name,
            type(exc).__name__,
        )


def update_state(job_id: str, phase: str, status: str = "running", **extra: Any) -> None:
    """Best-effort state update."""
    try:
        from api.services.state.repository import JobStateRepository

        repo = JobStateRepository()
        payload = {"phase": phase, "status": status, **extra}
        error_code = str(extra.get("error_code") or "") or None
        try:
            repo.update(job_id, status=status, phase=phase, error_code=error_code)
        except KeyError:
            return
        repo.append_history(job_id, phase, payload)
    except Exception as exc:
        LOGGER.warning("state update failed for %s: %s", job_id, exc)


def record_task_progress(task: Any, phase: str, **meta: Any) -> None:
    try:
        task.update_state(state="PROGRESS", meta={"phase": phase, **meta})
    except Exception as exc:
        LOGGER.debug("task progress update failed: %s", type(exc).__name__)


def wait_for_warmup_jobs(
    task: Any,
    *,
    job_id: str,
    credential: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    database_name: str,
    expected_jobs: int,
    timeout_seconds: int,
    poll_seconds: int = 15,
) -> dict[str, Any]:
    from api.services.k8s.monitoring import k8s_warmup_status

    deadline = time.monotonic() + timeout_seconds
    last_database: dict[str, Any] = {}
    # State-write dedup + adaptive backoff:
    #   * Skip the Table write when the visible progress fields have not
    #     moved since the last tick - a long-running warmup (10-30 min)
    #     used to write 60-180 identical rows that Storage Tables had to
    #     persist, throttle, and ship back as audit history.
    #   * Stretch the poll interval after a few quiet ticks so the
    #     k8s_warmup_status request rate falls from 1/15s to 1/60s once
    #     it is clear nothing is changing — k8s_warmup_status itself fans
    #     out 6 GETs per call against the AKS API.
    last_progress_signature: tuple[int, int, int, int] | None = None
    quiet_ticks = 0
    while True:
        status = k8s_warmup_status(credential, subscription_id, resource_group, cluster_name)
        databases = status.get("databases", []) if isinstance(status, dict) else []
        last_database = next(
            (
                database
                for database in databases
                if isinstance(database, dict) and database.get("name") == database_name
            ),
            {},
        )
        nodes_ready = int(last_database.get("nodes_ready") or 0)
        nodes_failed = int(last_database.get("nodes_failed") or 0)
        nodes_active = int(last_database.get("nodes_active") or 0)
        total_jobs = int(last_database.get("total_jobs") or expected_jobs)
        progress = {
            "database": database_name,
            "nodes_ready": nodes_ready,
            "nodes_failed": nodes_failed,
            "nodes_active": nodes_active,
            "total_jobs": total_jobs,
            "expected_jobs": expected_jobs,
        }
        signature = (nodes_ready, nodes_failed, nodes_active, total_jobs)
        if signature != last_progress_signature:
            record_task_progress(task, "warming_nodes", **progress)
            update_state(job_id, "warming_nodes", status="running", **progress)
            last_progress_signature = signature
            quiet_ticks = 0
        else:
            quiet_ticks += 1

        if nodes_failed > 0:
            return {"status": "failed", **progress, "detail": last_database}
        if nodes_ready >= expected_jobs:
            return {"status": "completed", **progress, "detail": last_database}
        if time.monotonic() >= deadline:
            return {"status": "timeout", **progress, "detail": last_database}
        # Adaptive sleep: stay at the configured `poll_seconds` for the
        # first few ticks (so transitions get the low-latency UI update),
        # then stretch to 2x → 4x → 4x… so a long quiet wait stops
        # hammering the AKS API. Capped at 60 s.
        sleep_seconds = poll_seconds
        if quiet_ticks >= 6:
            sleep_seconds = min(60, poll_seconds * 4)
        elif quiet_ticks >= 3:
            sleep_seconds = min(60, poll_seconds * 2)
        time.sleep(sleep_seconds)

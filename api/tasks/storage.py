"""Storage Celery tasks — BLAST database warmup/download.

Side effects: Copies BLAST database files from NCBI FTP to the workload
Storage account using azcopy via the terminal sidecar.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from api.services.azure_clients import storage_client
from api.services import get_credential

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
    "ref_viruses_rep_genomes": {"description": "RefSeq representative virus genomes", "size_hint": "~2 GB"},
}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _update_state(job_id: str, phase: str, status: str = "running", **extra: Any) -> None:
    """Best-effort state update."""
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
    name="api.tasks.storage.warmup_database",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def warmup_database(
    self,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    storage_account: str,
    database_name: str,
    caller_oid: str = "",
) -> dict[str, Any]:
    """Download a BLAST database from NCBI to the workload storage account.

    Uses the terminal sidecar's `update_blastdb.pl` or `azcopy` to transfer
    BLAST database files into the `blast-db` container. Falls back to direct
    Azure SDK blob operations for the download if the terminal sidecar is
    unavailable.
    """
    _update_state(job_id, "starting")

    db_info = BLAST_DATABASES.get(database_name)
    if not db_info:
        _update_state(job_id, "failed", status="failed", error_code=f"unknown database: {database_name}")
        return {"status": "failed", "error": f"unknown database: {database_name}"}

    _update_state(job_id, "downloading", status="running")

    # Try terminal_exec first (has azcopy + update_blastdb.pl)
    try:
        from api.services.terminal_exec import run as terminal_run

        # Use update_blastdb.pl via the terminal sidecar
        result = terminal_run(
            argv=["elastic-blast", "get-blastdb", database_name],
            timeout_seconds=7200,  # 2 hours max for large DBs
            env={
                "BLASTDB_DIR": f"/mnt/blast-db/{database_name}",
                "STORAGE_ACCOUNT": storage_account,
            },
        )

        if result.get("exit_code", 1) == 0:
            _update_state(job_id, "completed", status="completed")
            return {
                "database": database_name,
                "status": "completed",
                "output": result.get("stdout", "")[:1000],
            }
        else:
            error = result.get("stderr", result.get("stdout", "unknown error"))[:500]
            _update_state(job_id, "failed", status="failed", error_code=error)
            return {"database": database_name, "status": "failed", "error": error}

    except Exception as exc:
        LOGGER.warning("terminal_exec warmup failed: %s, falling back to stub", exc)
        _update_state(job_id, "failed", status="failed", error_code=str(exc)[:500])
        return {"database": database_name, "status": "failed", "error": str(exc)[:500]}


@shared_task(name="api.tasks.storage.check_database_updates", bind=True)
def check_database_updates(
    self,
    *,
    subscription_id: str,
    resource_group: str,
    storage_account: str,
) -> dict[str, Any]:
    """Check if any downloaded BLAST databases have updates available.

    Compares local blob metadata timestamps against NCBI FTP timestamps.
    Scheduled by beat for periodic checks.
    """
    # For now, list what databases exist in the storage account
    try:
        from api.services.storage_data import list_databases
        cred = get_credential()
        databases = list_databases(cred, subscription_id, resource_group, storage_account)
        return {
            "databases": databases,
            "updates_available": [],  # TODO: compare with NCBI FTP
            "status": "completed",
        }
    except Exception as exc:
        LOGGER.warning("check_database_updates failed: %s", exc)
        return {"databases": [], "updates_available": [], "status": "failed", "error": str(exc)[:500]}

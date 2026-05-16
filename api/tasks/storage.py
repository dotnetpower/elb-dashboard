"""Storage Celery tasks — BLAST database warmup/download.

Side effects: Copies BLAST database files from NCBI FTP to the workload
Storage account using azcopy via the terminal sidecar.
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Any

from celery import shared_task

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
    "core_nt": {"description": "Core nucleotide collection", "size_hint": "~700 MB"},
    "ref_viruses_rep_genomes": {
        "description": "RefSeq representative virus genomes",
        "size_hint": "~2 GB",
    },
}


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now(UTC).isoformat(timespec="seconds")


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
        _update_state(
            job_id,
            "failed",
            status="failed",
            error_code=f"unknown database: {database_name}",
        )
        return {"status": "failed", "error": f"unknown database: {database_name}"}

    _update_state(job_id, "downloading", status="running")

    try:
        from api.services.storage_data import list_databases

        databases = list_databases(get_credential(), storage_account)
        match = next((db for db in databases if db.get("name") == database_name), None)
        if not match or int(match.get("file_count") or 0) == 0:
            error = f"database {database_name!r} is not prepared in workload storage"
            _update_state(job_id, "failed", status="failed", error_code=error)
            return {"database": database_name, "status": "failed", "error": error}

        # Auto-shard step — sharding is a hard prereq for warmup (the
        # daemonset vmtouches the per-shard layout files, not the raw
        # NCBI volumes). Doing it here means the user can click
        # "Warmup" on a freshly downloaded DB without having to remember
        # to click the per-chip shard button first.
        #
        # Inline (synchronous) is safe in a Celery worker: there is no
        # HTTP timeout, ensure_shard_sets is idempotent, and the work
        # for even the largest known DB completes in a few minutes.
        already_sharded = bool(match.get("sharded")) and bool(match.get("shard_sets"))
        sharding = "skipped" if already_sharded else "running"
        if not already_sharded:
            _update_state(job_id, "sharding", status="running")
            try:
                from datetime import datetime
                import json

                from api.services.db_sharding import (
                    DEFAULT_CONTAINER,
                    ensure_shard_sets,
                )
                from api.services.sanitise import sanitise
                from api.services.storage_data import _blob_service  # type: ignore[attr-defined]

                cred = get_credential()
                # Mark in-progress before the long call so the SPA's
                # chip strip can reflect the auto-shard step.
                svc = _blob_service(cred, storage_account)
                cc = svc.get_container_client(DEFAULT_CONTAINER)
                bc = cc.get_blob_client(f"{database_name}-metadata.json")
                pre: dict[str, Any] = {}
                try:
                    pre = json.loads(bc.download_blob().readall().decode("utf-8"))
                except Exception:
                    pre = {"db_name": database_name}
                pre["db_name"] = database_name
                pre["sharding_in_progress"] = True
                pre["sharding_started_at"] = datetime.now(UTC).isoformat()
                pre.pop("sharding_error", None)
                try:
                    bc.upload_blob(json.dumps(pre).encode("utf-8"), overwrite=True)
                except Exception as exc:
                    LOGGER.warning(
                        "warmup_database pre-state write failed db=%s: %s",
                        database_name, type(exc).__name__,
                    )

                summary = ensure_shard_sets(cred, storage_account, database_name)

                # Persist final state so the next /api/blast/databases
                # poll flips the chip to "sharded".
                final: dict[str, Any] = {}
                try:
                    final = json.loads(bc.download_blob().readall().decode("utf-8"))
                except Exception:
                    final = {"db_name": database_name}
                final["sharding_in_progress"] = False
                final.pop("sharding_error", None)
                final["sharded"] = bool(summary.get("shard_sets"))
                final["shard_sets"] = summary.get("shard_sets", [])
                final["sharded_at"] = datetime.now(UTC).isoformat()
                if summary.get("total_bytes"):
                    final.setdefault("total_bytes", summary["total_bytes"])
                for key in ("total_letters", "total_sequences", "bytes_to_cache", "bytes_total"):
                    if summary.get(key):
                        final.setdefault(key, summary[key])
                try:
                    bc.upload_blob(json.dumps(final).encode("utf-8"), overwrite=True)
                except Exception as exc:
                    LOGGER.warning(
                        "warmup_database final-state write failed db=%s: %s",
                        database_name, type(exc).__name__,
                    )
                sharding = "completed"
            except Exception as exc:
                LOGGER.warning(
                    "warmup_database auto-shard failed db=%s: %s",
                    database_name, type(exc).__name__,
                )
                # Best-effort error marker so the SPA shows a useful chip.
                try:
                    from api.services.db_sharding import DEFAULT_CONTAINER as _DC
                    from api.services.sanitise import sanitise as _sanitise
                    from api.services.storage_data import _blob_service as _bs
                    import json as _json
                    cred2 = get_credential()
                    svc2 = _bs(cred2, storage_account)
                    bc2 = svc2.get_container_client(_DC).get_blob_client(
                        f"{database_name}-metadata.json"
                    )
                    err_meta: dict[str, Any] = {}
                    try:
                        err_meta = _json.loads(
                            bc2.download_blob().readall().decode("utf-8")
                        )
                    except Exception:
                        err_meta = {"db_name": database_name}
                    err_meta["sharding_in_progress"] = False
                    err_meta["sharding_error"] = _sanitise(
                        f"{type(exc).__name__}: {exc}"
                    )[:300]
                    bc2.upload_blob(
                        _json.dumps(err_meta).encode("utf-8"), overwrite=True
                    )
                except Exception:
                    pass
                # Sharding is a prereq — don't claim success if it failed.
                err = sanitise(f"{type(exc).__name__}: {exc}")[:300]
                _update_state(
                    job_id, "failed", status="failed", error_code=err,
                )
                return {
                    "database": database_name,
                    "status": "failed",
                    "error": f"auto-shard failed: {err}",
                }

        _update_state(job_id, "completed", status="completed")
        return {
            "database": database_name,
            "status": "completed",
            "file_count": match.get("file_count", 0),
            "total_bytes": match.get("total_bytes", 0),
            "source_version": match.get("source_version", "unknown"),
            "sharding": sharding,
            "output": (
                "Database is prepared in workload storage."
                if sharding == "skipped"
                else "Database prepared and sharded for warmup."
            ),
        }

    except Exception as exc:
        LOGGER.warning("warmup verification failed: %s", exc)
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
        return {
            "databases": [],
            "updates_available": [],
            "status": "failed",
            "error": str(exc)[:500],
        }

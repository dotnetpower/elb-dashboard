"""Activities for BLAST job submission and lifecycle management.

Each activity is single-purpose, idempotent, and side-effect tagged.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from services import compute as compute_svc
from services import storage_data as storage_data_svc
from services.azure_clients import credential_for_caller
from services.blast_config import generate_config
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

_SAFE_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_job_id(job_id: str) -> str:
    """Validate job_id is safe for shell interpolation."""
    if not job_id or not _SAFE_JOB_ID.match(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    return job_id


def activity_upload_query(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: uploads FASTA query text to blob storage."""
    cred = credential_for_caller(payload.get("user_assertion"))
    account = payload["storage_account"]
    job_id = _validate_job_id(payload["job_id"])
    blob_path = f"{job_id}/input.fa"

    url = storage_data_svc.upload_query_text(
        cred,
        account,
        "queries",
        blob_path,
        payload["query_data"],
    )
    LOGGER.info("uploaded query to %s", url)
    return {"query_blob_url": url, "blob_path": blob_path}


def activity_generate_blast_config(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: generates INI config and uploads to storage."""
    cred = credential_for_caller(payload.get("user_assertion"))
    account = payload["storage_account"]
    job_id = _validate_job_id(payload["job_id"])

    config_text = generate_config(payload)
    blob_path = f"{job_id}/elastic-blast.ini"
    url = storage_data_svc.upload_query_text(
        cred,
        account,
        "queries",
        blob_path,
        config_text,
    )
    LOGGER.info("uploaded config to %s", url)
    return {"config_blob_url": url, "config_text": config_text}


def activity_run_elastic_blast_submit(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: runs elastic-blast submit on the Remote Terminal VM."""
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])
    account = payload["storage_account"]
    config_url = f"https://{account}.blob.core.windows.net/queries/{job_id}/elastic-blast.ini"

    script = (
        f"cd /home/azureuser/elastic-blast-azure && "
        f"source venv/bin/activate && "
        f"azcopy cp '{config_url}' /tmp/elb-{job_id}.ini && "
        f"python bin/elastic-blast submit --cfg /tmp/elb-{job_id}.ini 2>&1 | tail -50; "
        f"echo EXIT_CODE=$?"
    )
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        payload["terminal_resource_group"],
        payload["terminal_vm_name"],
        script,
    )
    sanitised = sanitise(output)[:2000]
    LOGGER.info("elastic-blast submit output: %s", sanitised[:500])

    exit_code = _parse_exit_code(output)
    return {
        "output": sanitised,
        "success": exit_code == 0,
        "job_id": job_id,
    }


def activity_check_blast_status(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: runs elastic-blast status on the Remote Terminal VM."""
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])

    script = (
        f"cd /home/azureuser/elastic-blast-azure && "
        f"source venv/bin/activate && "
        f"python bin/elastic-blast status --cfg /tmp/elb-{job_id}.ini --exit-code 2>&1 | tail -20; "
        f"echo EXIT_CODE=$?"
    )
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        payload["terminal_resource_group"],
        payload["terminal_vm_name"],
        script,
    )
    sanitised = sanitise(output)[:1000]

    exit_code = _parse_exit_code(output)

    status_map = {
        0: "completed",
        1: "failed",
        2: "creating",
        3: "submitting",
        4: "running",
        5: "deleting",
        6: "unknown",
    }
    status = status_map.get(exit_code, "unknown")
    return {"status": status, "exit_code": exit_code, "output": sanitised}


def _parse_exit_code(output: str) -> int:
    """Extract EXIT_CODE=N from shell output."""
    for line in output.strip().split("\n"):
        if line.startswith("EXIT_CODE="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                pass
    return 6  # UNKNOWN


def activity_run_elastic_blast_delete(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: runs elastic-blast delete on the Remote Terminal VM."""
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])

    script = (
        f"cd /home/azureuser/elastic-blast-azure && "
        f"source venv/bin/activate && "
        f"python bin/elastic-blast delete --cfg /tmp/elb-{job_id}.ini 2>&1 | tail -20"
    )
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        payload["terminal_resource_group"],
        payload["terminal_vm_name"],
        script,
    )
    return {"output": sanitise(output)[:1000], "success": True}


def activity_list_result_blobs(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: none (read-only). Lists result blobs for a job."""
    cred = credential_for_caller(payload.get("user_assertion"))
    blobs = storage_data_svc.list_result_blobs(
        cred,
        payload["storage_account"],
        "results",
        payload.get("prefix", ""),
    )
    return {"blobs": blobs}


def activity_list_databases(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: none (read-only). Lists available BLAST databases."""
    cred = credential_for_caller(payload.get("user_assertion"))
    dbs = storage_data_svc.list_databases(
        cred,
        payload["storage_account"],
        payload.get("container", "blast-db"),
    )
    return {"databases": dbs}

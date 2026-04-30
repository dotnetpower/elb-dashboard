"""BLAST job submission orchestrator.

Sequence:
  1. Upload query FASTA to storage
  2. Enable storage public access (required by elastic-blast)
  3. Wait for propagation (15 s)
  4. Generate INI config and upload to storage
  5. Run elastic-blast submit on Remote Terminal VM
  6. Poll status until completion or failure
  7. Disable storage public access (always, even on error)

Output: BlastJobSummary dict.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import azure.durable_functions as df

LOGGER = logging.getLogger(__name__)

STATUS_POLL_INTERVAL_SECONDS = 30
STATUS_POLL_MAX_ATTEMPTS = 720  # 720 * 30s = 6 hours max


def submit_blast_orchestrator(
    context: df.DurableOrchestrationContext,
) -> dict[str, Any]:
    request: dict[str, Any] = context.get_input() or {}
    job_id = request.get("job_id", context.instance_id)
    request["job_id"] = job_id

    storage_payload = {
        "subscription_id": request["subscription_id"],
        "resource_group": request["resource_group"],
        "account_name": request["storage_account"],
        "user_assertion": request.get("user_assertion"),
        "enabled": True,
    }
    disable_payload = {**storage_payload, "enabled": False}

    # 1. Upload query if inline text provided
    context.set_custom_status({"phase": "uploading", "job_id": job_id})
    if request.get("query_data"):
        upload_result = yield context.call_activity("upload_query_activity", request)
        request["query_blob_url"] = upload_result["query_blob_url"]

    # 2. Enable storage public access
    context.set_custom_status({"phase": "enabling_storage", "job_id": job_id})
    yield context.call_activity("set_storage_public_access_activity", storage_payload)

    # 3. Wait for propagation
    propagation = context.current_utc_datetime + timedelta(seconds=15)
    yield context.create_timer(propagation)

    # 4. Generate and upload config
    context.set_custom_status({"phase": "configuring", "job_id": job_id})
    account = request["storage_account"]
    request["results_url"] = f"https://{account}.blob.core.windows.net/results"
    db = request.get("db", "")
    if db and not db.startswith("http"):
        request["db"] = f"https://{account}.blob.core.windows.net/{db}"

    yield context.call_activity("generate_blast_config_activity", request)

    # 5. Submit
    context.set_custom_status({"phase": "submitting", "job_id": job_id})
    submit_result = yield context.call_activity("run_elastic_blast_submit_activity", request)
    if not submit_result.get("success"):
        # Disable storage before returning failure
        yield context.call_activity("set_storage_public_access_activity", disable_payload)
        return {
            "job_id": job_id,
            "status": "failed",
            "phase": "submitting",
            "error": submit_result.get("output", "submit failed"),
        }

    # 6. Poll status
    final_status = "unknown"
    for attempt in range(STATUS_POLL_MAX_ATTEMPTS):
        next_poll = context.current_utc_datetime + timedelta(seconds=STATUS_POLL_INTERVAL_SECONDS)
        yield context.create_timer(next_poll)

        try:
            check = yield context.call_activity("check_blast_status_activity", request)
            final_status = check.get("status", "unknown")
        except Exception as exc:
            LOGGER.warning("status check failed attempt=%d: %s", attempt + 1, exc)
            final_status = "unknown"

        context.set_custom_status(
            {
                "phase": "running",
                "job_id": job_id,
                "blast_status": final_status,
                "poll_attempt": attempt + 1,
            }
        )

        if final_status in ("completed", "failed"):
            break

    # 7. Always disable storage public access
    yield context.call_activity("set_storage_public_access_activity", disable_payload)

    return {
        "job_id": job_id,
        "status": final_status,
        "phase": "completed" if final_status == "completed" else final_status,
    }

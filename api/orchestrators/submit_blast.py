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
RESULTS_VERIFY_INTERVAL_SECONDS = 30
RESULTS_VERIFY_MAX_ATTEMPTS = 10  # 10 * 30s = 5 min max waiting for .out files


def submit_blast_orchestrator(
    context: df.DurableOrchestrationContext,
) -> dict[str, Any]:
    request: dict[str, Any] = context.get_input() or {}
    job_id = request.get("job_id", context.instance_id)
    request["job_id"] = job_id
    entity_id = df.EntityId("job_registry_entity", "default")

    storage_payload = {
        "subscription_id": request["subscription_id"],
        "resource_group": request["resource_group"],
        "account_name": request["storage_account"],
        "user_assertion": request.get("user_assertion"),
        "enabled": True,
    }
    disable_payload = {**storage_payload, "enabled": False}

    try:
        # Accumulate step results for rich frontend display
        steps: dict[str, Any] = {}

        # 0. Ensure Remote Terminal VM is running
        context.set_custom_status({"phase": "checking_vm", "job_id": job_id, "steps": steps})
        vm_payload = {
            "subscription_id": request["subscription_id"],
            "resource_group": request.get("terminal_resource_group", "rg-elb-terminal"),
            "vm_name": request.get("terminal_vm_name", "vm-elb-terminal"),
            "user_assertion": request.get("user_assertion"),
        }
        vm_status = yield context.call_activity("ensure_vm_running_activity", vm_payload)
        steps["checking_vm"] = {"power_state": vm_status.get("power_state"), "started": vm_status.get("started")}
        if vm_status.get("started"):
            boot_wait = context.current_utc_datetime + timedelta(seconds=30)
            yield context.create_timer(boot_wait)

        # 1. Enable storage access
        context.set_custom_status({"phase": "enabling_storage", "job_id": job_id, "steps": steps})
        yield context.call_activity("set_storage_public_access_activity", storage_payload)
        steps["enabling_storage"] = {"done": True}

        # 2. Wait for propagation
        propagation = context.current_utc_datetime + timedelta(seconds=15)
        yield context.create_timer(propagation)

        # 3. Upload query
        context.set_custom_status({"phase": "uploading", "job_id": job_id, "steps": steps})
        if request.get("query_data"):
            upload_result = yield context.call_activity("upload_query_activity", request)
            request["query_blob_url"] = upload_result["query_blob_url"]
            steps["uploading"] = {"blob_url": upload_result.get("query_blob_url"), "blob_path": upload_result.get("blob_path")}
        else:
            steps["uploading"] = {"skipped": True}

        # 4. Generate and upload config
        context.set_custom_status({"phase": "configuring", "job_id": job_id, "steps": steps})
        account = request["storage_account"]
        request["results_url"] = f"https://{account}.blob.core.windows.net/results/{job_id}"
        db = request.get("db", "")
        if db and not db.startswith("http"):
            request["db"] = f"https://{account}.blob.core.windows.net/{db}"

        config_result = yield context.call_activity("generate_blast_config_activity", request)
        request["config_text"] = config_result.get("config_text", "")
        steps["configuring"] = {"config_url": config_result.get("config_blob_url")}

        # 5. Warmup / Prepare (optional — enabled by default for DB sharding)
        enable_warmup = request.get("enable_warmup", True)
        if enable_warmup:
            context.set_custom_status({"phase": "warming_up", "job_id": job_id, "steps": steps})
            prepare_result = yield context.call_activity("run_elastic_blast_prepare_activity", request)
            prepare_output = prepare_result.get("output", "")
            steps["warming_up"] = {
                "success": prepare_result.get("success"),
                "output": prepare_output[:4000],
            }
            if not prepare_result.get("success"):
                context.set_custom_status({"phase": "warmup_failed", "job_id": job_id, "steps": steps})
                yield context.call_activity("set_storage_public_access_activity", disable_payload)
                context.signal_entity(entity_id, "update_job",
                    {"job_id": job_id, "status": "failed", "phase": "warmup_failed"})
                return {"job_id": job_id, "status": "failed", "phase": "warmup_failed",
                        "error": prepare_output[:4000], "steps": steps}
            # After prepare, set reuse=true so submit uses the warm cluster
            request["reuse"] = True
            # Regenerate config with reuse=true
            config_result = yield context.call_activity("generate_blast_config_activity", request)
            request["config_text"] = config_result.get("config_text", "")

        # 6. Submit
        context.set_custom_status({"phase": "submitting", "job_id": job_id, "steps": steps})
        submit_result = yield context.call_activity("run_elastic_blast_submit_activity", request)
        submit_output = submit_result.get("output", "")
        steps["submitting"] = {"success": submit_result.get("success"), "output": submit_output[:4000]}
        if not submit_result.get("success"):
            context.set_custom_status({"phase": "submit_failed", "job_id": job_id, "steps": steps})
            yield context.call_activity("set_storage_public_access_activity", disable_payload)
            context.signal_entity(entity_id, "update_job",
                {"job_id": job_id, "status": "failed", "phase": "submit_failed"})
            return {"job_id": job_id, "status": "failed", "phase": "submit_failed",
                    "error": submit_output[:4000], "steps": steps}

        # 6. Poll status — minimum 2 polls before accepting "completed"
        # elastic-blast can report EXIT_CODE=0 prematurely if auth fails or
        # the AKS cluster hasn't been created yet.
        final_status = "unknown"
        last_check_output = ""
        MIN_POLLS_BEFORE_COMPLETE = 3  # At least 90 seconds before trusting "completed"
        for attempt in range(STATUS_POLL_MAX_ATTEMPTS):
            next_poll = context.current_utc_datetime + timedelta(seconds=STATUS_POLL_INTERVAL_SECONDS)
            yield context.create_timer(next_poll)

            try:
                check = yield context.call_activity("check_blast_status_activity", request)
                final_status = check.get("status", "unknown")
                last_check_output = check.get("output", "")
            except Exception as exc:
                LOGGER.warning("status check failed attempt=%d: %s", attempt + 1, exc)
                final_status = "unknown"

            context.set_custom_status({
                "phase": "running", "job_id": job_id,
                "blast_status": final_status, "poll_attempt": attempt + 1,
                "steps": steps,
            })

            if final_status in ("completed", "failed"):
                # Don't trust early "completed" — elastic-blast can return
                # EXIT_CODE=0 if az login fails or cluster isn't ready yet
                if attempt + 1 >= MIN_POLLS_BEFORE_COMPLETE:
                    break
                elif final_status == "completed":
                    LOGGER.warning("Ignoring early completed at poll %d (min=%d)", attempt + 1, MIN_POLLS_BEFORE_COMPLETE)
                    final_status = "running"  # Reset and keep polling
                else:
                    break  # Failed is trustworthy immediately

        steps["running"] = {"final_status": final_status, "polls": attempt + 1, "last_output": last_check_output[:4000]}

        # 7. Export results — verify .out files actually exist in blob
        if final_status == "completed":
            context.set_custom_status({"phase": "exporting_results", "job_id": job_id, "steps": steps})

            # 7a. Wait for results-export pod to finish writing
            # elastic-blast's "completed" status means BLAST computation is done,
            # but the results-export pod may still be uploading .out files to blob.
            result_blobs_payload = {
                "subscription_id": request["subscription_id"],
                "storage_account": account,
                "prefix": f"{job_id}/",
                "user_assertion": request.get("user_assertion"),
            }
            has_output_files = False
            for verify_attempt in range(RESULTS_VERIFY_MAX_ATTEMPTS):
                verify_wait = context.current_utc_datetime + timedelta(
                    seconds=RESULTS_VERIFY_INTERVAL_SECONDS,
                )
                yield context.create_timer(verify_wait)

                try:
                    blob_list = yield context.call_activity(
                        "list_result_blobs_activity", result_blobs_payload,
                    )
                    blobs = blob_list.get("blobs", [])
                    out_files = [b for b in blobs if b.get("name", "").endswith(".out")]
                    context.set_custom_status({
                        "phase": "exporting_results", "job_id": job_id,
                        "verify_attempt": verify_attempt + 1,
                        "blob_count": len(blobs), "out_file_count": len(out_files),
                        "steps": steps,
                    })
                    if out_files:
                        has_output_files = True
                        LOGGER.info(
                            "Found %d .out files after %d verify attempts",
                            len(out_files), verify_attempt + 1,
                        )
                        break
                except Exception as exc:
                    LOGGER.warning(
                        "result blob verification attempt %d failed: %s",
                        verify_attempt + 1, exc,
                    )

            steps["result_verification"] = {
                "has_output_files": has_output_files,
                "verify_attempts": verify_attempt + 1,
            }

            # 7b. Run export activity (captures logs/status as artifacts)
            try:
                export_result = yield context.call_activity("export_blast_results_activity", request)
                steps["exporting_results"] = {
                    "success": export_result.get("success"),
                    "auth_failed": export_result.get("auth_failed"),
                    "output": export_result.get("output", "")[:4000],
                    "has_output_files": has_output_files,
                }
                LOGGER.info("Results export: %s", export_result)
            except Exception as exc:
                steps["exporting_results"] = {"success": False, "error": str(exc)[:300]}
                LOGGER.warning("Results export failed (non-fatal): %s", exc)

        # 8. Always disable storage public access
        try:
            yield context.call_activity("set_storage_public_access_activity", disable_payload)
        except Exception as exc:
            LOGGER.warning("Failed to disable storage: %s", exc)

        # Signal entity with final status
        result_phase = "completed" if final_status == "completed" else final_status
        context.set_custom_status({"phase": result_phase, "job_id": job_id, "steps": steps})
        context.signal_entity(entity_id, "update_job",
            {"job_id": job_id, "status": final_status, "phase": result_phase})

        return {"job_id": job_id, "status": final_status, "phase": result_phase, "steps": steps}

    except Exception as exc:
        # CRITICAL: Always update entity on any failure so UI doesn't show stale status
        LOGGER.error("submit_blast_orchestrator failed for job=%s: %s", job_id, exc)
        error_msg = str(exc)[:500]
        context.signal_entity(entity_id, "update_job",
            {"job_id": job_id, "status": "failed", "phase": "error", "error": error_msg})
        # Try to disable storage access on failure
        try:
            yield context.call_activity("set_storage_public_access_activity", disable_payload)
        except Exception:
            pass
        raise  # Re-raise so the orchestrator status shows Failed

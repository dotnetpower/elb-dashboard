"""Standalone DB warmup orchestrator.

Loads a BLAST database onto AKS cluster nodes without submitting a BLAST job.
Reuses the existing elastic-blast prepare activities.

Sequence:
  1. Ensure VM is running
  2. Enable storage public access
  3. Wait for propagation
  4. Generate config for prepare-only (dummy query/results)
  5. Assign AKS RBAC roles (idempotent)
  6. Run elastic-blast prepare (fire-and-poll)
  7. Disable storage public access

Input: {
  subscription_id, resource_group, storage_account, storage_resource_group,
  region, db, program, aks_cluster_name, machine_type, num_nodes,
  acr_resource_group, acr_name,
  terminal_resource_group, terminal_vm_name,
  user_assertion,
}

Output: { status: "succeeded" | "failed", db, cluster, ... }
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import azure.durable_functions as df

LOGGER = logging.getLogger(__name__)

WARMUP_POLL_INTERVAL_SECONDS = 30
WARMUP_POLL_MAX_ATTEMPTS = 120  # 120 * 30s = 60 min ceiling
STORAGE_PROPAGATION_SECONDS = 10


def warmup_db_orchestrator(
    context: df.DurableOrchestrationContext,
) -> dict[str, Any]:
    request: dict[str, Any] = context.get_input() or {}
    instance_id = context.instance_id
    db_name = request.get("db_display_name", request.get("db", "unknown"))

    storage_payload = {
        "subscription_id": request["subscription_id"],
        "resource_group": request.get("storage_resource_group", request["resource_group"]),
        "account_name": request["storage_account"],
        "user_assertion": request.get("user_assertion"),
        "enabled": True,
    }
    disable_payload = {**storage_payload, "enabled": False}

    try:
        steps: dict[str, Any] = {}

        def _ts() -> str:
            return context.current_utc_datetime.isoformat()

        # 0. Ensure VM is running
        steps["checking_vm"] = {"started_at": _ts()}
        context.set_custom_status({"phase": "checking_vm", "db": db_name, "steps": steps})
        vm_payload = {
            "subscription_id": request["subscription_id"],
            "resource_group": request.get("terminal_resource_group", "rg-elb-terminal"),
            "vm_name": request.get("terminal_vm_name", "vm-elb-terminal"),
            "user_assertion": request.get("user_assertion"),
        }
        vm_status = yield context.call_activity("ensure_vm_running_activity", vm_payload)
        steps["checking_vm"].update({"completed_at": _ts(), "started": vm_status.get("started")})
        if vm_status.get("started"):
            yield context.create_timer(context.current_utc_datetime + timedelta(seconds=30))

        # 1. Enable storage access
        steps["enabling_storage"] = {"started_at": _ts()}
        context.set_custom_status({"phase": "enabling_storage", "db": db_name, "steps": steps})
        yield context.call_activity("set_storage_public_access_activity", storage_payload)
        steps["enabling_storage"].update({"done": True, "completed_at": _ts()})
        yield context.create_timer(context.current_utc_datetime + timedelta(seconds=STORAGE_PROPAGATION_SECONDS))

        # 2. Generate config for prepare-only
        steps["configuring"] = {"started_at": _ts()}
        context.set_custom_status({"phase": "configuring", "db": db_name, "steps": steps})

        # Build a minimal job_id for the prepare run
        job_id = f"warmup-{instance_id[:8]}"
        request["job_id"] = job_id
        request["enable_warmup"] = True
        # CRITICAL: reuse=True — we are warming an EXISTING cluster, not creating a new one.
        # reuse=False would cause elastic-blast to try creating a new AKS cluster.
        request["reuse"] = True
        # Use cluster's actual node config if available (passed from frontend)
        if not request.get("machine_type"):
            request["machine_type"] = "Standard_E32s_v5"
        if not request.get("num_nodes"):
            request["num_nodes"] = 3
        request["reuse"] = True
        # Dummy query/results — prepare only downloads DB, doesn't need real ones
        if not request.get("query_blob_url"):
            request["query_blob_url"] = f"https://{request['storage_account']}.blob.core.windows.net/queries/dummy.fa"
        if not request.get("results_url"):
            request["results_url"] = f"https://{request['storage_account']}.blob.core.windows.net/results/{job_id}"

        # Set DB URL if not already a full URL
        db_raw = request.get("db", "")
        if db_raw and not db_raw.startswith("http"):
            request["db"] = f"https://{request['storage_account']}.blob.core.windows.net/{db_raw}"

        config_result = yield context.call_activity("generate_blast_config_activity", request)
        request["config_text"] = config_result.get("config_text", "")
        steps["configuring"].update({"completed_at": _ts()})

        # 3. Assign RBAC roles (idempotent, non-fatal)
        aks_cluster = request.get("aks_cluster_name", "")
        if aks_cluster:
            roles_payload = {
                "subscription_id": request["subscription_id"],
                "resource_group": request["resource_group"],
                "cluster_name": aks_cluster,
                "acr_resource_group": request.get("acr_resource_group", ""),
                "acr_name": request.get("acr_name", ""),
                "storage_resource_group": request.get("storage_resource_group", request["resource_group"]),
                "storage_account": request["storage_account"],
                "user_assertion": request.get("user_assertion"),
            }
            try:
                yield context.call_activity("assign_aks_roles_activity", roles_payload)
            except Exception as exc:
                LOGGER.warning("Warmup role assignment failed (non-fatal): %s", exc)
                steps["roles"] = {"error": str(exc)[:200]}

        # 4. Run elastic-blast prepare
        steps["warming_up"] = {"started_at": _ts()}
        context.set_custom_status({"phase": "warming_up", "db": db_name, "steps": steps})
        yield context.call_activity("run_elastic_blast_prepare_activity", request)

        # 5. Poll until complete
        prepare_status = "running"
        prepare_output = ""
        for attempt in range(WARMUP_POLL_MAX_ATTEMPTS):
            yield context.create_timer(
                context.current_utc_datetime + timedelta(seconds=WARMUP_POLL_INTERVAL_SECONDS)
            )
            check = yield context.call_activity("check_elastic_blast_prepare_activity", request)
            prepare_status = check.get("status", "running")
            prepare_output = check.get("output", "")
            steps["warming_up"]["poll_attempt"] = attempt + 1
            steps["warming_up"]["output"] = prepare_output[:4000]
            context.set_custom_status({"phase": "warming_up", "db": db_name, "steps": steps})
            if prepare_status in ("succeeded", "failed", "lost"):
                break

        steps["warming_up"].update({
            "success": prepare_status == "succeeded",
            "status": prepare_status,
            "completed_at": _ts(),
        })

        # 6. Disable storage access (always)
        yield context.call_activity("set_storage_public_access_activity", disable_payload)

        if prepare_status == "succeeded":
            context.set_custom_status({"phase": "completed", "db": db_name, "steps": steps})
            return {
                "status": "succeeded",
                "db": db_name,
                "cluster": aks_cluster,
                "steps": steps,
            }
        else:
            context.set_custom_status({"phase": "failed", "db": db_name, "steps": steps})
            return {
                "status": "failed",
                "db": db_name,
                "cluster": aks_cluster,
                "error": prepare_output[:4000],
                "steps": steps,
            }

    except Exception as exc:
        LOGGER.error("warmup_db_orchestrator failed: %s", exc)
        try:
            yield context.call_activity("set_storage_public_access_activity", disable_payload)
        except Exception:
            pass
        return {
            "status": "failed",
            "db": db_name,
            "error": str(exc)[:2000],
        }

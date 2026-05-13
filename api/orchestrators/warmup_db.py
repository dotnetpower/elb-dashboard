"""Standalone DB warmup orchestrator.

Loads a BLAST database onto AKS cluster nodes via direct K8s API.
Inspired by elastic-blast-azure benchmark v3 PreloadedStrategy — deploys
K8s Jobs that use azcopy to download DB from blob to each node's local SSD.

No VM SSH dependency. Uses K8s API directly for both apply and polling (~1-3s
per call vs 30-60s for VM Run Command).

Sequence:
  1. Enable storage public access
  2. Wait for propagation
  3. Apply K8s warmup Jobs (one per node, azcopy blob→SSD)
  4. Poll K8s Jobs until all nodes complete
  5. Disable storage public access

Input: {
  subscription_id, resource_group, storage_account, storage_resource_group,
  region, db, db_display_name, program, aks_cluster_name,
  machine_type, num_nodes, acr_resource_group, acr_name,
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

WARMUP_POLL_INTERVAL_SECONDS = 15  # K8s API is fast — poll every 15s
WARMUP_POLL_MAX_ATTEMPTS = 480     # 480 * 15s = 120 min ceiling (core_nt is ~283GB)
STORAGE_PROPAGATION_SECONDS = 10
RBAC_PROPAGATION_SECONDS = 60      # AKS kubelet RBAC propagation grace period


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

        # 0. Enable storage access
        steps["enabling_storage"] = {"started_at": _ts()}
        context.set_custom_status({"phase": "enabling_storage", "db": db_name, "steps": steps})
        yield context.call_activity("set_storage_public_access_activity", storage_payload)
        steps["enabling_storage"].update({"done": True, "completed_at": _ts()})
        yield context.create_timer(context.current_utc_datetime + timedelta(seconds=STORAGE_PROPAGATION_SECONDS))

        # 1. Build DB URL
        steps["configuring"] = {"started_at": _ts()}
        context.set_custom_status({"phase": "configuring", "db": db_name, "steps": steps})

        db_raw = request.get("db", "")
        if db_raw and not db_raw.startswith("http"):
            db_url = f"https://{request['storage_account']}.blob.core.windows.net/{db_raw}"
        else:
            db_url = db_raw

        aks_cluster = request.get("aks_cluster_name", "")
        num_nodes = int(request.get("num_nodes") or 3)
        steps["configuring"].update({"completed_at": _ts(), "db_url": db_url, "num_nodes": num_nodes})

        # 2. Assign RBAC roles (idempotent, non-fatal)
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
                roles_res = yield context.call_activity("assign_aks_roles_activity", roles_payload)
                steps["roles"] = {"roles_assigned": roles_res.get("roles_assigned", [])}
            except Exception as exc:
                LOGGER.warning("Warmup role assignment failed (non-fatal): %s", exc)
                steps["roles"] = {"error": str(exc)[:200]}

            # Brief pause to let kubelet RBAC (AcrPull, Storage Blob Data
            # Contributor) propagate before the DaemonSet pods try to pull
            # the warmup image / call azcopy. The init container itself also
            # retries azcopy 6× with 30 s sleeps, so this is belt-and-braces.
            yield context.create_timer(
                context.current_utc_datetime + timedelta(seconds=RBAC_PROPAGATION_SECONDS)
            )

        # 3. Apply K8s warmup Jobs (one per node, azcopy blob→local SSD)
        steps["warming_up"] = {"started_at": _ts()}
        context.set_custom_status({"phase": "warming_up", "db": db_name, "steps": steps})

        k8s_payload = {
            "subscription_id": request["subscription_id"],
            "resource_group": request["resource_group"],
            "cluster_name": aks_cluster,
            "db_url": db_url,
            "db_name": db_name,
            "num_nodes": num_nodes,
            "acr_name": request.get("acr_name", ""),
            "acr_resource_group": request.get("acr_resource_group", ""),
            "user_assertion": request.get("user_assertion"),
        }
        apply_result = yield context.call_activity("k8s_warmup_db_activity", k8s_payload)
        steps["warming_up"]["jobs_applied"] = len(apply_result.get("job_names", []))

        # 4. Poll K8s Jobs until all nodes complete
        check_payload = {
            "subscription_id": request["subscription_id"],
            "resource_group": request["resource_group"],
            "cluster_name": aks_cluster,
            "db_name": db_name,
            "user_assertion": request.get("user_assertion"),
        }
        warmup_status = "running"
        for attempt in range(WARMUP_POLL_MAX_ATTEMPTS):
            yield context.create_timer(
                context.current_utc_datetime + timedelta(seconds=WARMUP_POLL_INTERVAL_SECONDS)
            )
            check = yield context.call_activity("k8s_check_warmup_db_activity", check_payload)
            warmup_status = check.get("status", "running")
            steps["warming_up"]["poll_attempt"] = attempt + 1
            steps["warming_up"]["ready"] = check.get("ready", 0)
            steps["warming_up"]["total"] = check.get("total", num_nodes)
            context.set_custom_status({"phase": "warming_up", "db": db_name, "steps": steps})
            if warmup_status in ("succeeded", "failed"):
                break

        steps["warming_up"].update({
            "success": warmup_status == "succeeded",
            "status": warmup_status,
            "completed_at": _ts(),
        })

        # 5. Disable storage access (always)
        yield context.call_activity("set_storage_public_access_activity", disable_payload)

        if warmup_status == "succeeded":
            context.set_custom_status({"phase": "completed", "db": db_name, "steps": steps})
            return {
                "status": "succeeded",
                "db": db_name,
                "cluster": aks_cluster,
                "steps": steps,
            }
        else:
            if warmup_status == "failed":
                # Surface the actual init container error from k8s_check_warmup_db_activity
                logs = check.get("logs", "")
                init_failed = check.get("init_failed", 0)
                restart_max = check.get("restart_max", 0)
                failed_pod = check.get("failed_pod", "")
                if logs:
                    error_msg = (
                        f"Warmup init container failed on {init_failed} pod(s) "
                        f"after {restart_max} retries (pod: {failed_pod}). "
                        f"Last logs:\n{logs}"
                    )[:4000]
                else:
                    error_msg = (
                        f"Warmup init container failed on {init_failed} pod(s) "
                        f"after {restart_max} retries (pod: {failed_pod}). "
                        f"No logs captured."
                    )[:2000]
            else:
                error_msg = f"Warmup polling timed out after {WARMUP_POLL_MAX_ATTEMPTS} attempts"
            steps["warming_up"]["error"] = error_msg
            context.set_custom_status({"phase": "failed", "db": db_name, "steps": steps})
            return {
                "status": "failed",
                "db": db_name,
                "cluster": aks_cluster,
                "error": error_msg,
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

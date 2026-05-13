"""ElasticBLAST HTTP routes.

All ``/api/blast/*`` handlers live here so the dynamic job-result route
family is registered from one Blueprint. Durable orchestrators, activities,
and entities remain in ``function_app.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

import azure.durable_functions as df
import azure.functions as func
import requests as _requests
from pydantic import ValidationError

from _http_utils import (
    _RE_BLOB_NAME,
    _RE_DB_NAME,
    _RE_INSTANCE_ID,
    _RE_STORAGE_ACCOUNT,
    _error_response,
    _json_response,
    _require_query,
    _validate_name,
    _validate_rg,
    _validate_sub,
)
from auth.token import AuthError, validate_bearer_token
from models.blast import BlastSubmitRequest
from services import compute as compute_svc
from services import keyvault as kv_svc
from services import storage_data as storage_data_svc
from services.azure_clients import credential_for_caller
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)


def _toggle_public_access(
    cred: Any,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    *,
    enabled: bool,
) -> None:
    """Enable or disable publicNetworkAccess on a storage account."""
    import time as _time

    from azure.mgmt.storage import StorageManagementClient

    mgmt = StorageManagementClient(cred, subscription_id)
    target = "Enabled" if enabled else "Disabled"
    mgmt.storage_accounts.update(
        resource_group,
        account_name,
        {"properties": {"public_network_access": target}},
    )
    LOGGER.info("Set publicNetworkAccess=%s on %s", target, account_name)
    if enabled:
        _time.sleep(10)  # wait for propagation


bp = df.Blueprint()


# ---------------------------------------------------------------------------
# BLAST — pre-flight readiness check
# ---------------------------------------------------------------------------
@bp.route(route="blast/pre-flight", methods=["POST"])
def blast_pre_flight(req: func.HttpRequest) -> func.HttpResponse:
    """Validate all preconditions before BLAST submission.

    Checks: ACR images built, BLAST DB exists in storage, AKS cluster running,
    Terminal VM running, storage containers exist. Returns a checklist with
    pass/fail for each item and actionable fix suggestions.
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return _error_response(400, "invalid JSON")

    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    acr_rg = body.get("acr_resource_group", "")
    acr_name = body.get("acr_name", "")
    storage_account = body.get("storage_account", "")
    cluster_name = body.get("aks_cluster_name", "")
    terminal_rg = body.get("terminal_resource_group", "rg-elb-terminal")
    terminal_vm = body.get("terminal_vm_name", "vm-elb-terminal")
    db_path = body.get("db", "")
    query_data = body.get("query_data", "")

    if not sub:
        return _error_response(400, "subscription_id required")

    cred = credential_for_caller(identity.raw_token)
    checks: list[dict[str, Any]] = []

    # 1. ACR images built
    if acr_rg and acr_name:
        try:
            from azure.mgmt.containerregistry import ContainerRegistryManagementClient

            acr_client = ContainerRegistryManagementClient(cred, sub)
            repos = acr_client.repositories.list(acr_rg, acr_name)
            repo_names = {r for r in repos} if repos else set()
            from services.image_tags import IMAGE_TAGS

            missing_images = []
            for image, tag in IMAGE_TAGS.items():
                if image not in repo_names:
                    missing_images.append(f"{image}:{tag}")
            if missing_images:
                checks.append(
                    {
                        "id": "acr_images",
                        "status": "fail",
                        "title": "ACR images not built",
                        "detail": f"Missing: {', '.join(missing_images)}",
                        "action": "Build images from the Dashboard ACR card",
                        "severity": "critical",
                    }
                )
            else:
                checks.append(
                    {"id": "acr_images", "status": "pass", "title": "ACR images available"}
                )
        except Exception as exc:
            checks.append(
                {
                    "id": "acr_images",
                    "status": "warn",
                    "title": "Could not check ACR images",
                    "detail": sanitise(str(exc))[:200],
                    "severity": "medium",
                }
            )
    else:
        checks.append(
            {
                "id": "acr_images",
                "status": "skip",
                "title": "ACR not configured",
                "detail": "Configure ACR in Dashboard settings",
                "severity": "high",
            }
        )

    # 2. BLAST database exists in storage
    if storage_account and db_path:
        try:
            # Extract db base name from path like "blast-db/core_nt/core_nt"
            db_parts = db_path.split("/")
            db_name = db_parts[-1] if db_parts else db_path
            container = db_parts[0] if len(db_parts) > 1 else "blast-db"

            dbs = storage_data_svc.list_databases(cred, storage_account, container)
            db_names = {d["name"] for d in dbs}
            if db_name in db_names:
                db_info = next((d for d in dbs if d["name"] == db_name), None)
                size_gb = (db_info["total_bytes"] / (1024**3)) if db_info else 0
                checks.append(
                    {
                        "id": "blast_db",
                        "status": "pass",
                        "title": f"Database '{db_name}' available",
                        "detail": f"{db_info['file_count']} files, {size_gb:.1f} GB"
                        if db_info
                        else "",
                    }
                )
            else:
                # Suggest downloading
                available = ", ".join(sorted(db_names)[:5])
                checks.append(
                    {
                        "id": "blast_db",
                        "status": "fail",
                        "title": f"Database '{db_name}' not found in storage",
                        "detail": f"Available: {available}"
                        if available
                        else "No databases found. Download one first.",
                        "action": (
                            f"Download '{db_name}' from NCBI using the Dashboard storage card"
                        ),
                        "action_type": "download_db",
                        "action_params": {"db_name": db_name},
                        "severity": "critical",
                        "suggested_dbs": [
                            "core_nt",
                            "16S_ribosomal_RNA",
                            "nt",
                            "nr",
                            "swissprot",
                        ],
                    }
                )
        except Exception as exc:
            msg = str(exc)
            if "AuthorizationFailure" in msg or "public" in msg.lower():
                checks.append(
                    {
                        "id": "blast_db",
                        "status": "warn",
                        "title": "Storage not accessible",
                        "detail": (
                            "Public network access may be disabled. Enable temporarily to check."
                        ),
                        "severity": "medium",
                    }
                )
            else:
                checks.append(
                    {
                        "id": "blast_db",
                        "status": "warn",
                        "title": "Could not verify database",
                        "detail": sanitise(msg)[:200],
                        "severity": "medium",
                    }
                )
    elif not db_path:
        checks.append(
            {
                "id": "blast_db",
                "status": "fail",
                "title": "No database selected",
                "severity": "critical",
            }
        )

    # 3. AKS cluster running
    if cluster_name and rg:
        try:
            from azure.mgmt.containerservice import ContainerServiceClient

            aks_client = ContainerServiceClient(cred, sub)
            cluster = aks_client.managed_clusters.get(rg, cluster_name)
            power_state = "Unknown"
            if cluster.power_state:
                power_state = cluster.power_state.code or "Unknown"
            prov_state = cluster.provisioning_state or "Unknown"
            if power_state == "Running" and prov_state == "Succeeded":
                checks.append(
                    {
                        "id": "aks_cluster",
                        "status": "pass",
                        "title": f"AKS cluster '{cluster_name}' running",
                    }
                )
            else:
                checks.append(
                    {
                        "id": "aks_cluster",
                        "status": "fail",
                        "title": (
                            f"AKS cluster not ready (power={power_state}, "
                            f"provisioning={prov_state})"
                        ),
                        "action": "Start cluster from the Dashboard",
                        "severity": "critical",
                    }
                )
        except Exception as exc:
            checks.append(
                {
                    "id": "aks_cluster",
                    "status": "fail",
                    "title": "AKS cluster not found or inaccessible",
                    "detail": sanitise(str(exc))[:200],
                    "action": "Create a cluster from the Dashboard",
                    "severity": "critical",
                }
            )
    else:
        checks.append(
            {
                "id": "aks_cluster",
                "status": "fail",
                "title": "No AKS cluster selected",
                "action": "Create or select a cluster",
                "severity": "critical",
            }
        )

    # 4. Terminal VM running
    if terminal_vm:
        try:
            from services.compute import get_vm_status

            vm_status = get_vm_status(cred, sub, terminal_rg, terminal_vm)
            power = vm_status.get("power_state", "unknown")
            if power == "running":
                checks.append(
                    {"id": "terminal_vm", "status": "pass", "title": "Terminal VM running"}
                )
            else:
                checks.append(
                    {
                        "id": "terminal_vm",
                        "status": "fail",
                        "title": f"Terminal VM not running (state: {power})",
                        "action": "Start VM from the Terminal page",
                        "severity": "critical",
                    }
                )
        except Exception:
            checks.append(
                {
                    "id": "terminal_vm",
                    "status": "fail",
                    "title": "Terminal VM not found",
                    "action": "Provision a Terminal VM first",
                    "severity": "critical",
                }
            )

    # 5. Storage containers exist (queries, results, blast-db)
    if storage_account:
        try:
            from azure.storage.blob import BlobServiceClient

            blob_svc = BlobServiceClient(
                account_url=f"https://{storage_account}.blob.core.windows.net",
                credential=cred,
            )
            existing_containers = {c.name for c in blob_svc.list_containers()}
            required = {"blast-db", "queries", "results"}
            missing_containers = required - existing_containers
            if missing_containers:
                checks.append(
                    {
                        "id": "storage_containers",
                        "status": "fail",
                        "title": (
                            f"Missing storage containers: {', '.join(sorted(missing_containers))}"
                        ),
                        "action": "Create containers from the Dashboard storage card",
                        "severity": "high",
                    }
                )
            else:
                checks.append(
                    {
                        "id": "storage_containers",
                        "status": "pass",
                        "title": "Storage containers ready",
                    }
                )
        except Exception as exc:
            checks.append(
                {
                    "id": "storage_containers",
                    "status": "warn",
                    "title": "Could not check storage containers",
                    "detail": sanitise(str(exc))[:200],
                    "severity": "medium",
                }
            )

    # 6. Query FASTA format validation
    if query_data:
        lines = query_data.strip().split("\n")
        if not lines or not lines[0].startswith(">"):
            checks.append(
                {
                    "id": "fasta_format",
                    "status": "fail",
                    "title": "Invalid FASTA: must start with '>' header line",
                    "severity": "high",
                }
            )
        else:
            seq_count = sum(1 for line in lines if line.startswith(">"))
            total_bases = sum(len(line.strip()) for line in lines if not line.startswith(">"))
            checks.append(
                {
                    "id": "fasta_format",
                    "status": "pass",
                    "title": f"FASTA valid: {seq_count} sequence(s), {total_bases:,} residues",
                }
            )

    # Summary
    all_pass = all(c["status"] in ("pass", "skip") for c in checks)
    critical_fails = [
        c for c in checks if c["status"] == "fail" and c.get("severity") == "critical"
    ]
    return _json_response(
        {
            "ready": all_pass,
            "checks": checks,
            "critical_blockers": len(critical_fails),
            "summary": "All checks passed — ready to submit"
            if all_pass
            else f"{len(critical_fails)} critical issue(s) must be resolved before submitting",
        }
    )


# ---------------------------------------------------------------------------
# BLAST — job submission
# ---------------------------------------------------------------------------
@bp.route(route="blast/submit", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def start_blast_submit(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    raw_body = req.get_body()
    if not raw_body:
        return _error_response(400, "request body required")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    try:
        parsed = BlastSubmitRequest.model_validate(payload)
    except ValidationError as exc:
        return _error_response(400, exc.json())

    job_id = f"job-{uuid.uuid4().hex[:12]}"

    orchestration_input = {
        **parsed.model_dump(),
        "job_id": job_id,
        "user_assertion": identity.raw_token,
        "owner_oid": identity.object_id,
    }
    instance_id = await client.start_new("submit_blast_orchestrator", None, orchestration_input)

    # Register job in entity
    entity_id = df.EntityId("job_registry_entity", "default")
    await client.signal_entity(
        entity_id,
        "add_job",
        {
            "job_id": job_id,
            "instance_id": instance_id,
            "job_title": parsed.job_title,
            "program": parsed.program.value,
            "db": parsed.db,
            "status": "submitted",
            "phase": "uploading",
            "config_snapshot": {
                "evalue": parsed.evalue,
                "max_target_seqs": parsed.max_target_seqs,
                "outfmt": parsed.outfmt,
                "machine_type": parsed.machine_type,
                "num_nodes": parsed.num_nodes,
            },
            "infrastructure": {
                "subscription_id": parsed.subscription_id,
                "resource_group": parsed.resource_group,
                "region": parsed.region,
                "storage_account": parsed.storage_account,
                "acr_name": parsed.acr_name,
                "cluster_name": parsed.aks_cluster_name or f"elastic-blast-{job_id[:12]}",
                "elb_namespace": f"elastic-blast-{job_id[:12]}",
                "terminal_vm": parsed.terminal_vm_name,
            },
            "owner_oid": identity.object_id,
            "owner_upn": identity.upn or "",
        },
    )

    LOGGER.info("started submit_blast_orchestrator job=%s instance=%s", job_id, instance_id)
    return _json_response(
        {
            "job_id": job_id,
            "instance_id": instance_id,
        },
        status=202,
    )


@bp.route(route="blast/submit/{instance_id}/status", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def get_blast_submit_status(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    instance_id = req.route_params.get("instance_id")
    if not instance_id or not _RE_INSTANCE_ID.match(instance_id):
        return _error_response(400, "invalid instance_id")
    status = await client.get_status(instance_id, show_input=False)
    if status is None or status.runtime_status is None:
        return _error_response(404, "instance not found")
    return _json_response(
        {
            "instance_id": status.instance_id,
            "runtime_status": status.runtime_status.name,
            "custom_status": status.custom_status,
            "created_time": status.created_time,
            "last_updated_time": status.last_updated_time,
            "output": status.output,
        }
    )


# ---------------------------------------------------------------------------
# BLAST — blob content preview
# ---------------------------------------------------------------------------
@bp.route(route="blast/jobs/{job_id}/file", methods=["GET"])
def read_blast_job_file(req: func.HttpRequest) -> func.HttpResponse:
    """Read a job artifact (input.fa or elastic-blast.ini) from queries container."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = req.route_params.get("job_id")
    filename = req.params.get("name", "")
    if not job_id or not filename:
        return _error_response(400, "job_id and name are required")

    # Only allow known safe filenames
    ALLOWED_FILES = {"input.fa", "elastic-blast.ini"}
    if filename not in ALLOWED_FILES:
        return _error_response(400, f"file not allowed: {filename}")

    params, err = _require_query(req, "subscription_id", "storage_account")
    if err:
        return err

    cred = credential_for_caller(identity.raw_token)
    try:
        max_bytes = min(int(req.params.get("max_bytes", "4096")), 10000)
    except ValueError:
        max_bytes = 4096

    try:
        text = storage_data_svc.read_blob_text(
            cred,
            params["storage_account"],
            "queries",
            f"{job_id}/{filename}",
            max_bytes=max_bytes,
        )
        return _json_response(
            {"name": filename, "content": text, "truncated": len(text) >= max_bytes}
        )
    except Exception as exc:
        return _error_response(404, f"file not found: {sanitise(str(exc))[:200]}")


# ---------------------------------------------------------------------------
# BLAST — query upload
# ---------------------------------------------------------------------------
@bp.route(route="blast/upload-query", methods=["POST"])
def upload_blast_query(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    content_type = req.headers.get("Content-Type", "")

    if "multipart/form-data" in content_type:
        # File upload
        files = req.files
        fa_file = files.get("query_file")
        if not fa_file:
            return _error_response(400, "query_file field required")
        fasta_text = fa_file.read().decode("utf-8")
        sub_id = req.form.get("subscription_id", "")
        account = req.form.get("storage_account", "")
        container = req.form.get("container", "queries")
        filename = fa_file.filename or "input.fa"
    else:
        # JSON body with inline FASTA
        try:
            body = json.loads(req.get_body() or b"{}")
        except json.JSONDecodeError as exc:
            return _error_response(400, f"invalid JSON: {exc}")
        fasta_text = body.get("query_data", "")
        if not fasta_text:
            return _error_response(400, "query_data required")
        sub_id = body.get("subscription_id", "")
        account = body.get("storage_account", "")
        container = body.get("container", "queries")
        filename = body.get("filename", "input.fa")

    if not all([sub_id, account]):
        return _error_response(400, "subscription_id and storage_account required")

    blob_path = f"upload-{uuid.uuid4().hex[:8]}/{filename}"

    cred = credential_for_caller(identity.raw_token)
    url = storage_data_svc.upload_query_text(cred, account, container, blob_path, fasta_text)
    return _json_response({"blob_url": url, "blob_path": blob_path})


# ---------------------------------------------------------------------------
# BLAST — job listing & detail
# ---------------------------------------------------------------------------
@bp.route(route="blast/jobs", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def list_blast_jobs(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    entity_id = df.EntityId("job_registry_entity", "default")
    state = await client.read_entity_state(entity_id)
    all_jobs = state.entity_state if state.entity_exists else []
    # C5: Filter jobs by owner — only return jobs belonging to the caller
    caller_oid = identity.object_id
    jobs = [j for j in (all_jobs or []) if j.get("owner_oid") == caller_oid]

    # Enrich with orchestrator status — fix stale entity states
    for job in jobs:
        instance_id = job.get("instance_id")
        if instance_id and job.get("status") in ("submitted", "uploading"):
            try:
                orch = await client.get_status(instance_id, show_input=False)
                if orch and orch.runtime_status:
                    rt = orch.runtime_status.name
                    if rt == "Failed":
                        job["status"] = "failed"
                        job["phase"] = "error"
                        # Extract error from output
                        out = orch.output or ""
                        if isinstance(out, str) and out:
                            job["error"] = out[:300]
                    elif rt == "Completed":
                        out = orch.output if isinstance(orch.output, dict) else {}
                        job["status"] = out.get("status", "completed")
                        job["phase"] = out.get("phase", "completed")
                    if orch.custom_status:
                        cs = orch.custom_status if isinstance(orch.custom_status, dict) else {}
                        if cs.get("phase") and job.get("phase") == "uploading":
                            job["phase"] = cs["phase"]
            except Exception:
                LOGGER.debug("failed to refresh stale blast job state", exc_info=True)

    return _json_response({"jobs": jobs})


@bp.route(route="blast/jobs/{job_id}", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def get_blast_job(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = req.route_params.get("job_id")
    if not job_id:
        return _error_response(400, "job_id missing")

    entity_id = df.EntityId("job_registry_entity", "default")
    state = await client.read_entity_state(entity_id)
    jobs = state.entity_state if state.entity_exists else []
    job = next((j for j in (jobs or []) if j.get("job_id") == job_id), None)
    if not job:
        return _error_response(404, "job not found")

    # Ownership check — only the job owner can view details
    if job.get("owner_oid") and job["owner_oid"] != identity.object_id:
        return _error_response(403, "not authorized to view this job")

    # Enrich with orchestrator status if instance_id is known
    instance_id = job.get("instance_id")
    if instance_id:
        show_history = req.params.get("history", "").lower() in ("1", "true")
        orch_status = await client.get_status(
            instance_id,
            show_input=False,
            show_history=show_history,
            show_history_output=show_history,
        )
        if orch_status:
            job["runtime_status"] = (
                orch_status.runtime_status.name if orch_status.runtime_status else "Unknown"
            )
            job["custom_status"] = orch_status.custom_status
            job["output"] = orch_status.output
            if show_history and orch_status.history:
                job["history"] = orch_status.history

    return _json_response(job)


@bp.route(route="blast/jobs/{job_id}", methods=["DELETE"])
@bp.durable_client_input(client_name="client")
async def delete_blast_job(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = req.route_params.get("job_id")
    if not job_id:
        return _error_response(400, "job_id missing")

    # Get job details for deletion
    entity_id = df.EntityId("job_registry_entity", "default")
    state = await client.read_entity_state(entity_id)
    jobs = state.entity_state if state.entity_exists else []
    job = next((j for j in (jobs or []) if j.get("job_id") == job_id), None)
    if not job:
        return _error_response(404, "job not found")

    # Ownership check
    if job.get("owner_oid") and job["owner_oid"] != identity.object_id:
        return _error_response(403, "not authorized to delete this job")

    # Update status to deleting
    await client.signal_entity(
        entity_id,
        "update_job",
        {
            "job_id": job_id,
            "status": "deleting",
            "phase": "deleting",
        },
    )

    # #3 CRITICAL: Start delete orchestrator to actually clean up AKS resources
    delete_input = {
        "job_id": job_id,
        "user_assertion": identity.raw_token,
        "owner_oid": identity.object_id,
        **{
            k: job.get(k)
            for k in (
                "subscription_id",
                "resource_group",
                "storage_account",
                "cluster_name",
                "config_snapshot",
            )
            if job.get(k)
        },
    }
    delete_instance_id = await client.start_new("delete_blast_orchestrator", None, delete_input)
    LOGGER.info("started delete_blast_orchestrator job=%s instance=%s", job_id, delete_instance_id)

    return _json_response(
        {"job_id": job_id, "status": "deleting", "instance_id": delete_instance_id}
    )


@bp.route(route="blast/jobs/{job_id}/cancel", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def cancel_blast_job(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Terminate a running BLAST orchestrator and mark the job as cancelled."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = req.route_params.get("job_id")
    if not job_id:
        return _error_response(400, "job_id missing")

    entity_id = df.EntityId("job_registry_entity", "default")
    state = await client.read_entity_state(entity_id)
    jobs = state.entity_state if state.entity_exists else []
    job = next((j for j in (jobs or []) if j.get("job_id") == job_id), None)
    if not job:
        return _error_response(404, "job not found")

    # Ownership check
    if job.get("owner_oid") and job["owner_oid"] != identity.object_id:
        return _error_response(403, "not authorized to cancel this job")

    instance_id = job.get("instance_id")
    if not instance_id:
        return _error_response(400, "no orchestrator instance for this job")

    # Terminate the running orchestrator
    await client.terminate(instance_id, "Cancelled by user")
    await client.signal_entity(
        entity_id,
        "update_job",
        {
            "job_id": job_id,
            "status": "cancelled",
            "phase": "cancelled",
        },
    )
    LOGGER.info("cancelled blast job=%s instance=%s", job_id, instance_id)
    return _json_response({"job_id": job_id, "status": "cancelled"})


# ---------------------------------------------------------------------------
# BLAST — results
# ---------------------------------------------------------------------------
@bp.route(route="blast/jobs/{job_id}/results", methods=["GET"])
def list_blast_results(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = req.route_params.get("job_id")
    if not job_id:
        return _error_response(400, "job_id missing")

    params, err = _require_query(req, "subscription_id", "storage_account")
    if err:
        return err

    resource_group = req.params.get("resource_group", "")

    cred = credential_for_caller(identity.raw_token)

    # Check public access state
    if resource_group:
        try:
            from azure.mgmt.storage import StorageManagementClient as _SM

            sm = _SM(cred, params["subscription_id"])
            acct = sm.storage_accounts.get_properties(resource_group, params["storage_account"])
            if getattr(acct, "public_network_access", "Enabled") != "Enabled":
                return _json_response(
                    {
                        "job_id": job_id,
                        "files": [],
                        "public_access_disabled": True,
                        "message": "Storage public access is disabled. Enable it to view results.",
                    }
                )
        except Exception:
            LOGGER.debug("failed to read storage public access state", exc_info=True)

    try:
        blobs = storage_data_svc.list_result_blobs(
            cred,
            params["storage_account"],
            "results",
            job_id,
        )
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))
    return _json_response({"job_id": job_id, "files": blobs})


@bp.route(route="blast/jobs/{job_id}/results/download", methods=["GET"])
def download_blast_result(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = req.route_params.get("job_id")
    if not job_id:
        return _error_response(400, "job_id missing")

    params, err = _require_query(req, "subscription_id", "storage_account", "blob_name")
    if err:
        return err
    # C7: Validate blob_name — reject path traversal
    blob_name = params["blob_name"]
    if ".." in blob_name or blob_name.startswith("/"):
        return _error_response(400, "Invalid blob_name")
    if err := _validate_name(blob_name, _RE_BLOB_NAME, "blob_name"):
        return _error_response(400, err)
    # Validate storage account name
    if err := _validate_name(params["storage_account"], _RE_STORAGE_ACCOUNT, "storage_account"):
        return _error_response(400, err)

    cred = credential_for_caller(identity.raw_token)
    url = storage_data_svc.generate_download_url(
        cred,
        params["storage_account"],
        "results",
        params["blob_name"],
    )
    return _json_response({"download_url": url})


# ---------------------------------------------------------------------------
# BLAST — custom database builder
# ---------------------------------------------------------------------------
@bp.route(route="blast/databases/build", methods=["POST"])
def build_custom_database(req: func.HttpRequest) -> func.HttpResponse:
    """Upload FASTA and run makeblastdb on the Terminal VM to create a custom BLAST DB.

    Expects JSON body:
      - subscription_id, resource_group, storage_account: Azure context
      - terminal_resource_group, terminal_vm_name: VM to run makeblastdb on
      - db_name: name for the new database (alphanumeric + _ -)
      - db_type: "nucl" or "prot"
      - title: human-readable title for the database (optional)
      - fasta_data: inline FASTA text (mutually exclusive with fasta_blob_url)
      - fasta_blob_url: pre-uploaded FASTA blob path (mutually exclusive with fasta_data)
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON body")

    sub = body.get("subscription_id", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    rg = body.get("resource_group", "")
    if err := _validate_rg(rg):
        return _error_response(400, err)
    storage_account = body.get("storage_account", "")
    if err := _validate_name(storage_account, _RE_STORAGE_ACCOUNT, "storage_account"):
        return _error_response(400, err)
    db_name = body.get("db_name", "")
    if err := _validate_name(db_name, _RE_DB_NAME, "db_name"):
        return _error_response(400, err)
    db_type = body.get("db_type", "nucl")
    if db_type not in ("nucl", "prot"):
        return _error_response(400, "db_type must be 'nucl' or 'prot'")
    title = body.get("title", db_name)
    if len(title) > 200:
        return _error_response(400, "title too long (max 200)")

    fasta_data = body.get("fasta_data")
    fasta_blob_url = body.get("fasta_blob_url")
    if not fasta_data and not fasta_blob_url:
        return _error_response(400, "provide fasta_data or fasta_blob_url")
    if fasta_data and fasta_blob_url:
        return _error_response(400, "provide fasta_data OR fasta_blob_url, not both")

    cred = credential_for_caller(identity.raw_token)

    terminal_rg = body.get(
        "terminal_resource_group", os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal")
    )
    terminal_vm = body.get("terminal_vm_name", "vm-elb-terminal")

    try:
        # Step 0: Enable public network access on the storage account.
        # The account is kept with publicNetworkAccess=Disabled by default;
        # we must open it for the data-plane upload/download to succeed.
        _toggle_public_access(cred, sub, rg, storage_account, enabled=True)

        # Step 1: Upload FASTA to blob if inline
        # Temporary staging path — cleaned up after build completes.
        fasta_staging_path = f"custom_db/.staging/{db_name}/input.fa"
        if fasta_data:
            storage_data_svc.upload_query_text(
                cred,
                storage_account,
                "blast-db",
                fasta_staging_path,
                fasta_data,
            )
            blob_path = fasta_staging_path
        else:
            blob_path = fasta_blob_url  # type: ignore[assignment]

        # Step 2: Get VM SSH details
        vm_ip = compute_svc.get_vm_public_ip(cred, sub, terminal_rg, terminal_vm)
        if not vm_ip:
            return _error_response(
                400, "Terminal VM has no public IP. Provision and start it first."
            )

        vault_url = os.environ.get("ELB_KEYVAULT_URL") or os.environ.get("KEY_VAULT_URI", "")
        if not vault_url:
            return _error_response(500, "Key Vault URL not configured")

        password = kv_svc.get_secret(cred, vault_url, f"vm-{terminal_vm}-password")

        # Step 3: Run makeblastdb on the VM via SSH
        from services.ssh_exec import run_ssh

        # Build the script to download FASTA from blob, run makeblastdb, upload results
        safe_db_name = db_name.replace("'", "")
        safe_title = title.replace("'", "").replace('"', "")
        safe_db_type = db_type

        script = f"""set -euo pipefail
WORK_DIR=$(mktemp -d /tmp/makeblastdb-XXXXXX)
cd "$WORK_DIR"

# Download FASTA from blob storage
az storage blob download \\
  --account-name '{storage_account}' \\
  --container-name 'blast-db' \\
  --name '{blob_path}' \\
  --file input.fa \\
  --auth-mode login \\
  --output none 2>&1

# Run makeblastdb
makeblastdb \\
  -in input.fa \\
  -dbtype {safe_db_type} \\
  -out '{safe_db_name}' \\
  -title '{safe_title}' \\
  -parse_seqids 2>&1

# Count generated files
FILE_COUNT=$(ls -1 {safe_db_name}.* 2>/dev/null | wc -l)
echo "MAKEBLASTDB_FILES=$FILE_COUNT"

# Upload all DB files back to blob storage under custom_db/{db_name}/
for f in {safe_db_name}.*; do
  az storage blob upload \\
    --account-name '{storage_account}' \\
    --container-name 'blast-db' \\
    --name "custom_db/{safe_db_name}/$f" \\
    --file "$f" \\
    --auth-mode login \\
    --overwrite \\
    --output none 2>&1
done

# Write metadata
cat > metadata.json <<METAEOF
{{
  "db_name": "{safe_db_name}",
  "db_type": "{safe_db_type}",
  "title": "{safe_title}",
  "source": "custom",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "file_count": $FILE_COUNT
}}
METAEOF

az storage blob upload \\
  --account-name '{storage_account}' \\
  --container-name 'blast-db' \\
  --name "custom_db/{safe_db_name}/{safe_db_name}-metadata.json" \\
  --file metadata.json \\
  --auth-mode login \\
  --overwrite \\
  --output none 2>&1

# Delete the staging FASTA blob
az storage blob delete \\
  --account-name '{storage_account}' \\
  --container-name 'blast-db' \\
  --name '{blob_path}' \\
  --auth-mode login \\
  --output none 2>&1 || true

# Cleanup local temp
rm -rf "$WORK_DIR"
echo "MAKEBLASTDB_DONE"
"""
        output = run_ssh(vm_ip, password, script, timeout=600)

        if "MAKEBLASTDB_DONE" not in output:
            return _error_response(500, f"makeblastdb failed: {sanitise(output[-500:])}")

        # Extract file count
        file_count = 0
        for line in output.splitlines():
            if line.startswith("MAKEBLASTDB_FILES="):
                file_count = int(line.split("=")[1].strip())

        return _json_response(
            {
                "db_name": db_name,
                "db_type": db_type,
                "title": title,
                "status": "completed",
                "file_count": file_count,
                "container": "blast-db",
                "path": f"custom_db/{db_name}/",
            }
        )
    except Exception as exc:
        msg = str(exc)
        if "AuthorizationFailure" in msg or "AuthorizationPermissionMismatch" in msg:
            return _error_response(403, "Storage or VM access denied. Check RBAC roles.")
        return _error_response(500, sanitise(msg[:500]))
    finally:
        # Always re-disable public network access, even on failure.
        try:
            _toggle_public_access(cred, sub, rg, storage_account, enabled=False)
        except Exception:
            LOGGER.warning("Could not re-disable public access on %s after custom DB build", storage_account)


# ---------------------------------------------------------------------------
# BLAST — results aggregation / analytics
# ---------------------------------------------------------------------------
@bp.route(route="blast/jobs/{job_id}/results/aggregate", methods=["GET"])
def blast_results_aggregate(req: func.HttpRequest) -> func.HttpResponse:
    """Parse BLAST tabular output (outfmt 7) and return aggregated statistics.

    Returns: hit count, unique subject count, E-value distribution,
    identity % distribution, taxonomy breakdown (from subject IDs).
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = req.route_params.get("job_id", "")
    if not job_id or not _RE_DB_NAME.match(job_id):
        return _error_response(400, "invalid job_id")

    params, err = _require_query(req, "subscription_id", "storage_account")
    if err:
        return err

    cred = credential_for_caller(identity.raw_token)

    try:
        # List result blobs for this job
        blobs = storage_data_svc.list_result_blobs(
            cred,
            params["storage_account"],
            "results",
            f"{job_id}/",
        )

        # Find .out files (BLAST output)
        out_blobs = [b for b in blobs if b["name"].endswith(".out")]
        if not out_blobs:
            return _json_response(
                {
                    "job_id": job_id,
                    "status": "no_results",
                    "message": "No .out result files found",
                    "stats": None,
                }
            )

        # Parse BLAST tabular output (outfmt 7)
        all_hits: list[dict] = []
        max_parse_bytes = 10 * 1024 * 1024  # 10MB cap per file

        for blob_info in out_blobs[:20]:  # cap at 20 files
            try:
                content = storage_data_svc.read_blob_text(
                    cred,
                    params["storage_account"],
                    "results",
                    blob_info["name"],
                    max_bytes=max_parse_bytes,
                )
                hits = _parse_blast_tabular(content)
                all_hits.extend(hits)
            except Exception as exc:
                LOGGER.warning("Failed to parse %s: %s", blob_info["name"], exc)

        if not all_hits:
            return _json_response(
                {
                    "job_id": job_id,
                    "status": "no_hits",
                    "message": "No BLAST hits found in result files",
                    "stats": {"total_hits": 0},
                }
            )

        # Aggregate statistics
        stats = _aggregate_blast_hits(all_hits)
        stats["files_parsed"] = len(out_blobs[:20])
        stats["total_files"] = len(out_blobs)

        return _json_response(
            {
                "job_id": job_id,
                "status": "ok",
                "stats": stats,
            }
        )
    except Exception as exc:
        msg = str(exc)
        if "AuthorizationFailure" in msg:
            return _error_response(403, "Storage access denied.")
        return _error_response(500, sanitise(msg[:500]))


# ---------------------------------------------------------------------------
# BLAST — alignment detail
# ---------------------------------------------------------------------------
@bp.route(route="blast/jobs/{job_id}/results/alignments", methods=["GET"])
def blast_results_alignments(req: func.HttpRequest) -> func.HttpResponse:
    """Return parsed pairwise alignments from BLAST output for visualization.

    Query params:
      - subscription_id, storage_account: Azure context
      - blob_name: specific .out file to parse (optional, defaults to first)
      - max_alignments: max number of alignments to return (default 50)
      - query_id: filter by specific query sequence ID (optional)
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = req.route_params.get("job_id", "")
    if not job_id or not _RE_DB_NAME.match(job_id):
        return _error_response(400, "invalid job_id")

    params, err = _require_query(req, "subscription_id", "storage_account")
    if err:
        return err

    blob_name = req.params.get("blob_name", "")
    max_alignments = min(int(req.params.get("max_alignments", "50")), 200)
    query_id_filter = req.params.get("query_id", "")

    cred = credential_for_caller(identity.raw_token)

    try:
        # If no specific blob, find the first .out file
        if not blob_name:
            blobs = storage_data_svc.list_result_blobs(
                cred,
                params["storage_account"],
                "results",
                f"{job_id}/",
            )
            out_blobs = [b for b in blobs if b["name"].endswith(".out")]
            if not out_blobs:
                return _json_response(
                    {"job_id": job_id, "alignments": [], "message": "No result files"}
                )
            blob_name = out_blobs[0]["name"]
        else:
            # Validate blob_name
            if ".." in blob_name or blob_name.startswith("/"):
                return _error_response(400, "invalid blob_name")

        # Read the result file
        content = storage_data_svc.read_blob_text(
            cred,
            params["storage_account"],
            "results",
            blob_name,
            max_bytes=20 * 1024 * 1024,  # 20MB cap
        )

        # Parse tabular hits
        hits = _parse_blast_tabular(content)

        # Filter by query_id if specified
        if query_id_filter:
            hits = [h for h in hits if h.get("qseqid") == query_id_filter]

        # Limit results
        hits = hits[:max_alignments]

        # Extract unique query IDs for the filter dropdown
        all_hits = _parse_blast_tabular(content)
        query_ids = sorted(set(h.get("qseqid", "") for h in all_hits if h.get("qseqid")))

        return _json_response(
            {
                "job_id": job_id,
                "blob_name": blob_name,
                "alignments": hits,
                "total_hits": len(all_hits),
                "returned": len(hits),
                "query_ids": query_ids[:100],  # cap at 100
            }
        )
    except Exception as exc:
        msg = str(exc)
        if "AuthorizationFailure" in msg:
            return _error_response(403, "Storage access denied.")
        return _error_response(500, sanitise(msg[:500]))


def _parse_blast_tabular(content: str) -> list[dict]:
    """Parse BLAST outfmt 7 (tabular with comments) into list of hit dicts.

    Default outfmt 7 columns:
    qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore
    """
    hits: list[dict] = []
    columns = [
        "qseqid",
        "sseqid",
        "pident",
        "length",
        "mismatch",
        "gapopen",
        "qstart",
        "qend",
        "sstart",
        "send",
        "evalue",
        "bitscore",
    ]
    custom_columns: list[str] | None = None

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # Parse column header from comment line
        if line.startswith("# Fields:"):
            field_str = line[len("# Fields:") :].strip()
            field_map = {
                "query acc.ver": "qseqid",
                "subject acc.ver": "sseqid",
                "% identity": "pident",
                "alignment length": "length",
                "mismatches": "mismatch",
                "gap opens": "gapopen",
                "q. start": "qstart",
                "q. end": "qend",
                "s. start": "sstart",
                "s. end": "send",
                "evalue": "evalue",
                "bit score": "bitscore",
                "query acc.": "qseqid",
                "subject acc.": "sseqid",
                "query id": "qseqid",
                "subject id": "sseqid",
                "% positives": "ppos",
                "query length": "qlen",
                "subject length": "slen",
                "query seq": "qseq",
                "subject seq": "sseq",
            }
            raw_fields = [f.strip() for f in field_str.split(",")]
            custom_columns = [
                field_map.get(f, f.replace(" ", "_").replace(".", "")) for f in raw_fields
            ]
            continue
        if line.startswith("#"):
            continue

        parts = line.split("\t")
        cols = custom_columns if custom_columns else columns
        if len(parts) < len(cols):
            continue

        hit: dict = {}
        for i, col in enumerate(cols):
            val = parts[i] if i < len(parts) else ""
            # Convert numeric fields
            if col in ("pident", "evalue", "bitscore", "ppos"):
                try:
                    hit[col] = float(val)
                except ValueError:
                    hit[col] = val
            elif col in (
                "length",
                "mismatch",
                "gapopen",
                "qstart",
                "qend",
                "sstart",
                "send",
                "qlen",
                "slen",
            ):
                try:
                    hit[col] = int(val)
                except ValueError:
                    hit[col] = val
            else:
                hit[col] = val
        hits.append(hit)

    return hits


def _aggregate_blast_hits(hits: list[dict]) -> dict:
    """Compute aggregate statistics from parsed BLAST hits."""

    total = len(hits)
    unique_queries = set()
    unique_subjects = set()
    evalues: list[float] = []
    identities: list[float] = []
    bitscores: list[float] = []
    lengths: list[int] = []
    subject_counts: dict[str, int] = {}

    for h in hits:
        qid = h.get("qseqid", "")
        sid = h.get("sseqid", "")
        if qid:
            unique_queries.add(qid)
        if sid:
            unique_subjects.add(sid)
            # Extract genus/species from accession for taxonomy approximation
            subject_counts[sid] = subject_counts.get(sid, 0) + 1

        ev = h.get("evalue")
        if isinstance(ev, (int, float)) and ev >= 0:
            evalues.append(ev)
        pident = h.get("pident")
        if isinstance(pident, (int, float)):
            identities.append(pident)
        bs = h.get("bitscore")
        if isinstance(bs, (int, float)):
            bitscores.append(bs)
        length = h.get("length")
        if isinstance(length, int):
            lengths.append(length)

    # E-value distribution (log10 bins)
    evalue_bins: dict[str, int] = {
        "0": 0,
        "1e-200..1e-100": 0,
        "1e-100..1e-50": 0,
        "1e-50..1e-10": 0,
        "1e-10..1e-5": 0,
        "1e-5..0.01": 0,
        "0.01..1": 0,
        "1..10": 0,
        ">10": 0,
    }
    for ev in evalues:
        if ev == 0:
            evalue_bins["0"] += 1
        elif ev < 1e-100:
            evalue_bins["1e-200..1e-100"] += 1
        elif ev < 1e-50:
            evalue_bins["1e-100..1e-50"] += 1
        elif ev < 1e-10:
            evalue_bins["1e-50..1e-10"] += 1
        elif ev < 1e-5:
            evalue_bins["1e-10..1e-5"] += 1
        elif ev < 0.01:
            evalue_bins["1e-5..0.01"] += 1
        elif ev < 1:
            evalue_bins["0.01..1"] += 1
        elif ev <= 10:
            evalue_bins["1..10"] += 1
        else:
            evalue_bins[">10"] += 1

    # Identity distribution (10% bins)
    identity_bins: dict[str, int] = {}
    for pct in range(0, 100, 10):
        label = f"{pct}-{pct + 10}%"
        identity_bins[label] = sum(1 for p in identities if pct <= p < pct + 10)
    identity_bins["100%"] = sum(1 for p in identities if p == 100)

    # Top hits (most frequently hit subjects)
    top_subjects = sorted(subject_counts.items(), key=lambda x: x[1], reverse=True)[:20]

    return {
        "total_hits": total,
        "unique_queries": len(unique_queries),
        "unique_subjects": len(unique_subjects),
        "evalue_distribution": evalue_bins,
        "identity_distribution": identity_bins,
        "top_subjects": [{"id": s, "count": c} for s, c in top_subjects],
        "avg_identity": round(sum(identities) / len(identities), 2) if identities else None,
        "avg_bitscore": round(sum(bitscores) / len(bitscores), 2) if bitscores else None,
        "avg_length": round(sum(lengths) / len(lengths), 1) if lengths else None,
        "max_bitscore": max(bitscores) if bitscores else None,
        "min_evalue": min(evalues) if evalues else None,
    }


# ---------------------------------------------------------------------------
# BLAST — database listing
# ---------------------------------------------------------------------------
@bp.route(route="blast/databases", methods=["GET"])
def list_blast_databases(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    params, err = _require_query(req, "subscription_id", "storage_account", "resource_group")
    if err:
        return err

    cred = credential_for_caller(identity.raw_token)

    try:
        # Check public network access state first
        from azure.mgmt.storage import StorageManagementClient as _StorageMgmt

        storage_mgmt = _StorageMgmt(cred, params["subscription_id"])
        acct = storage_mgmt.storage_accounts.get_properties(
            params["resource_group"],
            params["storage_account"],
        )
        public_access = getattr(acct, "public_network_access", "Enabled")

        if public_access != "Enabled":
            return _json_response(
                {
                    "databases": [],
                    "public_access_disabled": True,
                    "message": "Storage public network access is disabled. "
                    "Enable it temporarily to scan for databases.",
                }
            )

        # Try Data Plane access
        dbs = storage_data_svc.list_databases(
            cred,
            params["storage_account"],
        )
    except Exception as exc:
        msg = str(exc)
        if "AuthorizationFailure" in msg or "AuthorizationPermissionMismatch" in msg:
            return _error_response(
                403,
                (
                    "Storage data-plane access denied. "
                    "Assign 'Storage Blob Data Reader' (or Contributor) "
                    "role to your account on this storage account."
                ),
            )
        return _error_response(500, sanitise(msg))
    return _json_response({"databases": dbs})


# ---------------------------------------------------------------------------
# BLAST — database update check
# ---------------------------------------------------------------------------
@bp.route(route="blast/databases/check-updates", methods=["GET"])
def check_blast_db_updates(req: func.HttpRequest) -> func.HttpResponse:
    """Check NCBI S3 for the latest DB version and compare with local metadata."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        s3_base = "https://ncbi-blast-databases.s3.amazonaws.com"
        resp = _requests.get(f"{s3_base}/latest-dir", timeout=15)
        resp.raise_for_status()
        latest_version = resp.text.strip()
        return _json_response({"latest_version": latest_version})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


# ═══════════════════════════════════════════════════════════════════════
# P6 — Report Generator (CSV / JSON export)
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/jobs/{job_id}/results/export", methods=["GET"])
def blast_results_export(req: func.HttpRequest) -> func.HttpResponse:
    """Export BLAST results as CSV or JSON for reports."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = req.route_params.get("job_id", "")
    if not job_id or not _RE_DB_NAME.match(job_id):
        return _error_response(400, "invalid job_id")

    params, err = _require_query(req, "subscription_id", "storage_account")
    if err:
        return err

    fmt = req.params.get("format", "csv")
    if fmt not in ("csv", "json", "tsv"):
        return _error_response(400, "format must be csv, json, or tsv")

    cred = credential_for_caller(identity.raw_token)

    try:
        blobs = storage_data_svc.list_result_blobs(
            cred,
            params["storage_account"],
            "results",
            f"{job_id}/",
        )
        out_blobs = [b for b in blobs if b["name"].endswith(".out")]
        all_hits: list[dict] = []
        for blob_info in out_blobs[:20]:
            try:
                content = storage_data_svc.read_blob_text(
                    cred,
                    params["storage_account"],
                    "results",
                    blob_info["name"],
                    max_bytes=10 * 1024 * 1024,
                )
                all_hits.extend(_parse_blast_tabular(content))
            except Exception:
                LOGGER.debug("failed to parse result blob for export", exc_info=True)

        if fmt == "json":
            body = json.dumps(
                {"job_id": job_id, "hits": all_hits, "total": len(all_hits)}, default=str
            )
            return func.HttpResponse(
                body,
                status_code=200,
                mimetype="application/json",
                headers={"Content-Disposition": f'attachment; filename="{job_id}_results.json"'},
            )

        # CSV / TSV
        import csv
        import io

        delimiter = "\t" if fmt == "tsv" else ","
        cols = [
            "qseqid",
            "sseqid",
            "pident",
            "length",
            "mismatch",
            "gapopen",
            "qstart",
            "qend",
            "sstart",
            "send",
            "evalue",
            "bitscore",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, delimiter=delimiter, extrasaction="ignore")
        writer.writeheader()
        for h in all_hits:
            writer.writerow(h)
        ext = "tsv" if fmt == "tsv" else "csv"
        mime = "text/tab-separated-values" if fmt == "tsv" else "text/csv"
        return func.HttpResponse(
            buf.getvalue(),
            status_code=200,
            mimetype=mime,
            headers={"Content-Disposition": f'attachment; filename="{job_id}_results.{ext}"'},
        )
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)[:500]))


# ═══════════════════════════════════════════════════════════════════════
# P11 — Cost Estimator
# ═══════════════════════════════════════════════════════════════════════
_AZURE_VM_HOURLY_USD: dict[str, float] = {
    "Standard_D2s_v5": 0.096,
    "Standard_D4s_v5": 0.192,
    "Standard_D8s_v5": 0.384,
    "Standard_D16s_v5": 0.768,
    "Standard_E4s_v5": 0.252,
    "Standard_E8s_v5": 0.504,
    "Standard_E16s_v5": 1.008,
    "Standard_E32s_v5": 2.016,
    "Standard_E48s_v5": 3.024,
    "Standard_E64s_v5": 4.032,
    "Standard_D2s_v3": 0.096,
    "Standard_D4s_v3": 0.192,
}
_STORAGE_GB_MONTH_USD = 0.018  # Hot tier
_PD_GB_MONTH_USD = 0.040  # Managed SSD


@bp.route(route="blast/cost-estimate", methods=["POST"])
def blast_cost_estimate(req: func.HttpRequest) -> func.HttpResponse:
    """Estimate Azure cost for a BLAST job."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    node_sku = body.get("machine_type", "Standard_E16s_v5")
    num_nodes = max(1, min(int(body.get("num_nodes", 3)), 100))
    estimated_hours = max(0.1, min(float(body.get("estimated_hours", 2.0)), 168))
    pd_size_gb = max(10, min(int(body.get("pd_size_gb", 1000)), 10000))
    db_size_gb = float(body.get("db_size_gb", 50))

    hourly = _AZURE_VM_HOURLY_USD.get(node_sku, 1.0)

    compute_cost = hourly * num_nodes * estimated_hours
    disk_cost = (pd_size_gb * _PD_GB_MONTH_USD / 730) * estimated_hours * num_nodes
    storage_cost = db_size_gb * _STORAGE_GB_MONTH_USD / 730 * estimated_hours
    total = compute_cost + disk_cost + storage_cost

    return _json_response(
        {
            "estimate": {
                "compute_usd": round(compute_cost, 2),
                "disk_usd": round(disk_cost, 2),
                "storage_usd": round(storage_cost, 2),
                "total_usd": round(total, 2),
            },
            "params": {
                "node_sku": node_sku,
                "num_nodes": num_nodes,
                "estimated_hours": estimated_hours,
                "pd_size_gb": pd_size_gb,
                "hourly_rate_usd": hourly,
            },
            "note": "Estimate based on pay-as-you-go pricing. Actual costs may vary.",
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# P8 — Multi-DB Search
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/multi-submit", methods=["POST"])
def blast_multi_db_submit(req: func.HttpRequest) -> func.HttpResponse:
    """Submit the same query against multiple databases simultaneously."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    databases: list[str] = body.get("databases", [])
    if not databases or len(databases) < 2:
        return _error_response(400, "provide at least 2 databases")
    if len(databases) > 10:
        return _error_response(400, "max 10 databases per multi-search")

    # Validate each DB name
    for db in databases:
        if not db or len(db) > 200:
            return _error_response(400, f"invalid database name: {sanitise(db[:40])}")

    # Create a job for each database
    client = df.DurableOrchestrationClient(req)
    group_id = f"multi-{uuid.uuid4().hex[:12]}"
    jobs: list[dict] = []

    for db_name in databases:
        job_body = dict(body)
        job_body["db"] = db_name
        job_body.pop("databases", None)

        try:
            submit_req = BlastSubmitRequest(**job_body)
        except ValidationError as exc:
            return _error_response(400, f"DB '{sanitise(db_name[:30])}': {exc.errors()[0]['msg']}")

        job_id = f"{group_id}-{db_name.split('/')[-1]}"
        payload = submit_req.model_dump()
        payload["job_id"] = job_id
        payload["user_assertion"] = identity.raw_token
        payload["owner_upn"] = identity.upn
        payload["owner_oid"] = identity.oid
        payload["group_id"] = group_id

        instance_id = client.start_new("submit_blast_orchestrator", None, payload)
        jobs.append({"job_id": job_id, "db": db_name, "instance_id": instance_id})

    return _json_response(
        {
            "group_id": group_id,
            "jobs": jobs,
            "total": len(jobs),
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# P4 — Taxonomy Annotation
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/taxonomy", methods=["POST"])
def blast_taxonomy_lookup(req: func.HttpRequest) -> func.HttpResponse:
    """Look up NCBI taxonomy for subject accessions from BLAST results."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    accessions: list[str] = body.get("accessions", [])
    if not accessions:
        return _error_response(400, "provide accessions list")
    # Cap at 50 accessions per request
    accessions = accessions[:50]

    # Validate accession format
    _RE_ACCESSION = re.compile(r"^[A-Za-z0-9_.]+$")
    for acc in accessions:
        if not _RE_ACCESSION.match(acc):
            return _error_response(400, f"invalid accession: {sanitise(acc[:40])}")

    results: dict[str, dict] = {}
    try:
        # Use NCBI E-utilities to look up taxonomy
        # esummary for nucleotide/protein database
        ids_param = ",".join(accessions)
        # First: convert accession to GI via esearch
        esearch_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
            f"?db=nucleotide&id={ids_param}&retmode=json"
        )
        resp = _requests.get(esearch_url, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            uid_list = data.get("result", {}).get("uids", [])
            for uid in uid_list:
                info = data["result"].get(uid, {})
                acc_ver = info.get("accessionversion", info.get("caption", uid))
                results[acc_ver] = {
                    "accession": acc_ver,
                    "title": info.get("title", ""),
                    "organism": info.get("organism", ""),
                    "taxid": info.get("taxid", ""),
                    "source_db": info.get("sourcedb", ""),
                    "seq_length": info.get("slen", ""),
                    "mol_type": info.get("moltype", ""),
                    "update_date": info.get("updatedate", ""),
                }
    except Exception as exc:
        LOGGER.warning("NCBI taxonomy lookup failed: %s", exc)

    # Also try protein DB for any missing
    missing = [a for a in accessions if a not in results]
    if missing:
        try:
            ids_param = ",".join(missing)
            prot_url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                f"?db=protein&id={ids_param}&retmode=json"
            )
            resp = _requests.get(prot_url, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                for uid in data.get("result", {}).get("uids", []):
                    info = data["result"].get(uid, {})
                    acc_ver = info.get("accessionversion", info.get("caption", uid))
                    results[acc_ver] = {
                        "accession": acc_ver,
                        "title": info.get("title", ""),
                        "organism": info.get("organism", ""),
                        "taxid": info.get("taxid", ""),
                        "source_db": info.get("sourcedb", ""),
                        "seq_length": info.get("slen", ""),
                    }
        except Exception:
            LOGGER.debug("protein taxonomy lookup failed", exc_info=True)

    return _json_response(
        {
            "annotations": results,
            "found": len(results),
            "requested": len(accessions),
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# P5 — Query Preprocessor (FASTQ→FASTA, stats)
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/preprocess", methods=["POST"])
def blast_preprocess_query(req: func.HttpRequest) -> func.HttpResponse:
    """Preprocess query sequences: FASTQ→FASTA conversion, quality stats, filtering."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    input_data: str = body.get("input_data", "")
    if not input_data:
        return _error_response(400, "provide input_data")
    if len(input_data) > 50 * 1024 * 1024:  # 50 MB
        return _error_response(400, "input too large (max 50 MB)")

    input_format = body.get("format", "auto")  # auto, fastq, fasta
    min_length = int(body.get("min_length", 0))
    min_quality = int(body.get("min_quality", 0))

    lines = input_data.splitlines()

    # Auto-detect format
    if input_format == "auto":
        if lines and lines[0].startswith("@"):
            input_format = "fastq"
        elif lines and lines[0].startswith(">"):
            input_format = "fasta"
        else:
            return _error_response(
                400, "cannot detect format — must start with > (FASTA) or @ (FASTQ)"
            )

    fasta_seqs: list[dict] = []
    stats = {
        "input_sequences": 0,
        "output_sequences": 0,
        "total_bases": 0,
        "filtered_short": 0,
        "filtered_quality": 0,
        "avg_length": 0.0,
        "min_len": 0,
        "max_len": 0,
        "gc_content": 0.0,
    }

    if input_format == "fastq":
        # Parse FASTQ (4 lines per record: @id, seq, +, qual)
        i = 0
        lengths: list[int] = []
        gc_count = 0
        while i + 3 < len(lines):
            header = lines[i].strip()
            seq = lines[i + 1].strip()
            # lines[i+2] is "+"
            qual = lines[i + 3].strip()
            i += 4

            if not header.startswith("@"):
                continue
            stats["input_sequences"] += 1

            # Quality filtering
            if min_quality > 0 and qual:
                avg_q = sum(ord(c) - 33 for c in qual) / max(len(qual), 1)
                if avg_q < min_quality:
                    stats["filtered_quality"] += 1
                    continue

            # Length filtering
            if min_length > 0 and len(seq) < min_length:
                stats["filtered_short"] += 1
                continue

            seq_id = header[1:].split()[0]
            desc = header[1:].split(None, 1)[1] if " " in header[1:] else ""
            fasta_seqs.append({"id": seq_id, "desc": desc, "seq": seq})
            lengths.append(len(seq))
            gc_count += seq.upper().count("G") + seq.upper().count("C")
            stats["total_bases"] += len(seq)

        if lengths:
            stats["min_len"] = min(lengths)
            stats["max_len"] = max(lengths)
            stats["avg_length"] = round(sum(lengths) / len(lengths), 1)
            stats["gc_content"] = (
                round(gc_count / stats["total_bases"] * 100, 1) if stats["total_bases"] > 0 else 0
            )

    elif input_format == "fasta":
        # Parse FASTA
        current_id = ""
        current_desc = ""
        current_seq: list[str] = []
        lengths = []
        gc_count = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if current_id and current_seq:
                    seq = "".join(current_seq)
                    stats["input_sequences"] += 1
                    if min_length > 0 and len(seq) < min_length:
                        stats["filtered_short"] += 1
                    else:
                        fasta_seqs.append({"id": current_id, "desc": current_desc, "seq": seq})
                        lengths.append(len(seq))
                        gc_count += seq.upper().count("G") + seq.upper().count("C")
                        stats["total_bases"] += len(seq)
                parts = line[1:].split(None, 1)
                current_id = parts[0] if parts else ""
                current_desc = parts[1] if len(parts) > 1 else ""
                current_seq = []
            else:
                current_seq.append(line)

        # Last sequence
        if current_id and current_seq:
            seq = "".join(current_seq)
            stats["input_sequences"] += 1
            if min_length > 0 and len(seq) < min_length:
                stats["filtered_short"] += 1
            else:
                fasta_seqs.append({"id": current_id, "desc": current_desc, "seq": seq})
                lengths.append(len(seq))
                gc_count += seq.upper().count("G") + seq.upper().count("C")
                stats["total_bases"] += len(seq)

        if lengths:
            stats["min_len"] = min(lengths)
            stats["max_len"] = max(lengths)
            stats["avg_length"] = round(sum(lengths) / len(lengths), 1)
            stats["gc_content"] = (
                round(gc_count / stats["total_bases"] * 100, 1) if stats["total_bases"] > 0 else 0
            )

    stats["output_sequences"] = len(fasta_seqs)

    # Build output FASTA
    fasta_output_lines: list[str] = []
    for s in fasta_seqs:
        header = f">{s['id']}"
        if s["desc"]:
            header += f" {s['desc']}"
        fasta_output_lines.append(header)
        # Wrap sequence at 80 chars
        seq = s["seq"]
        for j in range(0, len(seq), 80):
            fasta_output_lines.append(seq[j : j + 80])

    return _json_response(
        {
            "fasta_output": "\n".join(fasta_output_lines),
            "stats": stats,
            "detected_format": input_format,
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# P2 — DB Version Registry
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/databases/versions", methods=["GET"])
def list_db_versions(req: func.HttpRequest) -> func.HttpResponse:
    """List all database versions (based on metadata.json files in blast-db container)."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    params, err = _require_query(req, "subscription_id", "storage_account", "resource_group")
    if err:
        return err

    cred = credential_for_caller(identity.raw_token)

    try:
        svc = storage_data_svc._blob_service(cred, params["storage_account"])
        cc = svc.get_container_client("blast-db")

        versions: list[dict] = []
        for blob in cc.list_blobs():
            if blob.name.endswith("-metadata.json") or blob.name.endswith("/metadata.json"):
                try:
                    bc = cc.get_blob_client(blob.name)
                    raw = bc.download_blob().readall().decode("utf-8")
                    meta = json.loads(raw)
                    meta["_blob_path"] = blob.name
                    meta["_last_modified"] = (
                        blob.last_modified.isoformat() if blob.last_modified else None
                    )
                    meta["_size_bytes"] = blob.size
                    versions.append(meta)
                except Exception:
                    LOGGER.debug("failed to read database metadata blob", exc_info=True)

        # Sort by created_at or downloaded_at
        versions.sort(
            key=lambda v: v.get("created_at") or v.get("downloaded_at") or "",
            reverse=True,
        )
        return _json_response({"versions": versions, "total": len(versions)})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)[:500]))


@bp.route(route="blast/databases/versions", methods=["POST"])
def save_db_version_metadata(req: func.HttpRequest) -> func.HttpResponse:
    """Save or update metadata for a database version."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    sub = body.get("subscription_id", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    storage_account = body.get("storage_account", "")
    if err := _validate_name(storage_account, _RE_STORAGE_ACCOUNT, "storage_account"):
        return _error_response(400, err)
    db_name = body.get("db_name", "")
    if err := _validate_name(db_name, _RE_DB_NAME, "db_name"):
        return _error_response(400, err)

    cred = credential_for_caller(identity.raw_token)
    import datetime as _dt

    metadata = {
        "db_name": db_name,
        "db_type": body.get("db_type", "unknown"),
        "title": body.get("title", db_name),
        "source": body.get("source", "custom"),
        "source_version": body.get("source_version", ""),
        "version_tag": body.get("version_tag", ""),
        "notes": body.get("notes", ""),
        "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "created_by": identity.upn or identity.oid,
    }

    try:
        blob_path = f"{db_name}/{db_name}-metadata.json"
        storage_data_svc.upload_query_text(
            cred,
            storage_account,
            "blast-db",
            blob_path,
            json.dumps(metadata, indent=2),
        )
        return _json_response({"db_name": db_name, "status": "saved", "metadata": metadata})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)[:500]))


# ═══════════════════════════════════════════════════════════════════════
# P7 — Scheduled / Triggered BLAST routes
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/schedules", methods=["GET"])
def list_blast_schedules(req: func.HttpRequest) -> func.HttpResponse:
    """List all scheduled BLAST configurations."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    client = df.DurableOrchestrationClient(req)
    try:
        entity_id = df.EntityId("scheduled_blast_entity", "global")
        resp = client.read_entity_state(entity_id)
        schedules = resp.entity_state if resp.entity_exists else []
    except Exception:
        schedules = []

    return _json_response({"schedules": schedules})


@bp.route(route="blast/schedules", methods=["POST"])
def create_blast_schedule(req: func.HttpRequest) -> func.HttpResponse:
    """Create a scheduled/triggered BLAST job configuration."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    name = body.get("name", "")
    if not name or len(name) > 100:
        return _error_response(400, "provide a schedule name (max 100 chars)")

    trigger_type = body.get("trigger_type", "manual")  # manual, cron, on_upload
    if trigger_type not in ("manual", "cron", "on_upload"):
        return _error_response(400, "trigger_type must be manual, cron, or on_upload")

    schedule_config = {
        "schedule_id": uuid.uuid4().hex[:12],
        "name": name,
        "trigger_type": trigger_type,
        "cron_expression": body.get("cron_expression", ""),
        "watch_container": body.get("watch_container", "queries"),
        "watch_prefix": body.get("watch_prefix", ""),
        "blast_params": {
            k: v
            for k, v in body.items()
            if k
            not in ("name", "trigger_type", "cron_expression", "watch_container", "watch_prefix")
        },
        "owner_upn": identity.upn,
    }

    client = df.DurableOrchestrationClient(req)
    entity_id = df.EntityId("scheduled_blast_entity", "global")
    client.signal_entity(entity_id, "add_schedule", schedule_config)

    return _json_response({"status": "created", "schedule": schedule_config})


@bp.route(route="blast/schedules/{schedule_id}", methods=["DELETE"])
def delete_blast_schedule(req: func.HttpRequest) -> func.HttpResponse:
    """Delete a scheduled BLAST configuration."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    schedule_id = req.route_params.get("schedule_id", "")
    if not schedule_id:
        return _error_response(400, "schedule_id required")

    client = df.DurableOrchestrationClient(req)
    entity_id = df.EntityId("scheduled_blast_entity", "global")
    client.signal_entity(entity_id, "remove_schedule", {"schedule_id": schedule_id})

    return _json_response({"status": "deleted", "schedule_id": schedule_id})


@bp.route(route="blast/schedules/{schedule_id}/run", methods=["POST"])
def run_blast_schedule(req: func.HttpRequest) -> func.HttpResponse:
    """Manually trigger a scheduled BLAST job."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    schedule_id = req.route_params.get("schedule_id", "")
    if not schedule_id:
        return _error_response(400, "schedule_id required")

    client = df.DurableOrchestrationClient(req)

    # Read schedule config
    try:
        entity_id = df.EntityId("scheduled_blast_entity", "global")
        resp = client.read_entity_state(entity_id)
        schedules = resp.entity_state if resp.entity_exists else []
    except Exception:
        return _error_response(404, "no schedules found")

    target = next((s for s in schedules if s.get("schedule_id") == schedule_id), None)
    if not target:
        return _error_response(404, f"schedule {sanitise(schedule_id)} not found")

    # Submit a BLAST job using the schedule's params
    blast_params = target.get("blast_params", {})
    try:
        submit_req = BlastSubmitRequest(**blast_params)
    except ValidationError as exc:
        return _error_response(400, f"invalid schedule params: {exc.errors()[0]['msg']}")

    job_id = f"sched-{schedule_id}-{uuid.uuid4().hex[:8]}"
    payload = submit_req.model_dump()
    payload["job_id"] = job_id
    payload["user_assertion"] = identity.raw_token
    payload["owner_upn"] = identity.upn
    payload["owner_oid"] = identity.oid
    payload["schedule_id"] = schedule_id

    instance_id = client.start_new("submit_blast_orchestrator", None, payload)

    # Mark run
    client.signal_entity(entity_id, "mark_run", {"schedule_id": schedule_id})

    return _json_response(
        {
            "job_id": job_id,
            "instance_id": instance_id,
            "schedule_id": schedule_id,
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# P12 — Primer Design (Primer3 on Terminal VM)
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/primer-design", methods=["POST"])
def blast_primer_design(req: func.HttpRequest) -> func.HttpResponse:
    """Design PCR primers for a target region using Primer3 on the Terminal VM."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    sequence = body.get("sequence", "")
    if not sequence or len(sequence) < 50:
        return _error_response(400, "provide a sequence of at least 50 bp")
    if len(sequence) > 100000:
        return _error_response(400, "sequence too long (max 100 kb)")

    # Validate sequence content
    clean_seq = re.sub(r"\s", "", sequence).upper()
    if not re.match(r"^[ATGCNRYSWKMBDHVU]+$", clean_seq):
        return _error_response(400, "invalid nucleotide characters in sequence")

    target_start = int(body.get("target_start", max(1, len(clean_seq) // 4)))
    target_length = int(body.get("target_length", min(200, len(clean_seq) // 2)))
    product_min = int(body.get("product_size_min", 100))
    product_max = int(body.get("product_size_max", 1000))
    num_return = min(int(body.get("num_return", 5)), 10)

    sub = body.get("subscription_id", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)

    terminal_rg = body.get(
        "terminal_resource_group", os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal")
    )
    terminal_vm = body.get("terminal_vm_name", "vm-elb-terminal")

    cred = credential_for_caller(identity.raw_token)

    try:
        vm_ip = compute_svc.get_vm_public_ip(cred, sub, terminal_rg, terminal_vm)
        if not vm_ip:
            return _error_response(400, "Terminal VM not available")

        vault_url = os.environ.get("ELB_KEYVAULT_URL") or os.environ.get("KEY_VAULT_URI", "")
        if not vault_url:
            return _error_response(500, "Key Vault URL not configured")
        password = kv_svc.get_secret(cred, vault_url, f"vm-{terminal_vm}-password")

        from services.ssh_exec import run_ssh

        # Build Primer3 boulder-IO input
        primer3_input = f"""SEQUENCE_TEMPLATE={clean_seq}
SEQUENCE_TARGET={target_start},{target_length}
PRIMER_TASK=generic
PRIMER_PICK_LEFT_PRIMER=1
PRIMER_PICK_RIGHT_PRIMER=1
PRIMER_NUM_RETURN={num_return}
PRIMER_PRODUCT_SIZE_RANGE={product_min}-{product_max}
PRIMER_MIN_SIZE=18
PRIMER_OPT_SIZE=20
PRIMER_MAX_SIZE=25
PRIMER_MIN_TM=57.0
PRIMER_OPT_TM=60.0
PRIMER_MAX_TM=63.0
PRIMER_MIN_GC=40.0
PRIMER_MAX_GC=60.0
="""

        script = f"""set -euo pipefail
# Check if primer3_core is available
if ! command -v primer3_core &>/dev/null; then
  sudo apt-get update -qq && sudo apt-get install -y -qq primer3 2>&1 | tail -3
fi

# Run primer3
echo '{primer3_input}' | primer3_core 2>&1
"""
        output = run_ssh(vm_ip, password, script, timeout=120)

        # Parse Primer3 output
        primers = _parse_primer3_output(output)

        return _json_response(
            {
                "primers": primers,
                "target": {"start": target_start, "length": target_length},
                "product_size_range": f"{product_min}-{product_max}",
                "sequence_length": len(clean_seq),
            }
        )
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)[:500]))


def _parse_primer3_output(raw: str) -> list[dict]:
    """Parse Primer3 boulder-IO output into list of primer pair dicts."""
    lines = raw.strip().splitlines()
    kv: dict[str, str] = {}
    for line in lines:
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    primers: list[dict] = []
    i = 0
    while True:
        left_seq = kv.get(f"PRIMER_LEFT_{i}_SEQUENCE")
        right_seq = kv.get(f"PRIMER_RIGHT_{i}_SEQUENCE")
        if not left_seq:
            break

        left_pos = kv.get(f"PRIMER_LEFT_{i}", "")
        right_pos = kv.get(f"PRIMER_RIGHT_{i}", "")

        pair: dict = {
            "pair_index": i,
            "left_sequence": left_seq,
            "right_sequence": right_seq,
            "left_tm": _safe_float(kv.get(f"PRIMER_LEFT_{i}_TM")),
            "right_tm": _safe_float(kv.get(f"PRIMER_RIGHT_{i}_TM")),
            "left_gc": _safe_float(kv.get(f"PRIMER_LEFT_{i}_GC_PERCENT")),
            "right_gc": _safe_float(kv.get(f"PRIMER_RIGHT_{i}_GC_PERCENT")),
            "product_size": _safe_int(kv.get(f"PRIMER_PAIR_{i}_PRODUCT_SIZE")),
            "pair_penalty": _safe_float(kv.get(f"PRIMER_PAIR_{i}_PENALTY")),
        }
        if left_pos:
            parts = left_pos.split(",")
            pair["left_start"] = int(parts[0]) if parts else 0
            pair["left_length"] = int(parts[1]) if len(parts) > 1 else 0
        if right_pos:
            parts = right_pos.split(",")
            pair["right_start"] = int(parts[0]) if parts else 0
            pair["right_length"] = int(parts[1]) if len(parts) > 1 else 0

        primers.append(pair)
        i += 1

    return primers


def _safe_float(val: str | None) -> float | None:
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _safe_int(val: str | None) -> int | None:
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None

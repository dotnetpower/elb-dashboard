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
    terminal_rg = body.get("terminal_resource_group") or "rg-elb-terminal"
    terminal_vm = body.get("terminal_vm_name") or "vm-elb-terminal"
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

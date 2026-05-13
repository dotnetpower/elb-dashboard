"""BLAST job management routes — list, get, delete, cancel, results, export."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import azure.durable_functions as df
import azure.functions as func

from _http_utils import (
    _RE_BLOB_NAME,
    _RE_DB_NAME,
    _RE_STORAGE_ACCOUNT,
    _error_response,
    _json_response,
    _require_query,
    _validate_name,
    _validate_rg,
    _validate_sub,
)
from auth.token import AuthError, validate_bearer_token
from services import compute as compute_svc
from services import keyvault as kv_svc
from services import storage_data as storage_data_svc
from services.azure_clients import credential_for_caller
from services.sanitise import sanitise
from routes.blast import _toggle_public_access

LOGGER = logging.getLogger(__name__)

bp = df.Blueprint()

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



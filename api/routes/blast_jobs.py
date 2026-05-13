"""BLAST job management routes — list, get, delete, cancel, results, export."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import azure.durable_functions as df
import azure.functions as func
import requests as _requests

from _http_utils import (
    _RE_BLOB_NAME,
    _RE_DB_NAME,
    _RE_STORAGE_ACCOUNT,
    _RE_VM_NAME,
    _error_response,
    _json_response,
    _require_query,
    _validate_name,
    _validate_rg,
    _validate_sub,
    resolve_terminal_secret,
)
from auth.token import AuthError, validate_bearer_token
from routes.blast import _toggle_public_access
from services import compute as compute_svc
from services import network as network_svc
from services import storage_data as storage_data_svc
from services.azure_clients import credential_for_caller
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

MAX_LIST_STATUS_REFRESHES = 8
LIST_STATUS_REFRESH_TIMEOUT_SECONDS = 3
_RE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
_RE_CUSTOM_DB_TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()/-]{0,199}$")
MAX_CUSTOM_FASTA_BYTES = 20 * 1024 * 1024

bp = df.Blueprint()


def _normalise_job_registry_state(raw_jobs: object) -> list[dict]:
    if isinstance(raw_jobs, list):
        candidates = raw_jobs
    elif isinstance(raw_jobs, dict) and isinstance(raw_jobs.get("jobs"), list):
        candidates = raw_jobs["jobs"]
    else:
        LOGGER.warning("Unexpected job registry state type: %s", type(raw_jobs).__name__)
        return []
    return [job for job in candidates if isinstance(job, dict)]


async def _read_job_registry(client: df.DurableOrchestrationClient) -> list[dict]:
    entity_id = df.EntityId("job_registry_entity", "default")
    state = await client.read_entity_state(entity_id)
    raw_jobs = state.entity_state if state.entity_exists else []
    return _normalise_job_registry_state(raw_jobs)


def _validate_job_id_param(job_id: str | None) -> str | None:
    if not job_id or not _RE_JOB_ID.match(job_id):
        return None
    return job_id


async def _find_job_for_caller(
    client: df.DurableOrchestrationClient,
    job_id: str,
    caller_oid: str,
) -> tuple[dict | None, func.HttpResponse | None]:
    jobs = await _read_job_registry(client)
    job = next((item for item in jobs if item.get("job_id") == job_id), None)
    if not job:
        return None, _error_response(404, "job not found")
    owner_oid = job.get("owner_oid")
    if not owner_oid or owner_oid != caller_oid:
        return None, _error_response(403, "not authorized for this job")
    return job, None


def _validate_result_blob_name(blob_name: str, job_id: str) -> str | None:
    if ".." in blob_name or blob_name.startswith("/"):
        return "invalid blob_name"
    if not blob_name.startswith(f"{job_id}/"):
        return "blob_name must be under this job's result prefix"
    if err := _validate_name(blob_name, _RE_BLOB_NAME, "blob_name"):
        return err
    return None


@bp.route(route="blast/jobs", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def list_blast_jobs(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    all_jobs = await _read_job_registry(client)

    # C5: Filter jobs by owner — only return jobs belonging to the caller
    caller_oid = identity.object_id
    jobs = [j for j in all_jobs if j.get("owner_oid") == caller_oid]

    async def refresh_job_status(job: dict) -> None:
        instance_id = job.get("instance_id")
        if not instance_id or job.get("status") not in ("submitted", "uploading"):
            return
        try:
            orch = await asyncio.wait_for(
                client.get_status(instance_id, show_input=False),
                timeout=LIST_STATUS_REFRESH_TIMEOUT_SECONDS,
            )
            if orch and orch.runtime_status:
                rt = orch.runtime_status.name
                if rt == "Failed":
                    job["status"] = "failed"
                    job["phase"] = "error"
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

    refresh_candidates = [
        job
        for job in jobs
        if job.get("instance_id") and job.get("status") in ("submitted", "uploading")
    ][:MAX_LIST_STATUS_REFRESHES]
    if refresh_candidates:
        await asyncio.gather(*(refresh_job_status(job) for job in refresh_candidates))

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

    job_id = _validate_job_id_param(req.route_params.get("job_id"))
    if not job_id:
        return _error_response(400, "invalid job_id")

    job, err = await _find_job_for_caller(client, job_id, identity.object_id)
    if err:
        return err

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

    job_id = _validate_job_id_param(req.route_params.get("job_id"))
    if not job_id:
        return _error_response(400, "invalid job_id")

    # Get job details for deletion
    entity_id = df.EntityId("job_registry_entity", "default")
    job, err = await _find_job_for_caller(client, job_id, identity.object_id)
    if err:
        return err

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

    job_id = _validate_job_id_param(req.route_params.get("job_id"))
    if not job_id:
        return _error_response(400, "invalid job_id")

    entity_id = df.EntityId("job_registry_entity", "default")
    job, err = await _find_job_for_caller(client, job_id, identity.object_id)
    if err:
        return err

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
@bp.durable_client_input(client_name="client")
async def list_blast_results(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = _validate_job_id_param(req.route_params.get("job_id"))
    if not job_id:
        return _error_response(400, "invalid job_id")

    _, auth_err = await _find_job_for_caller(client, job_id, identity.object_id)
    if auth_err:
        return auth_err

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
            f"{job_id}/",
        )
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))
    return _json_response({"job_id": job_id, "files": blobs})


@bp.route(route="blast/jobs/{job_id}/results/download", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def download_blast_result(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = _validate_job_id_param(req.route_params.get("job_id"))
    if not job_id:
        return _error_response(400, "invalid job_id")

    _, auth_err = await _find_job_for_caller(client, job_id, identity.object_id)
    if auth_err:
        return auth_err

    params, err = _require_query(req, "subscription_id", "storage_account", "blob_name")
    if err:
        return err
    # C7: Validate blob_name — reject path traversal
    blob_name = params["blob_name"]
    if err := _validate_result_blob_name(blob_name, job_id):
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
    title = body.get("title") or db_name
    if not isinstance(title, str) or not _RE_CUSTOM_DB_TITLE.match(title):
        return _error_response(400, "invalid title")

    fasta_data = body.get("fasta_data")
    fasta_blob_url = body.get("fasta_blob_url")
    if not fasta_data and not fasta_blob_url:
        return _error_response(400, "provide fasta_data or fasta_blob_url")
    if fasta_data and fasta_blob_url:
        return _error_response(400, "provide fasta_data OR fasta_blob_url, not both")
    if fasta_data and not isinstance(fasta_data, str):
        return _error_response(400, "fasta_data must be a string")
    if isinstance(fasta_data, str) and len(fasta_data.encode("utf-8")) > MAX_CUSTOM_FASTA_BYTES:
        return _error_response(400, "fasta_data too large (max 20 MB)")
    if fasta_blob_url and not isinstance(fasta_blob_url, str):
        return _error_response(400, "fasta_blob_url must be a blob path")
    if fasta_blob_url and not _RE_BLOB_NAME.match(fasta_blob_url):
        return _error_response(400, "invalid fasta_blob_url")

    cred = credential_for_caller(identity.raw_token)

    terminal_rg = body.get(
        "terminal_resource_group", os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal")
    )
    terminal_vm = body.get("terminal_vm_name", "vm-elb-terminal")
    if err := _validate_rg(terminal_rg):
        return _error_response(400, err)
    if err := _validate_name(terminal_vm, _RE_VM_NAME, "terminal_vm_name"):
        return _error_response(400, err)

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

        # Step 2: Run makeblastdb on the VM via SSH, with Azure Run Command as fallback.
        secret_name = f"vm-{terminal_vm}-password"
        password, _ = resolve_terminal_secret(cred, sub, terminal_rg, terminal_vm, secret_name)
        if password:
            try:
                network_svc.ensure_ssh_from_function_app(
                    cred,
                    sub,
                    terminal_rg,
                    f"nsg-{terminal_vm}",
                )
            except Exception:
                LOGGER.debug("failed to ensure Function App SSH NSG rule", exc_info=True)

        safe_db_name = db_name.replace("'", "")
        safe_title = title.replace("'", "").replace('"', "")
        safe_db_type = db_type

        script = f"""set -euo pipefail
export HOME=/home/azureuser
if ! az account show -o none 2>/dev/null; then az login --identity -o none 2>/dev/null || true; fi

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
        output = compute_svc.run_shell(
            cred,
            sub,
            terminal_rg,
            terminal_vm,
            script,
            max_retries=3,
            ssh_password=password,
        )

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
            LOGGER.warning(
                "Could not re-disable public access on %s after custom DB build", storage_account
            )


# ---------------------------------------------------------------------------
# BLAST — results aggregation / analytics
# ---------------------------------------------------------------------------
@bp.route(route="blast/jobs/{job_id}/results/aggregate", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def blast_results_aggregate(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Parse BLAST tabular output (outfmt 7) and return aggregated statistics.

    Returns: hit count, unique subject count, E-value distribution,
    identity % distribution, taxonomy breakdown (from subject IDs).
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = _validate_job_id_param(req.route_params.get("job_id"))
    if not job_id:
        return _error_response(400, "invalid job_id")

    _, auth_err = await _find_job_for_caller(client, job_id, identity.object_id)
    if auth_err:
        return auth_err

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
@bp.durable_client_input(client_name="client")
async def blast_results_alignments(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
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

    job_id = _validate_job_id_param(req.route_params.get("job_id"))
    if not job_id:
        return _error_response(400, "invalid job_id")

    _, auth_err = await _find_job_for_caller(client, job_id, identity.object_id)
    if auth_err:
        return auth_err

    params, err = _require_query(req, "subscription_id", "storage_account")
    if err:
        return err

    blob_name = req.params.get("blob_name", "")
    try:
        max_alignments = min(max(int(req.params.get("max_alignments", "50")), 1), 200)
    except ValueError:
        return _error_response(400, "invalid max_alignments")
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
            if err := _validate_result_blob_name(blob_name, job_id):
                return _error_response(400, err)

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
@bp.durable_client_input(client_name="client")
async def blast_results_export(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Export BLAST results as CSV or JSON for reports."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    job_id = _validate_job_id_param(req.route_params.get("job_id"))
    if not job_id:
        return _error_response(400, "invalid job_id")

    _, auth_err = await _find_job_for_caller(client, job_id, identity.object_id)
    if auth_err:
        return auth_err

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

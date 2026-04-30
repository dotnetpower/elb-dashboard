"""Azure Functions Python v2 entry point.

Registers HTTP triggers, the Durable Functions orchestrator, and activities.
All HTTP triggers are anonymous at the platform level — auth is enforced by
`auth.token.validate_bearer_token` so the SPA can use MSAL bearer tokens.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import azure.durable_functions as df
import azure.functions as func
from pydantic import ValidationError

from activities import blast as blast_activities
from activities import storage as storage_activities
from activities import terminal as terminal_activities
from auth.token import AuthError, validate_bearer_token
from entities import job_registry as _job_reg
from models.blast import BlastSubmitRequest
from models.terminal import HealthResponse, ProvisionTerminalRequest
from orchestrators import provision_terminal as _prov_term
from orchestrators import storage_window as _stor_win
from orchestrators import submit_blast as _sub_blast
from services import keyvault as kv_svc
from services import monitoring as monitoring_svc
from services import storage_data as storage_data_svc
from services.azure_clients import credential_for_caller

LOGGER = logging.getLogger(__name__)

app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _json_response(body: Any, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(body, default=str),
        status_code=status,
        mimetype="application/json",
    )


def _error_response(status: int, message: str) -> func.HttpResponse:
    return _json_response({"error": message}, status=status)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json_response(HealthResponse().model_dump())


# ---------------------------------------------------------------------------
# Whoami — returns the validated caller, useful for the SPA to render UPN
# ---------------------------------------------------------------------------
@app.route(route="me", methods=["GET"])
def whoami(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    return _json_response(
        {
            "object_id": identity.object_id,
            "tenant_id": identity.tenant_id,
            "upn": identity.upn,
        }
    )


# ---------------------------------------------------------------------------
# Remote Terminal — provisioning starter
# ---------------------------------------------------------------------------
@app.route(route="terminal/provision", methods=["POST"])
@app.durable_client_input(client_name="client")
async def start_provision_terminal(
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
        parsed = ProvisionTerminalRequest.model_validate(payload)
    except ValidationError as exc:
        return _error_response(400, exc.json())

    orchestration_input = {
        **parsed.model_dump(),
        "user_assertion": identity.raw_token,
        "owner_oid": identity.object_id,
    }
    instance_id = await client.start_new(
        "provision_terminal_orchestrator", None, orchestration_input
    )
    LOGGER.info("started provision_terminal_orchestrator instance=%s", instance_id)
    return client.create_check_status_response(req, instance_id)


@app.route(route="terminal/status/{instance_id}", methods=["GET"])
@app.durable_client_input(client_name="client")
async def get_provision_status(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    instance_id = req.route_params.get("instance_id")
    if not instance_id:
        return _error_response(400, "instance_id missing")
    status = await client.get_status(instance_id, show_input=False)
    if status is None:
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


@app.route(route="terminal/{vm_name}/password", methods=["GET"])
def reveal_terminal_password(req: func.HttpRequest) -> func.HttpResponse:
    """One-shot reveal of the VM admin password from Key Vault.

    The SPA is expected to display this exactly once and not persist it. The
    caller's identity is enforced via OBO — only users with KV Secrets User
    on the vault can read it.
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    vm_name = req.route_params.get("vm_name")
    if not vm_name:
        return _error_response(400, "vm_name missing")
    vault_uri = os.environ.get("KEY_VAULT_URI")
    if not vault_uri:
        return _error_response(500, "KEY_VAULT_URI not configured")
    cred = credential_for_caller(identity.raw_token)
    try:
        password = kv_svc.get_secret(cred, vault_uri, f"vm-{vm_name}-password")
    except Exception as exc:
        LOGGER.warning("secret read failed for vm=%s: %s", vm_name, exc)
        return _error_response(404, "password secret not found")
    return _json_response({"vm_name": vm_name, "password": password})


# ---------------------------------------------------------------------------
# Monitoring (read-only)
# ---------------------------------------------------------------------------


def _require_query(
    req: func.HttpRequest, *names: str
) -> tuple[dict[str, str] | None, func.HttpResponse | None]:
    values: dict[str, str] = {}
    for name in names:
        v = req.params.get(name)
        if not v:
            return None, _error_response(400, f"missing query param '{name}'")
        values[name] = v
    return values, None


@app.route(route="monitor/aks", methods=["GET"])
def monitor_aks(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    clusters = monitoring_svc.list_aks_clusters(
        cred, params["subscription_id"], params["resource_group"]
    )
    return _json_response({"clusters": clusters})


@app.route(route="monitor/storage", methods=["GET"])
def monitor_storage(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "account_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    summary = monitoring_svc.get_storage_summary(
        cred, params["subscription_id"], params["resource_group"], params["account_name"]
    )
    return _json_response(summary)


@app.route(route="monitor/storage/public-access", methods=["POST"])
def toggle_storage_public_access(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    try:
        body = json.loads(req.get_body() or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")
    required = {"subscription_id", "resource_group", "account_name", "enabled"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")
    cred = credential_for_caller(identity.raw_token)
    result = monitoring_svc.set_storage_public_access(
        cred,
        body["subscription_id"],
        body["resource_group"],
        body["account_name"],
        bool(body["enabled"]),
    )
    LOGGER.info(
        "storage public-access toggled by oid=%s account=%s -> %s",
        identity.object_id,
        body["account_name"],
        result.get("public_network_access"),
    )
    return _json_response(result)


@app.route(route="monitor/storage/public-access/window", methods=["POST"])
@app.durable_client_input(client_name="client")
async def start_storage_public_access_window(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Enable public access for a bounded TTL, then auto-disable.

    Request body: subscription_id, resource_group, account_name, ttl_seconds (optional).
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    try:
        body = json.loads(req.get_body() or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")
    required = {"subscription_id", "resource_group", "account_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")
    payload = {
        **body,
        "user_assertion": identity.raw_token,
        "owner_oid": identity.object_id,
    }
    instance_id = await client.start_new("storage_public_access_window_orchestrator", None, payload)
    return client.create_check_status_response(req, instance_id)


@app.route(route="monitor/acr", methods=["GET"])
def monitor_acr(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "registry_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    summary = monitoring_svc.list_acr_repositories(
        cred, params["subscription_id"], params["resource_group"], params["registry_name"]
    )
    return _json_response(summary)


@app.route(route="monitor/terminal", methods=["GET"])
def monitor_terminal(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "vm_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    status = monitoring_svc.get_vm_status(
        cred, params["subscription_id"], params["resource_group"], params["vm_name"]
    )
    return _json_response(status)


# ---------------------------------------------------------------------------
# BLAST — job submission
# ---------------------------------------------------------------------------
@app.route(route="blast/submit", methods=["POST"])
@app.durable_client_input(client_name="client")
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
            "owner_oid": identity.object_id,
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


@app.route(route="blast/submit/{instance_id}/status", methods=["GET"])
@app.durable_client_input(client_name="client")
async def get_blast_submit_status(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    instance_id = req.route_params.get("instance_id")
    if not instance_id:
        return _error_response(400, "instance_id missing")
    status = await client.get_status(instance_id, show_input=False)
    if status is None:
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
# BLAST — query upload
# ---------------------------------------------------------------------------
@app.route(route="blast/upload-query", methods=["POST"])
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
@app.route(route="blast/jobs", methods=["GET"])
@app.durable_client_input(client_name="client")
async def list_blast_jobs(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    entity_id = df.EntityId("job_registry_entity", "default")
    state = await client.read_entity_state(entity_id)
    jobs = state.entity_state if state.entity_exists else []
    return _json_response({"jobs": jobs or []})


@app.route(route="blast/jobs/{job_id}", methods=["GET"])
@app.durable_client_input(client_name="client")
async def get_blast_job(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    try:
        validate_bearer_token(req.headers.get("Authorization"))
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

    # Enrich with orchestrator status if instance_id is known
    instance_id = job.get("instance_id")
    if instance_id:
        orch_status = await client.get_status(instance_id, show_input=False)
        if orch_status:
            job["runtime_status"] = orch_status.runtime_status.name
            job["custom_status"] = orch_status.custom_status
            job["output"] = orch_status.output

    return _json_response(job)


@app.route(route="blast/jobs/{job_id}", methods=["DELETE"])
@app.durable_client_input(client_name="client")
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

    # Update status
    await client.signal_entity(
        entity_id,
        "update_job",
        {
            "job_id": job_id,
            "status": "deleting",
            "phase": "deleting",
        },
    )

    return _json_response({"job_id": job_id, "status": "deleting"})


# ---------------------------------------------------------------------------
# BLAST — results
# ---------------------------------------------------------------------------
@app.route(route="blast/jobs/{job_id}/results", methods=["GET"])
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

    cred = credential_for_caller(identity.raw_token)
    blobs = storage_data_svc.list_result_blobs(
        cred,
        params["storage_account"],
        "results",
        job_id,
    )
    return _json_response({"job_id": job_id, "files": blobs})


@app.route(route="blast/jobs/{job_id}/results/download", methods=["GET"])
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

    cred = credential_for_caller(identity.raw_token)
    url = storage_data_svc.generate_download_url(
        cred,
        params["storage_account"],
        "results",
        params["blob_name"],
    )
    return _json_response({"download_url": url})


# ---------------------------------------------------------------------------
# BLAST — database listing
# ---------------------------------------------------------------------------
@app.route(route="blast/databases", methods=["GET"])
def list_blast_databases(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    params, err = _require_query(req, "subscription_id", "storage_account")
    if err:
        return err

    cred = credential_for_caller(identity.raw_token)
    dbs = storage_data_svc.list_databases(
        cred,
        params["storage_account"],
    )
    return _json_response({"databases": dbs})


# ---------------------------------------------------------------------------
# Durable orchestrator + activity + entity registrations
# ---------------------------------------------------------------------------
@app.orchestration_trigger(context_name="context")
def provision_terminal_orchestrator(context):
    return _prov_term.provision_terminal_orchestrator(context)


@app.orchestration_trigger(context_name="context")
def storage_public_access_window_orchestrator(context):
    return _stor_win.storage_public_access_window_orchestrator(context)


@app.orchestration_trigger(context_name="context")
def submit_blast_orchestrator(context):
    return _sub_blast.submit_blast_orchestrator(context)


@app.entity_trigger(context_name="context")
def job_registry_entity(context):
    return _job_reg.job_registry_entity(context)


@app.activity_trigger(input_name="payload")
def ensure_resource_group_activity(payload: dict) -> dict:
    return terminal_activities.activity_ensure_resource_group(payload)


@app.activity_trigger(input_name="payload")
def ensure_network_activity(payload: dict) -> dict:
    return terminal_activities.activity_ensure_network(payload)


@app.activity_trigger(input_name="payload")
def generate_password_activity(payload: dict) -> dict:
    return terminal_activities.activity_generate_password(payload)


@app.activity_trigger(input_name="payload")
def create_vm_activity(payload: dict) -> dict:
    return terminal_activities.activity_create_vm(payload)


@app.activity_trigger(input_name="payload")
def check_cloud_init_activity(payload: dict) -> dict:
    return terminal_activities.activity_check_cloud_init(payload)


@app.activity_trigger(input_name="payload")
def set_storage_public_access_activity(payload: dict) -> dict:
    return storage_activities.activity_set_storage_public_access(payload)


# BLAST activities
@app.activity_trigger(input_name="payload")
def upload_query_activity(payload: dict) -> dict:
    return blast_activities.activity_upload_query(payload)


@app.activity_trigger(input_name="payload")
def generate_blast_config_activity(payload: dict) -> dict:
    return blast_activities.activity_generate_blast_config(payload)


@app.activity_trigger(input_name="payload")
def run_elastic_blast_submit_activity(payload: dict) -> dict:
    return blast_activities.activity_run_elastic_blast_submit(payload)


@app.activity_trigger(input_name="payload")
def check_blast_status_activity(payload: dict) -> dict:
    return blast_activities.activity_check_blast_status(payload)


@app.activity_trigger(input_name="payload")
def run_elastic_blast_delete_activity(payload: dict) -> dict:
    return blast_activities.activity_run_elastic_blast_delete(payload)


@app.activity_trigger(input_name="payload")
def list_result_blobs_activity(payload: dict) -> dict:
    return blast_activities.activity_list_result_blobs(payload)


@app.activity_trigger(input_name="payload")
def list_databases_activity(payload: dict) -> dict:
    return blast_activities.activity_list_databases(payload)

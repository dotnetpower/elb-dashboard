"""Azure Functions Python v2 entry point.

Registers HTTP triggers, the Durable Functions orchestrator, and activities.
All HTTP triggers are anonymous at the platform level — auth is enforced by
`auth.token.validate_bearer_token` so the SPA can use MSAL bearer tokens.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import azure.durable_functions as df
import azure.functions as func
from pydantic import ValidationError

from activities import storage as storage_activities
from activities import terminal as terminal_activities
from auth.token import AuthError, validate_bearer_token
from models.terminal import HealthResponse, ProvisionTerminalRequest
from orchestrators import provision_terminal as provision_terminal_module
from orchestrators import storage_window as storage_window_module
from services import keyvault as kv_svc
from services import monitoring as monitoring_svc
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
# Durable orchestrator + activity registrations
# ---------------------------------------------------------------------------
app.orchestration_trigger(context_name="context")(
    provision_terminal_module.provision_terminal_orchestrator
)
app.orchestration_trigger(context_name="context")(
    storage_window_module.storage_public_access_window_orchestrator
)


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

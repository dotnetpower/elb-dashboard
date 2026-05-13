"""Remote Terminal HTTP routes (provision / status / lifecycle / inspection).

All routes are gated by `auth.token.validate_bearer_token`. Side-effecting
calls run under the Function App MI; per-user authorization is enforced at
the JWT layer plus Azure RBAC on each downstream resource.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import azure.durable_functions as df
import azure.functions as func
from pydantic import ValidationError

from _http_utils import (
    _RE_INSTANCE_ID,
    _RE_VM_NAME,
    _error_response,
    _json_response,
    _validate_ip,
    _validate_name,
    _validate_rg,
    _validate_sub,
    resolve_terminal_secret,
)
from auth.token import AuthError, validate_bearer_token
from models.terminal import ProvisionTerminalRequest
from services import compute as compute_svc
from services import keyvault as kv_svc
from services import network as network_svc
from services.azure_clients import credential_for_caller
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

bp = df.Blueprint()


def _terminal_rg(req: func.HttpRequest) -> str:
    return req.params.get("resource_group") or os.environ.get(
        "TERMINAL_DEFAULT_RG", "rg-elb-terminal"
    )


# ---------------------------------------------------------------------------
# Provisioning starter & status
# ---------------------------------------------------------------------------
@bp.route(route="terminal/provision", methods=["POST"])
@bp.durable_client_input(client_name="client")
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


@bp.route(route="terminal/status/{instance_id}", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def get_provision_status(
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
# VM-scoped helpers
# ---------------------------------------------------------------------------
@bp.route(route="terminal/{vm_name}/password", methods=["GET"])
def reveal_terminal_password(req: func.HttpRequest) -> func.HttpResponse:
    """One-shot reveal of the VM admin password from Key Vault.

    Resolves the vault name with `_default_vault_name` (the same helper used
    by the provision activity). Older clients that only know the legacy
    ``kv-elb-{vm[-8:]}`` pattern are still served when subscription_id /
    resource_group are not supplied — fallback only.
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    vm_name = req.route_params.get("vm_name")
    if not vm_name:
        return _error_response(400, "vm_name missing")
    if err := _validate_name(vm_name, _RE_VM_NAME, "vm_name"):
        return _error_response(400, err)
    sub = req.params.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    rg = req.params.get("resource_group") or os.environ.get("TERMINAL_DEFAULT_RG", "")
    cred = credential_for_caller(identity.raw_token)

    secret_name = f"vm-{vm_name}-password"
    password, _ = resolve_terminal_secret(cred, sub, rg, vm_name, secret_name)
    if password:
        return _json_response({"vm_name": vm_name, "password": password})
    return _error_response(404, "password secret not found")


@bp.route(route="terminal/{vm_name}/open-ssh", methods=["POST"])
def open_ssh_port(req: func.HttpRequest) -> func.HttpResponse:
    """Add an NSG rule to allow SSH from the caller's IP."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    vm_name = req.route_params.get("vm_name")
    if not vm_name:
        return _error_response(400, "vm_name missing")
    if err := _validate_name(vm_name, _RE_VM_NAME, "vm_name"):
        return _error_response(400, err)
    rg = _terminal_rg(req)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    sub = req.params.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    caller_ip = req.headers.get("X-Azure-SocketIP", "")
    if not caller_ip:
        caller_ip = req.params.get("caller_ip", "")
    if not caller_ip:
        forwarded = req.headers.get("X-Forwarded-For", "")
        caller_ip = forwarded.split(",")[0].strip() if forwarded else ""
    if not caller_ip:
        return _error_response(400, "caller_ip query param required")
    if err := _validate_ip(caller_ip):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)
    nsg_name = f"nsg-{vm_name}"
    try:
        network_svc.create_ssh_rule(cred, sub, rg, nsg_name, caller_ip)
        LOGGER.info("NSG rule AllowSSH created for %s from %s", nsg_name, caller_ip)
        return _json_response({"ok": True, "nsg": nsg_name, "allowed_ip": caller_ip})
    except Exception as exc:
        LOGGER.warning("Failed to create NSG rule: %s", exc)
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="terminal/{vm_name}/stop", methods=["POST"])
def stop_terminal_vm(req: func.HttpRequest) -> func.HttpResponse:
    """Deallocate (stop) the terminal VM to save costs."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    vm_name = req.route_params.get("vm_name")
    if not vm_name:
        return _error_response(400, "vm_name missing")
    if err := _validate_name(vm_name, _RE_VM_NAME, "vm_name"):
        return _error_response(400, err)
    rg = _terminal_rg(req)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    sub = req.params.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)
    try:
        compute_svc.deallocate_vm(cred, sub, rg, vm_name)
        LOGGER.info("VM %s deallocated in %s", vm_name, rg)
        return _json_response({"ok": True, "vm_name": vm_name, "status": "deallocated"})
    except Exception as exc:
        LOGGER.warning("Failed to stop VM %s: %s", vm_name, exc)
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="terminal/{vm_name}/start", methods=["POST"])
def start_terminal_vm(req: func.HttpRequest) -> func.HttpResponse:
    """Start a deallocated terminal VM."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    vm_name = req.route_params.get("vm_name")
    if not vm_name:
        return _error_response(400, "vm_name missing")
    if err := _validate_name(vm_name, _RE_VM_NAME, "vm_name"):
        return _error_response(400, err)
    rg = _terminal_rg(req)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    sub = req.params.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)
    try:
        from azure.mgmt.compute import ComputeManagementClient
        cc = ComputeManagementClient(cred, sub)
        cc.virtual_machines.begin_start(rg, vm_name).result()
        LOGGER.info("VM %s started in %s", vm_name, rg)
        return _json_response({"ok": True, "vm_name": vm_name, "status": "running"})
    except Exception as exc:
        LOGGER.warning("Failed to start VM %s: %s", vm_name, exc)
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="terminal/{vm_name}/health", methods=["GET"])
def terminal_health_check(req: func.HttpRequest) -> func.HttpResponse:
    """Check managed identity login status and installed tool versions on the terminal VM."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    vm_name = req.route_params.get("vm_name")
    if not vm_name:
        return _error_response(400, "vm_name missing")
    if err := _validate_name(vm_name, _RE_VM_NAME, "vm_name"):
        return _error_response(400, err)
    rg = _terminal_rg(req)
    sub = req.params.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    cred = credential_for_caller(identity.raw_token)
    script = "\n".join(
        [
            "#!/bin/bash",
            "sudo -Hu azureuser bash -lc '",
            "  export AZCOPY_AUTO_LOGIN_TYPE=MSI",
            "  if command -v elb-az-login-mi >/dev/null 2>&1; then",
            "    elb-az-login-mi >/tmp/elb-az-login-mi.$(id -u).log 2>&1 || true",
            "  elif ! az account show -o none 2>/dev/null; then",
            "    az login --identity --allow-no-subscriptions -o none >/dev/null 2>&1 || true",
            "  fi",
            "' >/dev/null 2>&1 || true",
            "AZ_VERSION=$(az version -o tsv --query '\"azure-cli\"' 2>/dev/null || true)",
            "[ -n \"$AZ_VERSION\" ] || AZ_VERSION='not installed'",
            "echo AZ_VERSION=$AZ_VERSION",
            "kubectl version --client -o yaml >/tmp/kubectl-version.yaml 2>/dev/null || true",
            "KUBECTL_VERSION=$(awk '/gitVersion/ {print $2; exit}' /tmp/kubectl-version.yaml)",
            "if [ -z \"$KUBECTL_VERSION\" ]; then",
            "  KUBECTL_VERSION=$(kubectl version --client 2>/dev/null | head -1 || true)",
            "fi",
            "[ -n \"$KUBECTL_VERSION\" ] || KUBECTL_VERSION='not installed'",
            "echo KUBECTL_VERSION=$KUBECTL_VERSION",
            "echo AZCOPY_VERSION=$(azcopy --version 2>/dev/null | head -1 || echo 'not installed')",
            "echo PYTHON_VERSION=$(python3.11 --version 2>/dev/null || echo 'not installed')",
            "AZ_LOGIN_USER=$(sudo -Hu azureuser bash -lc '",
            "  az account show --query user.name -o tsv 2>/dev/null || true",
            "')",
            "if [ -n \"$AZ_LOGIN_USER\" ]; then",
            "  echo AZ_LOGIN_OK=1",
            "  echo AZ_LOGIN_USER=$AZ_LOGIN_USER",
            "else",
            "  echo AZ_LOGIN_OK=0",
            "  echo AZ_LOGIN_USER=none",
            "fi",
            "if [ -f /home/azureuser/.azure/azureProfile.json ]; then",
            "  MTIME=$(stat -c %Y /home/azureuser/.azure/azureProfile.json 2>/dev/null || echo 0)",
            "  NOW=$(date +%s)",
            "  AGE=$((NOW - MTIME))",
            "  echo AZ_LOGIN_AGE_SECONDS=$AGE",
            "else",
            "  echo AZ_LOGIN_AGE_SECONDS=-1",
            "fi",
            "",
        ]
    )
    try:
        output = compute_svc.run_shell(cred, sub, rg, vm_name, script)
        result: dict[str, Any] = {}
        for line in output.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
        age_str = result.get("AZ_LOGIN_AGE_SECONDS", "-1")
        try:
            age = int(age_str)
        except ValueError:
            age = -1
        az_login_active = result.get("AZ_LOGIN_OK") == "1"
        return _json_response({
            "az_cli": result.get("AZ_VERSION", "unknown"),
            "kubectl": result.get("KUBECTL_VERSION", "unknown"),
            "azcopy": result.get("AZCOPY_VERSION", "unknown"),
            "python": result.get("PYTHON_VERSION", "unknown"),
            "az_login_active": az_login_active,
            "az_login_user": result.get("AZ_LOGIN_USER", "unknown"),
            "az_login_age_seconds": age,
        })
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="terminal/{vm_name}/destroy", methods=["POST"])
def destroy_terminal_vm(req: func.HttpRequest) -> func.HttpResponse:
    """Destroy the terminal VM and all associated resources (NIC, disk, PIP, KV secret)."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    vm_name = req.route_params.get("vm_name")
    if not vm_name:
        return _error_response(400, "vm_name missing")
    if err := _validate_name(vm_name, _RE_VM_NAME, "vm_name"):
        return _error_response(400, err)
    rg = _terminal_rg(req)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    sub = req.params.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    delete_rg = req.params.get("delete_rg", "false").lower() == "true"

    cred = credential_for_caller(identity.raw_token)
    errors: list[str] = []

    try:
        compute_svc.delete_vm(cred, sub, rg, vm_name)
    except Exception as exc:
        errors.append(f"VM: {sanitise(str(exc))}")

    for resource_type, name_template in [
        ("nic", f"nic-{vm_name}"),
        ("pip", f"pip-{vm_name}"),
        ("nsg", f"nsg-{vm_name}"),
    ]:
        try:
            network_svc.delete_resource(cred, sub, rg, resource_type, name_template)
        except Exception as exc:
            if "not found" not in str(exc).lower():
                errors.append(f"{resource_type}: {sanitise(str(exc))}")

    candidate_vaults: list[str] = []
    env_uri = os.environ.get("KEY_VAULT_URI")
    if env_uri:
        candidate_vaults.append(env_uri.rstrip("/") + "/")
    try:
        from activities.terminal import _default_vault_name
        canonical = _default_vault_name(sub, rg, vm_name)
        candidate_vaults.append(f"https://{canonical}.vault.azure.net/")
    except Exception:
        LOGGER.debug("Could not compute canonical terminal Key Vault name", exc_info=True)
    legacy_suffix = vm_name[-8:] if len(vm_name) >= 8 else vm_name
    candidate_vaults.append(f"https://kv-elb-{legacy_suffix}.vault.azure.net/")
    for vault_uri in candidate_vaults:
        try:
            kv_svc.delete_secret(cred, vault_uri, f"vm-{vm_name}-password")
            break
        except Exception:
            LOGGER.debug("Could not delete terminal password from %s", vault_uri, exc_info=True)
            continue

    if delete_rg:
        try:
            network_svc.delete_resource_group(cred, sub, rg)
        except Exception as exc:
            errors.append(f"RG: {sanitise(str(exc))}")

    LOGGER.info("Terminal VM %s destroy: errors=%s", vm_name, errors or "none")
    return _json_response({
        "vm_name": vm_name,
        "status": "destroyed" if not errors else "partial",
        "errors": errors,
    })

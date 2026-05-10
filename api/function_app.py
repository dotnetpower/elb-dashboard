"""Azure Functions Python v2 entry point.

Registers HTTP triggers, the Durable Functions orchestrator, and activities.
All HTTP triggers are anonymous at the platform level — auth is enforced by
`auth.token.validate_bearer_token` so the SPA can use MSAL bearer tokens.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from typing import Any

import azure.durable_functions as df
import azure.functions as func
import requests as _requests
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
from orchestrators import delete_blast as _del_blast
from services import keyvault as kv_svc
from services.sanitise import sanitise
from services import monitoring as monitoring_svc
from services import storage_data as storage_data_svc
from services import compute as compute_svc
from services import network as network_svc
from services.azure_clients import credential_for_caller

LOGGER = logging.getLogger(__name__)

app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ---------------------------------------------------------------------------
# Input validation patterns (Azure naming rules)
# ---------------------------------------------------------------------------
_RE_RESOURCE_GROUP = re.compile(r"^[-\w._()]{1,90}$")
_RE_VM_NAME = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9]{0,62}[a-zA-Z0-9]?$")
_RE_STORAGE_ACCOUNT = re.compile(r"^[a-z0-9]{3,24}$")
_RE_ACR_NAME = re.compile(r"^[a-zA-Z0-9]{5,50}$")
_RE_CLUSTER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$")
_RE_DB_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")
_RE_SUBSCRIPTION = re.compile(r"^[0-9a-fA-F-]{36}$")
_RE_BLOB_NAME = re.compile(r"^[^/][a-zA-Z0-9._/-]{0,1024}$")


def _validate_name(value: str, pattern: re.Pattern[str], label: str) -> str | None:
    """Return an error message if value doesn't match pattern, else None."""
    if not value:
        return f"{label} is required"
    if not pattern.match(value):
        return f"Invalid {label}: '{sanitise(value[:40])}'"
    return None


def _validate_ip(value: str) -> str | None:
    """Validate IPv4 address format."""
    try:
        ipaddress.ip_address(value)
        return None
    except ValueError:
        return f"Invalid IP address: '{sanitise(value[:40])}'"


def _validate_sub(value: str) -> str | None:
    """Validate subscription ID format."""
    return _validate_name(value, _RE_SUBSCRIPTION, "subscription_id")


def _validate_rg(value: str) -> str | None:
    """Validate resource group name format."""
    return _validate_name(value, _RE_RESOURCE_GROUP, "resource_group")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_RE_INSTANCE_ID = re.compile(r"^[a-zA-Z0-9]{16,64}$")


def _json_response(body: Any, status: int = 200) -> func.HttpResponse:
    resp = func.HttpResponse(
        json.dumps(body, default=str),
        status_code=status,
        mimetype="application/json; charset=utf-8",
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    if err := _validate_name(vm_name, _RE_VM_NAME, "vm_name"):
        return _error_response(400, err)
    vault_uri = os.environ.get("KEY_VAULT_URI")
    if not vault_uri:
        vault_suffix = vm_name[-8:] if len(vm_name) >= 8 else vm_name
        vault_uri = f"https://kv-elb-{vault_suffix}.vault.azure.net/"
    cred = credential_for_caller(identity.raw_token)
    try:
        password = kv_svc.get_secret(cred, vault_uri, f"vm-{vm_name}-password")
    except Exception as exc:
        LOGGER.warning("secret read failed for vm=%s: %s", vm_name, exc)
        return _error_response(404, "password secret not found")
    return _json_response({"vm_name": vm_name, "password": password})


@app.route(route="terminal/{vm_name}/open-ssh", methods=["POST"])
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
    rg = req.params.get("resource_group") or os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal")
    if err := _validate_rg(rg):
        return _error_response(400, err)
    sub = req.params.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    # Use platform-injected client IP, fallback to X-Forwarded-For
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


@app.route(route="terminal/{vm_name}/stop", methods=["POST"])
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
    rg = req.params.get("resource_group") or os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal")
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


@app.route(route="terminal/{vm_name}/start", methods=["POST"])
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
    rg = req.params.get("resource_group") or os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal")
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


@app.route(route="terminal/{vm_name}/health", methods=["GET"])
def terminal_health_check(req: func.HttpRequest) -> func.HttpResponse:
    """Check az login status and installed tool versions on the terminal VM."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    vm_name = req.route_params.get("vm_name")
    if not vm_name:
        return _error_response(400, "vm_name missing")
    if err := _validate_name(vm_name, _RE_VM_NAME, "vm_name"):
        return _error_response(400, err)
    rg = req.params.get("resource_group") or os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal")
    sub = req.params.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    cred = credential_for_caller(identity.raw_token)
    script = (
        "#!/bin/bash\n"
        "echo AZ_VERSION=$(az version -o tsv --query '\"azure-cli\"' 2>/dev/null || echo 'not installed')\n"
        "echo KUBECTL_VERSION=$(kubectl version --client --short 2>/dev/null | head -1 || echo 'not installed')\n"
        "echo AZCOPY_VERSION=$(azcopy --version 2>/dev/null | head -1 || echo 'not installed')\n"
        "echo PYTHON_VERSION=$(python3.11 --version 2>/dev/null || echo 'not installed')\n"
        "if [ -f /home/azureuser/.azure/azureProfile.json ]; then\n"
        "  MTIME=$(stat -c %Y /home/azureuser/.azure/azureProfile.json 2>/dev/null || echo 0)\n"
        "  NOW=$(date +%s)\n"
        "  AGE=$((NOW - MTIME))\n"
        "  echo AZ_LOGIN_AGE_SECONDS=$AGE\n"
        "  echo AZ_LOGIN_USER=$(cat /home/azureuser/.azure/azureProfile.json 2>/dev/null | python3 -c 'import sys,json; subs=json.load(sys.stdin).get(\"subscriptions\",[]); print(subs[0].get(\"user\",{}).get(\"name\",\"unknown\") if subs else \"none\")' 2>/dev/null || echo 'unknown')\n"
        "else\n"
        "  echo AZ_LOGIN_AGE_SECONDS=-1\n"
        "  echo AZ_LOGIN_USER=none\n"
        "fi\n"
    )
    try:
        output = compute_svc.run_shell(cred, sub, rg, vm_name, script)
        result: dict[str, Any] = {}
        for line in output.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
        # Parse az login age
        age_str = result.get("AZ_LOGIN_AGE_SECONDS", "-1")
        try:
            age = int(age_str)
        except ValueError:
            age = -1
        az_login_active = 0 < age < 86400  # Active if profile updated in last 24h
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


# ---------------------------------------------------------------------------
# Monitoring (read-only)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ACR Image Build & Storage DB Prepare (long-running)
# ---------------------------------------------------------------------------

@app.route(route="acr/build-images", methods=["POST"])
def build_acr_images(req: func.HttpRequest) -> func.HttpResponse:
    """Build ElasticBLAST images in ACR via ACR Build Tasks.

    Schedules builds and returns immediately with run IDs. The UI polls
    monitor/acr to track build status — no HTTP thread blocking.
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    body = json.loads(raw.decode("utf-8"))
    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    registry = body.get("registry_name", "")
    if not all([sub, rg, registry]):
        return _error_response(400, "subscription_id, resource_group, registry_name required")
    # #7-9: Validate inputs
    if err := _validate_sub(sub):
        return _error_response(400, err)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    if err := _validate_name(registry, _RE_ACR_NAME, "registry_name"):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)

    from services.image_tags import IMAGE_TAGS, IMAGE_BUILD_INFO, SOURCE_REPO, SOURCE_BRANCH
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient
    from azure.mgmt.containerregistry.models import (
        DockerBuildRequest,
        PlatformProperties,
        OS,
    )

    acr = ContainerRegistryManagementClient(cred, sub, api_version="2019-06-01-preview")
    results = []
    for image, tag in IMAGE_TAGS.items():
        full_image = f"{image}:{tag}"
        build_info = IMAGE_BUILD_INFO.get(image, {})
        context_path = build_info.get("context", "")
        dockerfile = build_info.get("dockerfile", "Dockerfile")
        if context_path:
            source_location = f"{SOURCE_REPO}#{SOURCE_BRANCH}:{context_path}"
        else:
            source_location = f"{SOURCE_REPO}#{SOURCE_BRANCH}"
        LOGGER.info("Scheduling ACR build for %s (source: %s, dockerfile: %s)", full_image, source_location, dockerfile)
        try:
            pre_build = build_info.get("pre_build_cmd", "")
            if pre_build:
                task_yaml = f"""version: v1.1.0
steps:
  - cmd: bash -c "{pre_build}"
  - build: -f {dockerfile} -t $Registry/{full_image} .
  - push:
    - $Registry/{full_image}
"""
                from azure.mgmt.containerregistry.models import EncodedTaskRunRequest
                build_req = EncodedTaskRunRequest(
                    encoded_task_content=base64.b64encode(task_yaml.encode()).decode(),
                    source_location=f"{SOURCE_REPO}#{SOURCE_BRANCH}",
                    platform=PlatformProperties(os=OS.LINUX),
                    timeout=3600,
                )
            else:
                build_req = DockerBuildRequest(
                    docker_file_path=dockerfile,
                    image_names=[full_image],
                    source_location=source_location,
                    is_push_enabled=True,
                    platform=PlatformProperties(os=OS.LINUX),
                    timeout=3600,
                )
            # #1 CRITICAL: Fire-and-forget — schedule build and return immediately
            poller = acr.registries.begin_schedule_run(rg, registry, build_req)
            run_result = poller.result()
            run_id = run_result.run_id or ""
            status = run_result.status or "Queued"
            results.append({"image": full_image, "status": "scheduled", "run_id": run_id, "acr_status": status})
        except Exception as exc:
            LOGGER.warning("ACR build schedule failed for %s: %s", full_image, exc)
            results.append({"image": full_image, "status": "failed", "error": sanitise(str(exc))})
    return _json_response({"results": results})


@app.route(route="storage/prepare-db", methods=["POST"])
def prepare_blast_db(req: func.HttpRequest) -> func.HttpResponse:
    """Download BLAST database from NCBI to Azure Blob Storage.

    Uses Azure Blob's start_copy_from_url to copy directly from NCBI's
    public S3 bucket. The copy is server-side — Azure Storage fetches from
    S3 directly. No VM, no azcopy, no az login needed.
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    body = json.loads(raw.decode("utf-8"))
    sub = body.get("subscription_id", "")
    storage_rg = body.get("storage_resource_group", "")
    account_name = body.get("account_name", "")
    db_name = body.get("db_name", "core_nt")
    if not all([sub, storage_rg, account_name]):
        return _error_response(400, "subscription_id, storage_resource_group, account_name required")
    # M5: Validate db_name
    if err := _validate_name(db_name, _RE_DB_NAME, "db_name"):
        return _error_response(400, err)
    # H7: Validate account_name
    if err := _validate_name(account_name, _RE_STORAGE_ACCOUNT, "account_name"):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)
    try:
        from xml.etree import ElementTree

        s3_base = "https://ncbi-blast-databases.s3.amazonaws.com"

        # 1. Resolve the latest version directory
        latest_resp = _requests.get(f"{s3_base}/latest-dir", timeout=15)
        latest_resp.raise_for_status()
        latest_dir = latest_resp.text.strip()
        LOGGER.info("NCBI BLAST DB latest dir: %s", latest_dir)

        # 2. List matching objects under {latest_dir}/{db_name}*
        prefix = f"{latest_dir}/{db_name}"
        all_keys: list[str] = []
        continuation = ""
        max_pages = 50  # H6: Guard against unbounded S3 listing
        for _page in range(max_pages):
            list_url = f"{s3_base}?list-type=2&prefix={prefix}&max-keys=1000"
            if continuation:
                list_url += f"&continuation-token={continuation}"
            resp = _requests.get(list_url, timeout=30)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
            for el in root.findall(".//s3:Contents/s3:Key", ns):
                if el.text and not el.text.endswith("/"):
                    all_keys.append(el.text)
            is_truncated = root.findtext("s3:IsTruncated", "false", ns)
            if is_truncated == "true":
                token_el = root.find("s3:NextContinuationToken", ns)
                continuation = token_el.text if token_el is not None and token_el.text else ""
            else:
                break

        if not all_keys:
            return _error_response(404, f"No files found for database '{db_name}' in NCBI S3 (dir: {latest_dir})")

        # 3. Copy each file to Azure Blob via start_copy_from_url (server-side)
        #    Uses the caller's credential (needs Storage Blob Data Contributor on the account)
        from azure.storage.blob import BlobServiceClient
        blob_svc = BlobServiceClient(
            account_url=f"https://{account_name}.blob.core.windows.net",
            credential=cred,
        )
        container = blob_svc.get_container_client("blast-db")
        copied = []
        skipped = []
        for key in all_keys:
            source_url = f"{s3_base}/{key}"
            blob_name = key.split("/")[-1]  # strip date prefix dir
            blob_client = container.get_blob_client(blob_name)
            try:
                blob_client.start_copy_from_url(source_url)
                copied.append(blob_name)
                LOGGER.info("Started copy: %s -> blast-db/%s", source_url, blob_name)
            except Exception as copy_exc:
                if "PendingCopyOperation" in str(copy_exc):
                    skipped.append(blob_name)
                    LOGGER.info("Copy already in progress for %s, skipping", blob_name)
                else:
                    raise

        LOGGER.info("DB prepare initiated for %s: %d files from %s (%d skipped/in-progress)", db_name, len(copied), latest_dir, len(skipped))

        # Write version metadata blob so we can detect updates later
        import json as _json_mod
        from datetime import datetime as _dt, timezone as _tz
        metadata_blob = container.get_blob_client(f"{db_name}-metadata.json")
        metadata_blob.upload_blob(
            _json_mod.dumps({
                "db_name": db_name,
                "source_version": latest_dir,
                "downloaded_at": _dt.now(_tz.utc).isoformat(),
                "file_count": len(copied) + len(skipped),
            }).encode("utf-8"),
            overwrite=True,
        )

        return _json_response({
            "ok": True,
            "db_name": db_name,
            "files_copied": len(copied),
            "files_already_copying": len(skipped),
            "source_version": latest_dir,
            "output": f"Server-side copy: {len(copied)} started, {len(skipped)} already in progress ({latest_dir}).",
        })
    except Exception as exc:
        LOGGER.warning("DB prepare failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))


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


@app.route(route="monitor/aks/run-command", methods=["POST"])
def aks_run_command(req: func.HttpRequest) -> func.HttpResponse:
    """Execute a read-only kubectl command on an AKS cluster via Run Command API."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    body = json.loads(raw.decode("utf-8"))
    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    cluster = body.get("cluster_name", "")
    command = body.get("command", "")
    if not all([sub, rg, cluster, command]):
        return _error_response(400, "subscription_id, resource_group, cluster_name, command required")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)
    try:
        result = monitoring_svc.run_aks_command(cred, sub, rg, cluster, command)
        return _json_response(result)
    except ValueError as exc:
        return _error_response(400, str(exc))
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


# ---------------------------------------------------------------------------
# AKS — Direct Kubernetes API (fast, ~1-3s instead of ~30s)
# ---------------------------------------------------------------------------
@app.route(route="monitor/aks/nodes", methods=["GET"])
def aks_get_nodes(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "cluster_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    try:
        nodes = monitoring_svc.k8s_get_nodes(cred, params["subscription_id"], params["resource_group"], params["cluster_name"])
        return _json_response({"nodes": nodes})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@app.route(route="monitor/aks/pods", methods=["GET"])
def aks_get_pods(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "cluster_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    try:
        pods = monitoring_svc.k8s_get_pods(cred, params["subscription_id"], params["resource_group"], params["cluster_name"])
        return _json_response({"pods": pods})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@app.route(route="monitor/aks/top-nodes", methods=["GET"])
def aks_top_nodes(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "cluster_name")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    try:
        metrics = monitoring_svc.k8s_top_nodes(cred, params["subscription_id"], params["resource_group"], params["cluster_name"])
        return _json_response({"nodes": metrics})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@app.route(route="monitor/aks/pod-logs", methods=["GET"])
def aks_pod_logs(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group", "cluster_name", "namespace", "pod_name")
    if err:
        return err
    try:
        tail = int(req.params.get("tail", "200"))
    except ValueError:
        return _error_response(400, "tail must be a valid integer")
    if tail < 1 or tail > 10000:
        return _error_response(400, "tail must be between 1 and 10000")
    cred = credential_for_caller(identity.raw_token)
    try:
        logs = monitoring_svc.k8s_pod_logs(cred, params["subscription_id"], params["resource_group"], params["cluster_name"], params["namespace"], params["pod_name"], tail)
        return _json_response({"logs": logs, "pod_name": params["pod_name"], "namespace": params["namespace"]})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


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
    try:
        status = monitoring_svc.get_vm_status(
            cred, params["subscription_id"], params["resource_group"], params["vm_name"]
        )
    except Exception as exc:
        exc_type = type(exc).__name__
        if exc_type == "ResourceNotFoundError" or "not found" in str(exc).lower():
            return _json_response(
                {"error": "VM not found", "vm_name": params["vm_name"], "resource_group": params["resource_group"]},
                status=404,
            )
        LOGGER.warning("monitor_terminal unexpected error: %s", exc_type)
        return _error_response(500, "Failed to query VM status")
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
@app.route(route="blast/jobs/{job_id}/file", methods=["GET"])
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
            cred, params["storage_account"], "queries",
            f"{job_id}/{filename}", max_bytes=max_bytes,
        )
        return _json_response({"name": filename, "content": text, "truncated": len(text) >= max_bytes})
    except Exception as exc:
        return _error_response(404, f"file not found: {sanitise(str(exc))[:200]}")


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
                pass

    return _json_response({"jobs": jobs})


@app.route(route="blast/jobs/{job_id}", methods=["GET"])
@app.durable_client_input(client_name="client")
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
            instance_id, show_input=False,
            show_history=show_history, show_history_output=show_history,
        )
        if orch_status:
            job["runtime_status"] = orch_status.runtime_status.name if orch_status.runtime_status else "Unknown"
            job["custom_status"] = orch_status.custom_status
            job["output"] = orch_status.output
            if show_history and orch_status.history:
                job["history"] = orch_status.history

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
        **{k: job.get(k) for k in (
            "subscription_id", "resource_group", "storage_account",
            "cluster_name", "config_snapshot",
        ) if job.get(k)},
    }
    delete_instance_id = await client.start_new(
        "delete_blast_orchestrator", None, delete_input
    )
    LOGGER.info("started delete_blast_orchestrator job=%s instance=%s", job_id, delete_instance_id)

    return _json_response({"job_id": job_id, "status": "deleting", "instance_id": delete_instance_id})


@app.route(route="blast/jobs/{job_id}/cancel", methods=["POST"])
@app.durable_client_input(client_name="client")
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
    await client.signal_entity(entity_id, "update_job", {
        "job_id": job_id, "status": "cancelled", "phase": "cancelled",
    })
    LOGGER.info("cancelled blast job=%s instance=%s", job_id, instance_id)
    return _json_response({"job_id": job_id, "status": "cancelled"})


# ---------------------------------------------------------------------------
# ARM discovery — backend-proxied so the browser uses az login credential
# ---------------------------------------------------------------------------
@app.route(route="arm/subscriptions", methods=["GET"])
def list_subscriptions(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    from azure.mgmt.resource import SubscriptionClient

    cred = credential_for_caller(identity.raw_token)
    client = SubscriptionClient(cred)
    subs = []
    for s in client.subscriptions.list():
        state = s.state
        subs.append({
            "subscriptionId": s.subscription_id,
            "displayName": s.display_name,
            "state": state.value if hasattr(state, "value") else str(state or "Unknown"),
            "tenantId": s.tenant_id,
        })
    subs.sort(key=lambda x: x["displayName"])
    return _json_response(subs)


@app.route(route="arm/subscriptions/{subscription_id}/resource-groups", methods=["GET"])
def list_resource_groups_route(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    subscription_id = req.route_params.get("subscription_id")
    if not subscription_id:
        return _error_response(400, "subscription_id missing")

    from services.azure_clients import resource_client

    cred = credential_for_caller(identity.raw_token)
    rc = resource_client(cred, subscription_id)
    groups = [{"name": g.name, "location": g.location, "tags": g.tags or {}}
              for g in rc.resource_groups.list()]
    groups.sort(key=lambda x: x["name"])
    return _json_response(groups)


# ---------------------------------------------------------------------------
# ARM — Resource Group tags (read/write ELB config)
# ---------------------------------------------------------------------------
ELB_TAG_PREFIX = "elb-"

@app.route(route="arm/resource-group/tags", methods=["GET"])
def get_rg_tags(req: func.HttpRequest) -> func.HttpResponse:
    """Read ELB-related tags from a resource group."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    from services.azure_clients import resource_client
    rc = resource_client(cred, params["subscription_id"])
    try:
        rg = rc.resource_groups.get(params["resource_group"])
        tags = {k: v for k, v in (rg.tags or {}).items() if k.startswith(ELB_TAG_PREFIX)}
        return _json_response({"resource_group": rg.name, "tags": tags})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@app.route(route="arm/resource-group/tags", methods=["POST"])
def set_rg_tags(req: func.HttpRequest) -> func.HttpResponse:
    """Write ELB-related tags to a resource group (merge, not replace)."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "body required")
    body = json.loads(raw.decode("utf-8"))
    sub = body.get("subscription_id", "")
    rg_name = body.get("resource_group", "")
    new_tags = body.get("tags", {})
    if not sub or not rg_name or not new_tags:
        return _error_response(400, "subscription_id, resource_group, tags required")
    # Only allow elb- prefixed tags
    for k in new_tags:
        if not k.startswith(ELB_TAG_PREFIX):
            return _error_response(400, f"tag key must start with '{ELB_TAG_PREFIX}': {k}")
    cred = credential_for_caller(identity.raw_token)
    from services.azure_clients import resource_client
    rc = resource_client(cred, sub)
    try:
        rg = rc.resource_groups.get(rg_name)
        merged = {**(rg.tags or {}), **new_tags}
        rc.resource_groups.create_or_update(rg_name, {"location": rg.location, "tags": merged})
        return _json_response({"resource_group": rg_name, "tags": {k: v for k, v in merged.items() if k.startswith(ELB_TAG_PREFIX)}})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@app.route(route="arm/subscriptions/{subscription_id}/resource-groups/{rg}/storage-accounts", methods=["GET"])
def list_storage_accounts_route(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    subscription_id = req.route_params.get("subscription_id")
    rg = req.route_params.get("rg")
    if not subscription_id or not rg:
        return _error_response(400, "subscription_id and rg required")

    from services.azure_clients import storage_client as sc

    cred = credential_for_caller(identity.raw_token)
    client = sc(cred, subscription_id)
    accounts = [{"name": a.name, "location": a.location}
                for a in client.storage_accounts.list_by_resource_group(rg)]
    accounts.sort(key=lambda x: x["name"])
    return _json_response(accounts)


@app.route(route="arm/subscriptions/{subscription_id}/resource-groups/{rg}/acrs", methods=["GET"])
def list_acrs_route(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    subscription_id = req.route_params.get("subscription_id")
    rg = req.route_params.get("rg")
    if not subscription_id or not rg:
        return _error_response(400, "subscription_id and rg required")

    from services.azure_clients import acr_client

    cred = credential_for_caller(identity.raw_token)
    client = acr_client(cred, subscription_id)
    registries = [{"name": r.name, "location": r.location,
                   "loginServer": r.login_server}
                  for r in client.registries.list_by_resource_group(rg)]
    registries.sort(key=lambda x: x["name"])
    return _json_response(registries)


@app.route(route="arm/subscriptions/{subscription_id}/resource-groups/{rg}/vms", methods=["GET"])
def list_vms_route(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    subscription_id = req.route_params.get("subscription_id")
    rg = req.route_params.get("rg")
    if not subscription_id or not rg:
        return _error_response(400, "subscription_id and rg required")

    from services.azure_clients import compute_client as cc

    cred = credential_for_caller(identity.raw_token)
    client = cc(cred, subscription_id)
    vms = [{"name": v.name, "location": v.location}
           for v in client.virtual_machines.list(rg)]
    vms.sort(key=lambda x: x["name"])
    return _json_response(vms)


# ---------------------------------------------------------------------------
# Resource provisioning — wizard-driven resource creation
# ---------------------------------------------------------------------------
@app.route(route="resources/ensure-rg", methods=["POST"])
def ensure_resource_group(req: func.HttpRequest) -> func.HttpResponse:
    """Create a resource group if it doesn't exist. Idempotent."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = json.loads(req.get_body() or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "region"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")

    from services import network as net_svc

    cred = credential_for_caller(identity.raw_token)
    try:
        net_svc.ensure_resource_group(
            cred, body["subscription_id"], body["resource_group"], body["region"],
        )
    except Exception as exc:
        LOGGER.warning("ensure_resource_group failed: %s", exc)
        return _error_response(500, f"failed to create resource group: {sanitise(str(exc))}")

    LOGGER.info(
        "ensure_resource_group by oid=%s rg=%s",
        identity.object_id, body["resource_group"],
    )
    return _json_response({
        "resource_group": body["resource_group"],
        "region": body["region"],
        "status": "created",
    })


@app.route(route="resources/ensure-storage", methods=["POST"])
def ensure_storage_account(req: func.HttpRequest) -> func.HttpResponse:
    """Create a storage account with HNS if it doesn't exist. Idempotent."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = json.loads(req.get_body() or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "account_name", "region"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")

    cred = credential_for_caller(identity.raw_token)
    try:
        monitoring_svc.ensure_storage_account(
            cred,
            body["subscription_id"],
            body["resource_group"],
            body["account_name"],
            body["region"],
        )
    except Exception as exc:
        LOGGER.warning("ensure_storage_account failed: %s", exc)
        return _error_response(500, f"failed to create storage account: {sanitise(str(exc))}")

    LOGGER.info(
        "ensure_storage_account by oid=%s account=%s",
        identity.object_id, body["account_name"],
    )
    return _json_response({
        "account_name": body["account_name"],
        "region": body["region"],
        "status": "created",
    })


@app.route(route="resources/ensure-acr", methods=["POST"])
def ensure_acr(req: func.HttpRequest) -> func.HttpResponse:
    """Create an ACR if it doesn't exist. Idempotent."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = json.loads(req.get_body() or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "registry_name", "region"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")

    cred = credential_for_caller(identity.raw_token)
    try:
        monitoring_svc.ensure_acr(
            cred,
            body["subscription_id"],
            body["resource_group"],
            body["registry_name"],
            body["region"],
        )
    except Exception as exc:
        LOGGER.warning("ensure_acr failed: %s", exc)
        return _error_response(500, f"failed to create ACR: {sanitise(str(exc))}")

    LOGGER.info(
        "ensure_acr by oid=%s registry=%s",
        identity.object_id, body["registry_name"],
    )
    return _json_response({
        "registry_name": body["registry_name"],
        "region": body["region"],
        "status": "created",
    })


# ---------------------------------------------------------------------------
# AKS Cluster provisioning
# ---------------------------------------------------------------------------
# Allowed node SKUs for ElasticBLAST (E-series v5, memory-optimized)
_AKS_ALLOWED_SKUS = [
    "Standard_E16s_v5",
    "Standard_E20s_v5",
    "Standard_E32s_v5",
    "Standard_E48s_v5",
    "Standard_E64s_v5",
]


@app.route(route="aks/skus", methods=["GET"])
def list_aks_skus(req: func.HttpRequest) -> func.HttpResponse:
    """Return the allowed node SKUs for AKS cluster provisioning."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    return _json_response({"skus": _AKS_ALLOWED_SKUS, "default": "Standard_E32s_v5"})


@app.route(route="aks/provision", methods=["POST"])
@app.durable_client_input(client_name="client")
async def provision_aks_cluster(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Create an AKS cluster for ElasticBLAST. Returns immediately — polls via status endpoint."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "region", "cluster_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")

    sub = body["subscription_id"]
    rg = body["resource_group"]
    cluster_name = body["cluster_name"]
    node_sku = body.get("node_sku", "Standard_E32s_v5")
    node_count = body.get("node_count", 10)

    # Validate inputs
    if err := _validate_sub(sub):
        return _error_response(400, err)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    if err := _validate_name(cluster_name, _RE_CLUSTER_NAME, "cluster_name"):
        return _error_response(400, err)
    if node_sku not in _AKS_ALLOWED_SKUS:
        return _error_response(400, f"node_sku must be one of: {', '.join(_AKS_ALLOWED_SKUS)}")
    if not isinstance(node_count, int) or node_count < 3 or node_count > 20:
        return _error_response(400, "node_count must be between 3 and 20")

    orchestration_input = {
        **body,
        "user_assertion": identity.raw_token,
        "owner_oid": identity.object_id,
    }
    instance_id = await client.start_new(
        "provision_aks_orchestrator", None, orchestration_input
    )
    LOGGER.info("started provision_aks_orchestrator instance=%s cluster=%s", instance_id, cluster_name)
    return client.create_check_status_response(req, instance_id)


@app.route(route="aks/delete", methods=["POST"])
def delete_aks_cluster(req: func.HttpRequest) -> func.HttpResponse:
    """Delete an AKS cluster."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "cluster_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")

    # #9: Validate inputs
    if err := _validate_sub(body["subscription_id"]):
        return _error_response(400, err)
    if err := _validate_rg(body["resource_group"]):
        return _error_response(400, err)
    if err := _validate_name(body["cluster_name"], _RE_CLUSTER_NAME, "cluster_name"):
        return _error_response(400, err)

    cred = credential_for_caller(identity.raw_token)
    try:
        from azure.mgmt.containerservice import ContainerServiceClient
        aks_client = ContainerServiceClient(cred, body["subscription_id"])
        aks_client.managed_clusters.begin_delete(body["resource_group"], body["cluster_name"])
        LOGGER.info("AKS cluster delete started: %s in %s", body["cluster_name"], body["resource_group"])
        return _json_response({"cluster_name": body["cluster_name"], "status": "deleting"})
    except Exception as exc:
        LOGGER.warning("AKS delete failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))


@app.route(route="aks/start", methods=["POST"])
def start_aks_cluster(req: func.HttpRequest) -> func.HttpResponse:
    """Start a stopped AKS cluster."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    body = json.loads(raw.decode("utf-8"))
    required = {"subscription_id", "resource_group", "cluster_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")
    if err := _validate_sub(body["subscription_id"]):
        return _error_response(400, err)
    if err := _validate_rg(body["resource_group"]):
        return _error_response(400, err)
    if err := _validate_name(body["cluster_name"], _RE_CLUSTER_NAME, "cluster_name"):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)
    try:
        from azure.mgmt.containerservice import ContainerServiceClient
        aks_client = ContainerServiceClient(cred, body["subscription_id"])
        aks_client.managed_clusters.begin_start(body["resource_group"], body["cluster_name"])
        LOGGER.info("AKS cluster start initiated: %s", body["cluster_name"])
        return _json_response({"cluster_name": body["cluster_name"], "status": "starting"})
    except Exception as exc:
        LOGGER.warning("AKS start failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))


@app.route(route="aks/stop", methods=["POST"])
def stop_aks_cluster(req: func.HttpRequest) -> func.HttpResponse:
    """Stop a running AKS cluster to save cost."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    body = json.loads(raw.decode("utf-8"))
    required = {"subscription_id", "resource_group", "cluster_name"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")
    if err := _validate_sub(body["subscription_id"]):
        return _error_response(400, err)
    if err := _validate_rg(body["resource_group"]):
        return _error_response(400, err)
    if err := _validate_name(body["cluster_name"], _RE_CLUSTER_NAME, "cluster_name"):
        return _error_response(400, err)
    cred = credential_for_caller(identity.raw_token)
    try:
        from azure.mgmt.containerservice import ContainerServiceClient
        aks_client = ContainerServiceClient(cred, body["subscription_id"])
        aks_client.managed_clusters.begin_stop(body["resource_group"], body["cluster_name"])
        LOGGER.info("AKS cluster stop initiated: %s", body["cluster_name"])
        return _json_response({"cluster_name": body["cluster_name"], "status": "stopping"})
    except Exception as exc:
        LOGGER.warning("AKS stop failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))


@app.route(route="aks/{cluster_name}/assign-roles", methods=["POST"])
def assign_aks_roles(req: func.HttpRequest) -> func.HttpResponse:
    """Assign RBAC roles to AKS kubelet identity for ACR pull and storage access."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    cluster_name = req.route_params.get("cluster_name", "")
    acr_rg = body.get("acr_resource_group", "")
    acr_name = body.get("acr_name", "")
    storage_rg = body.get("storage_resource_group", "")
    storage_account = body.get("storage_account", "")

    if not all([sub, rg, cluster_name]):
        return _error_response(400, "subscription_id, resource_group required")

    cred = credential_for_caller(identity.raw_token)
    assigned = []
    try:
        from azure.mgmt.containerservice import ContainerServiceClient
        from azure.mgmt.authorization import AuthorizationManagementClient

        aks_client = ContainerServiceClient(cred, sub)
        cluster = aks_client.managed_clusters.get(rg, cluster_name)
        kubelet_oid = None
        if cluster.identity_profile and "kubeletidentity" in cluster.identity_profile:
            kubelet_oid = cluster.identity_profile["kubeletidentity"].object_id

        if not kubelet_oid:
            return _error_response(400, "kubelet identity not found on cluster")

        auth_client = AuthorizationManagementClient(cred, sub)

        # AcrPull on ACR
        if acr_rg and acr_name:
            scope = f"/subscriptions/{sub}/resourceGroups/{acr_rg}/providers/Microsoft.ContainerRegistry/registries/{acr_name}"
            _assign_role(auth_client, scope, kubelet_oid, "7f951dda-4ed3-4680-a7ca-43fe172d538d")  # AcrPull
            assigned.append("AcrPull")

        # Storage Blob Data Contributor on storage account
        if storage_rg and storage_account:
            scope = f"/subscriptions/{sub}/resourceGroups/{storage_rg}/providers/Microsoft.Storage/storageAccounts/{storage_account}"
            _assign_role(auth_client, scope, kubelet_oid, "ba92f5b4-2d11-453d-a403-e96b0029c9fe")  # Storage Blob Data Contributor
            assigned.append("StorageBlobDataContributor")

        return _json_response({"kubelet_oid": kubelet_oid, "roles_assigned": assigned})
    except Exception as exc:
        LOGGER.warning("Role assignment failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))


def _assign_role(auth_client: Any, scope: str, principal_id: str, role_definition_id: str) -> None:
    """Assign a role to a principal. Idempotent — ignores conflict."""
    role_def = f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{role_definition_id}"
    assignment_name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}:{principal_id}:{role_definition_id}"))
    try:
        auth_client.role_assignments.create(
            scope, assignment_name,
            {
                "role_definition_id": role_def,
                "principal_id": principal_id,
                "principal_type": "ServicePrincipal",
            },
        )
    except Exception as exc:
        if "Conflict" in str(exc) or "RoleAssignmentExists" in str(exc):
            LOGGER.debug("Role already assigned, skipping: %s", assignment_name)
        else:
            raise


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

    resource_group = req.params.get("resource_group", "")

    cred = credential_for_caller(identity.raw_token)

    # Check public access state
    if resource_group:
        try:
            from azure.mgmt.storage import StorageManagementClient as _SM
            sm = _SM(cred, params["subscription_id"])
            acct = sm.storage_accounts.get_properties(resource_group, params["storage_account"])
            if getattr(acct, "public_network_access", "Enabled") != "Enabled":
                return _json_response({
                    "job_id": job_id,
                    "files": [],
                    "public_access_disabled": True,
                    "message": "Storage public access is disabled. Enable it to view results.",
                })
        except Exception:
            pass  # Fall through and try data plane anyway

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
# BLAST — database listing
# ---------------------------------------------------------------------------
@app.route(route="blast/databases", methods=["GET"])
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
            params["resource_group"], params["storage_account"],
        )
        public_access = getattr(acct, "public_network_access", "Enabled")

        if public_access != "Enabled":
            return _json_response({
                "databases": [],
                "public_access_disabled": True,
                "message": "Storage public network access is disabled. "
                           "Enable it temporarily to scan for databases.",
            })

        # Try Data Plane access with caller's credential
        dbs = storage_data_svc.list_databases(
            cred,
            params["storage_account"],
        )
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))
    return _json_response({"databases": dbs})


# ---------------------------------------------------------------------------
# BLAST — database update check
# ---------------------------------------------------------------------------
@app.route(route="blast/databases/check-updates", methods=["GET"])
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


@app.orchestration_trigger(context_name="context")
def delete_blast_orchestrator(context):
    return _del_blast.delete_blast_orchestrator(context)


# #2 CRITICAL: AKS provision orchestrator
@app.orchestration_trigger(context_name="context")
def provision_aks_orchestrator(context):
    """Create AKS cluster + assign roles as a Durable orchestrator."""
    from orchestrators import provision_aks as _prov_aks
    return _prov_aks.provision_aks_orchestrator(context)


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
def ensure_keyvault_activity(payload: dict) -> dict:
    return terminal_activities.activity_ensure_keyvault(payload)


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
def ensure_vm_running_activity(payload: dict) -> dict:
    return blast_activities.activity_ensure_vm_running(payload)


@app.activity_trigger(input_name="payload")
def generate_blast_config_activity(payload: dict) -> dict:
    return blast_activities.activity_generate_blast_config(payload)


@app.activity_trigger(input_name="payload")
def run_elastic_blast_submit_activity(payload: dict) -> dict:
    return blast_activities.activity_run_elastic_blast_submit(payload)


@app.activity_trigger(input_name="payload")
def run_elastic_blast_prepare_activity(payload: dict) -> dict:
    return blast_activities.activity_run_elastic_blast_prepare(payload)


@app.activity_trigger(input_name="payload")
def check_blast_status_activity(payload: dict) -> dict:
    return blast_activities.activity_check_blast_status(payload)


@app.activity_trigger(input_name="payload")
def export_blast_results_activity(payload: dict) -> dict:
    return blast_activities.activity_export_blast_results(payload)


@app.activity_trigger(input_name="payload")
def run_elastic_blast_delete_activity(payload: dict) -> dict:
    return blast_activities.activity_run_elastic_blast_delete(payload)


@app.activity_trigger(input_name="payload")
def list_result_blobs_activity(payload: dict) -> dict:
    return blast_activities.activity_list_result_blobs(payload)


@app.activity_trigger(input_name="payload")
def k8s_check_blast_status_activity(payload: dict) -> dict:
    return blast_activities.activity_k8s_check_blast_status(payload)


@app.activity_trigger(input_name="payload")
def k8s_check_warmup_ready_activity(payload: dict) -> dict:
    return blast_activities.activity_k8s_check_warmup_ready(payload)


@app.activity_trigger(input_name="payload")
def list_databases_activity(payload: dict) -> dict:
    return blast_activities.activity_list_databases(payload)


# AKS provision activities
@app.activity_trigger(input_name="payload")
def create_aks_cluster_activity(payload: dict) -> dict:
    """Activity: create AKS cluster (long-running, handled by DF retry)."""
    from services.azure_clients import credential_for_assertion
    cred = credential_for_assertion(payload["user_assertion"])
    from azure.mgmt.containerservice import ContainerServiceClient
    aks_client = ContainerServiceClient(cred, payload["subscription_id"])
    cluster_params = {
        "location": payload["region"],
        "tags": {"created-by": "elastic-blast-control-plane", "owner-oid": payload.get("owner_oid", "")},
        "identity": {"type": "SystemAssigned"},
        "dns_prefix": payload["cluster_name"],
        "auto_upgrade_profile": {"upgrade_channel": "none"},
        "agent_pool_profiles": [{
            "name": "nodepool1",
            "count": payload.get("node_count", 10),
            "vm_size": payload.get("node_sku", "Standard_E32s_v5"),
            "os_disk_type": "Managed",
            "mode": "System",
            "enable_auto_scaling": False,
            "type": "VirtualMachineScaleSets",
        }],
        "network_profile": {"load_balancer_sku": "standard"},
        "storage_profile": {"blob_csi_driver": {"enabled": True}},
    }
    poller = aks_client.managed_clusters.begin_create_or_update(
        payload["resource_group"], payload["cluster_name"], cluster_params
    )
    poller.result()
    return {"cluster_name": payload["cluster_name"], "status": "succeeded"}


@app.activity_trigger(input_name="payload")
def assign_aks_roles_activity(payload: dict) -> dict:
    """Activity: assign RBAC roles to AKS kubelet identity."""
    from services.azure_clients import credential_for_assertion
    cred = credential_for_assertion(payload["user_assertion"])
    sub = payload["subscription_id"]
    rg = payload["resource_group"]
    cluster_name = payload["cluster_name"]

    from azure.mgmt.containerservice import ContainerServiceClient
    from azure.mgmt.authorization import AuthorizationManagementClient

    aks_client = ContainerServiceClient(cred, sub)
    cluster = aks_client.managed_clusters.get(rg, cluster_name)
    kubelet_oid = None
    if cluster.identity_profile and "kubeletidentity" in cluster.identity_profile:
        kubelet_oid = cluster.identity_profile["kubeletidentity"].object_id

    if not kubelet_oid:
        return {"roles_assigned": [], "error": "kubelet identity not found"}

    auth_client = AuthorizationManagementClient(cred, sub)
    assigned: list[str] = []

    acr_rg = payload.get("acr_resource_group", "")
    acr_name = payload.get("acr_name", "")
    storage_rg = payload.get("storage_resource_group", "")
    storage_account = payload.get("storage_account", "")

    if acr_rg and acr_name:
        scope = f"/subscriptions/{sub}/resourceGroups/{acr_rg}/providers/Microsoft.ContainerRegistry/registries/{acr_name}"
        _assign_role(auth_client, scope, kubelet_oid, "7f951dda-4ed3-4680-a7ca-43fe172d538d")
        assigned.append("AcrPull")
    if storage_rg and storage_account:
        scope = f"/subscriptions/{sub}/resourceGroups/{storage_rg}/providers/Microsoft.Storage/storageAccounts/{storage_account}"
        _assign_role(auth_client, scope, kubelet_oid, "ba92f5b4-2d11-453d-a403-e96b0029c9fe")
        assigned.append("StorageBlobDataContributor")

    return {"kubelet_oid": kubelet_oid, "roles_assigned": assigned}


# #6 HIGH: Terminal VM teardown
@app.route(route="terminal/{vm_name}/destroy", methods=["POST"])
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
    rg = req.params.get("resource_group") or os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal")
    if err := _validate_rg(rg):
        return _error_response(400, err)
    sub = req.params.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    delete_rg = req.params.get("delete_rg", "false").lower() == "true"

    cred = credential_for_caller(identity.raw_token)
    errors: list[str] = []

    # Delete VM first
    try:
        compute_svc.delete_vm(cred, sub, rg, vm_name)
    except Exception as exc:
        errors.append(f"VM: {sanitise(str(exc))}")

    # Delete NIC, PIP, OS disk, NSG
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

    # Delete KV secret
    vault_uri = os.environ.get("KEY_VAULT_URI")
    if not vault_uri:
        vault_suffix = vm_name[-8:] if len(vm_name) >= 8 else vm_name
        vault_uri = f"https://kv-elb-{vault_suffix}.vault.azure.net/"
    try:
        kv_svc.delete_secret(cred, vault_uri, f"vm-{vm_name}-password")
    except Exception:
        pass  # Non-critical if secret doesn't exist

    # Optionally delete RG
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

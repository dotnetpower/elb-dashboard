"""Azure Functions Python v2 entry point.

Registers HTTP triggers, the Durable Functions orchestrator, and activities.
All HTTP triggers are anonymous at the platform level — auth is enforced by
`auth.token.validate_bearer_token` so the SPA can use MSAL bearer tokens.

Route groups extracted to ``api/routes/*`` and registered as
``df.Blueprint`` instances. New extractions should follow the same pattern:
move handlers + their tests, then ``app.register_functions(bp)`` here.
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

from _http_utils import (
    _RE_ACR_NAME,
    _RE_BLOB_NAME,
    _RE_CLUSTER_NAME,
    _RE_DB_NAME,
    _RE_INSTANCE_ID,
    _RE_RESOURCE_GROUP,
    _RE_STORAGE_ACCOUNT,
    _RE_SUBSCRIPTION,
    _RE_VM_NAME,
    _error_response,
    _json_response,
    _require_query,
    _validate_ip,
    _validate_name,
    _validate_rg,
    _validate_sub,
)
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
from routes import monitor as _monitor_routes
from services import keyvault as kv_svc
from services.sanitise import sanitise
from services import monitoring as monitoring_svc
from services import storage_data as storage_data_svc
from services import compute as compute_svc
from services import network as network_svc
from services.azure_clients import credential_for_caller

LOGGER = logging.getLogger(__name__)

app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Register Blueprints (one per route group). Add new groups here as they are
# extracted out of this file.
app.register_functions(_monitor_routes.bp)


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

    Resolves the vault name with the same `_default_vault_name` helper used
    by the provision activity (`api/activities/terminal.py`). Older clients
    that only know the legacy `kv-elb-{vm[-8:]}` pattern are still served
    when `subscription_id`/`resource_group` are not supplied, but this is a
    fallback only — the canonical layout is the (sub, rg, vm) hash form.
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

    candidate_uris: list[str] = []
    env_uri = os.environ.get("KEY_VAULT_URI")
    if env_uri:
        candidate_uris.append(env_uri.rstrip("/") + "/")
    if sub and rg:
        try:
            from activities.terminal import _default_vault_name
            canonical = _default_vault_name(sub, rg, vm_name)
            candidate_uris.append(f"https://{canonical}.vault.azure.net/")
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("could not derive canonical vault name: %s", exc)
    # Legacy fallback for VMs provisioned before the hash-suffix change.
    legacy_suffix = vm_name[-8:] if len(vm_name) >= 8 else vm_name
    candidate_uris.append(f"https://kv-elb-{legacy_suffix}.vault.azure.net/")

    last_exc: Exception | None = None
    for vault_uri in candidate_uris:
        try:
            password = kv_svc.get_secret(cred, vault_uri, f"vm-{vm_name}-password")
            return _json_response({"vm_name": vm_name, "password": password})
        except Exception as exc:
            last_exc = exc
            LOGGER.info("secret lookup miss on %s: %s", vault_uri, str(exc)[:120])
    LOGGER.warning("secret read failed for vm=%s on all candidates: %s", vm_name, last_exc)
    return _error_response(404, "password secret not found")


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
        "echo KUBECTL_VERSION=$(kubectl version --client -o yaml 2>/dev/null | grep gitVersion | head -1 | sed 's/.*: //' || kubectl version --client 2>/dev/null | head -1 || echo 'not installed')\n"
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

    # Optional: filter to specific images (e.g. ["ncbi/elasticblast-job-submit"])
    requested_images = body.get("images", [])  # empty = all
    from azure.mgmt.containerregistry.models import (
        DockerBuildRequest,
        PlatformProperties,
        OS,
    )

    acr = ContainerRegistryManagementClient(cred, sub, api_version="2019-06-01-preview")
    results = []
    # Schedule all builds first (fire pollers in parallel)
    pollers: list[tuple[str, Any]] = []
    for image, tag in IMAGE_TAGS.items():
        # Skip if specific images requested and this one isn't in the list
        if requested_images and image not in requested_images:
            continue
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
                # Build context for the docker step — defaults to "." (the
                # uploaded source root). When build_context_dir is set the
                # build step descends into that subdirectory so Dockerfile
                # COPY directives resolve against subdir-local files.
                build_context_dir = build_info.get("build_context_dir", ".")
                task_yaml = f"""version: v1.1.0
steps:
  - cmd: bash -c "{pre_build}"
  - build: -f {dockerfile} -t $Registry/{full_image} {build_context_dir}
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
            poller = acr.registries.begin_schedule_run(rg, registry, build_req)
            pollers.append((full_image, poller))
        except Exception as exc:
            LOGGER.warning("ACR build schedule failed for %s: %s", full_image, exc)
            results.append({"image": full_image, "status": "failed", "error": sanitise(str(exc))})

    # Collect results from all pollers (builds are now running in parallel in ACR)
    for full_image, poller in pollers:
        try:
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

        # 2.5 Enable public network access (required for start_copy_from_url from NCBI S3)
        try:
            from azure.mgmt.storage import StorageManagementClient
            storage_mgmt = StorageManagementClient(cred, sub)
            storage_mgmt.storage_accounts.update(
                storage_rg, account_name,
                {"properties": {"public_network_access": "Enabled"}},
            )
            LOGGER.info("Temporarily enabled public access on %s for DB download", account_name)
            import time as _time
            _time.sleep(10)  # wait for propagation
        except Exception as toggle_exc:
            LOGGER.warning("Could not enable public access (may already be enabled): %s", str(toggle_exc)[:100])

        # 3. Background-start all copies. For large DBs (100s of files), this would
        #    exceed the 4-min SWA proxy timeout if done synchronously. Spawn a thread
        #    that fires all start_copy_from_url calls in parallel, returns immediately.
        from azure.storage.blob import BlobServiceClient
        blob_svc = BlobServiceClient(
            account_url=f"https://{account_name}.blob.core.windows.net",
            credential=cred,
        )
        container = blob_svc.get_container_client("blast-db")

        def _do_copies():
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import json as _json_mod
            from datetime import datetime as _dt, timezone as _tz

            def _copy_one(key: str) -> tuple[str, str]:
                source_url = f"{s3_base}/{key}"
                # Layout MUST match what `elastic-blast` (upstream
                # `util.py:get_blastdb_info`) expects: it calls
                # `os.path.dirname(db_url)` and runs `azcopy list` on it,
                # then filters lines where `os.path.basename(db)` appears.
                # That requires files to live in a SUBFOLDER named after
                # the database (e.g. `blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA.nsq`).
                # A flat layout (`blast-db/16S_ribosomal_RNA.nsq`) makes
                # `azcopy list` of the parent return wrong results and
                # elastic-blast reports "BLAST database … was not found".
                file_basename = key.split("/")[-1]
                blob_name = f"{db_name}/{file_basename}"
                try:
                    container.get_blob_client(blob_name).start_copy_from_url(source_url)
                    return (blob_name, "started")
                except Exception as e:
                    if "PendingCopyOperation" in str(e):
                        return (blob_name, "skipped")
                    LOGGER.warning("Copy failed for %s: %s", blob_name, str(e)[:200])
                    return (blob_name, "error")

            started = 0
            skipped = 0
            errors = 0
            with ThreadPoolExecutor(max_workers=20) as ex:
                futures = [ex.submit(_copy_one, k) for k in all_keys]
                for f in as_completed(futures):
                    _, status = f.result()
                    if status == "started":
                        started += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        errors += 1

            LOGGER.info("DB prepare done for %s: %d started, %d skipped, %d errors", db_name, started, skipped, errors)

            # Write metadata blob
            try:
                metadata_blob = container.get_blob_client(f"{db_name}-metadata.json")
                metadata_blob.upload_blob(
                    _json_mod.dumps({
                        "db_name": db_name,
                        "source_version": latest_dir,
                        "downloaded_at": _dt.now(_tz.utc).isoformat(),
                        "file_count": started + skipped,
                    }).encode("utf-8"),
                    overwrite=True,
                )
            except Exception as e:
                LOGGER.warning("metadata write failed: %s", str(e)[:100])

            # Re-disable public access
            try:
                from azure.mgmt.storage import StorageManagementClient as _SM
                _sm = _SM(cred, sub)
                _sm.storage_accounts.update(
                    storage_rg, account_name,
                    {"properties": {"public_network_access": "Disabled"}},
                )
                LOGGER.info("Re-disabled public access on %s", account_name)
            except Exception as e:
                LOGGER.warning("Could not re-disable public access on %s: %s", account_name, str(e)[:100])

        from threading import Thread
        Thread(target=_do_copies, daemon=True).start()

        return _json_response({
            "ok": True,
            "db_name": db_name,
            "files_copied": 0,  # async — actual count tracked by client polling list_databases
            "files_total": len(all_keys),
            "source_version": latest_dir,
            "output": f"Started background copy of {len(all_keys)} files from {latest_dir}. Poll /blast/databases for progress.",
            "async": True,
        })
    except Exception as exc:
        LOGGER.warning("DB prepare failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))


# ---------------------------------------------------------------------------
# BLAST — pre-flight readiness check
# ---------------------------------------------------------------------------
@app.route(route="blast/pre-flight", methods=["POST"])
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
                checks.append({
                    "id": "acr_images", "status": "fail",
                    "title": "ACR images not built",
                    "detail": f"Missing: {', '.join(missing_images)}",
                    "action": "Build images from the Dashboard ACR card",
                    "severity": "critical",
                })
            else:
                checks.append({"id": "acr_images", "status": "pass", "title": "ACR images available"})
        except Exception as exc:
            checks.append({
                "id": "acr_images", "status": "warn",
                "title": "Could not check ACR images",
                "detail": sanitise(str(exc))[:200],
                "severity": "medium",
            })
    else:
        checks.append({
            "id": "acr_images", "status": "skip",
            "title": "ACR not configured",
            "detail": "Configure ACR in Dashboard settings",
            "severity": "high",
        })

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
                checks.append({
                    "id": "blast_db", "status": "pass",
                    "title": f"Database '{db_name}' available",
                    "detail": f"{db_info['file_count']} files, {size_gb:.1f} GB" if db_info else "",
                })
            else:
                # Suggest downloading
                available = ", ".join(sorted(db_names)[:5])
                checks.append({
                    "id": "blast_db", "status": "fail",
                    "title": f"Database '{db_name}' not found in storage",
                    "detail": f"Available: {available}" if available else "No databases found. Download one first.",
                    "action": f"Download '{db_name}' from NCBI using the Dashboard storage card",
                    "action_type": "download_db",
                    "action_params": {"db_name": db_name},
                    "severity": "critical",
                    "suggested_dbs": ["core_nt", "16S_ribosomal_RNA", "nt", "nr", "swissprot"],
                })
        except Exception as exc:
            msg = str(exc)
            if "AuthorizationFailure" in msg or "public" in msg.lower():
                checks.append({
                    "id": "blast_db", "status": "warn",
                    "title": "Storage not accessible",
                    "detail": "Public network access may be disabled. Enable temporarily to check.",
                    "severity": "medium",
                })
            else:
                checks.append({
                    "id": "blast_db", "status": "warn",
                    "title": "Could not verify database",
                    "detail": sanitise(msg)[:200],
                    "severity": "medium",
                })
    elif not db_path:
        checks.append({
            "id": "blast_db", "status": "fail",
            "title": "No database selected",
            "severity": "critical",
        })

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
                checks.append({
                    "id": "aks_cluster", "status": "pass",
                    "title": f"AKS cluster '{cluster_name}' running",
                })
            else:
                checks.append({
                    "id": "aks_cluster", "status": "fail",
                    "title": f"AKS cluster not ready (power={power_state}, provisioning={prov_state})",
                    "action": "Start cluster from the Dashboard",
                    "severity": "critical",
                })
        except Exception as exc:
            checks.append({
                "id": "aks_cluster", "status": "fail",
                "title": "AKS cluster not found or inaccessible",
                "detail": sanitise(str(exc))[:200],
                "action": "Create a cluster from the Dashboard",
                "severity": "critical",
            })
    else:
        checks.append({
            "id": "aks_cluster", "status": "fail",
            "title": "No AKS cluster selected",
            "action": "Create or select a cluster",
            "severity": "critical",
        })

    # 4. Terminal VM running
    if terminal_vm:
        try:
            from services.compute import get_vm_status
            vm_status = get_vm_status(cred, sub, terminal_rg, terminal_vm)
            power = vm_status.get("power_state", "unknown")
            if power == "running":
                checks.append({"id": "terminal_vm", "status": "pass", "title": "Terminal VM running"})
            else:
                checks.append({
                    "id": "terminal_vm", "status": "fail",
                    "title": f"Terminal VM not running (state: {power})",
                    "action": "Start VM from the Terminal page",
                    "severity": "critical",
                })
        except Exception:
            checks.append({
                "id": "terminal_vm", "status": "fail",
                "title": "Terminal VM not found",
                "action": "Provision a Terminal VM first",
                "severity": "critical",
            })

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
                checks.append({
                    "id": "storage_containers", "status": "fail",
                    "title": f"Missing storage containers: {', '.join(sorted(missing_containers))}",
                    "action": "Create containers from the Dashboard storage card",
                    "severity": "high",
                })
            else:
                checks.append({"id": "storage_containers", "status": "pass", "title": "Storage containers ready"})
        except Exception as exc:
            checks.append({
                "id": "storage_containers", "status": "warn",
                "title": "Could not check storage containers",
                "detail": sanitise(str(exc))[:200],
                "severity": "medium",
            })

    # 6. Query FASTA format validation
    if query_data:
        lines = query_data.strip().split("\n")
        if not lines or not lines[0].startswith(">"):
            checks.append({
                "id": "fasta_format", "status": "fail",
                "title": "Invalid FASTA: must start with '>' header line",
                "severity": "high",
            })
        else:
            seq_count = sum(1 for l in lines if l.startswith(">"))
            total_bases = sum(len(l.strip()) for l in lines if not l.startswith(">"))
            checks.append({
                "id": "fasta_format", "status": "pass",
                "title": f"FASTA valid: {seq_count} sequence(s), {total_bases:,} residues",
            })

    # Summary
    all_pass = all(c["status"] in ("pass", "skip") for c in checks)
    critical_fails = [c for c in checks if c["status"] == "fail" and c.get("severity") == "critical"]
    return _json_response({
        "ready": all_pass,
        "checks": checks,
        "critical_blockers": len(critical_fails),
        "summary": "All checks passed — ready to submit" if all_pass
                   else f"{len(critical_fails)} critical issue(s) must be resolved before submitting",
    })


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
            caller_oid=identity.object_id,
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
            caller_oid=identity.object_id,
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


@app.route(route="aks/openapi/deploy", methods=["POST"])
@app.durable_client_input(client_name="client")
async def deploy_openapi(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Re-deploy the OpenAPI service to an existing AKS cluster.

    Body: {subscription_id, resource_group, cluster_name, acr_name?,
           storage_account?}.
    Returns the standard Durable Functions check-status response.
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
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

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

    orchestration_input = {
        **body,
        "user_assertion": identity.raw_token,
        "owner_oid": identity.object_id,
    }
    instance_id = await client.start_new(
        "deploy_openapi_orchestrator", None, orchestration_input
    )
    LOGGER.info(
        "started deploy_openapi_orchestrator instance=%s cluster=%s",
        instance_id, body["cluster_name"],
    )
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
    """Assign a role to a principal. Idempotent — ignores conflict.

    The Function App MI usually has only Contributor at subscription scope,
    which does NOT include `Microsoft.Authorization/roleAssignments/write`.
    When the assignment fails with `AuthorizationFailed` /
    `InsufficientPermissions`, log the exact `az role assignment create`
    command an admin can run to unblock provisioning, but do NOT raise —
    callers (orchestrators) treat role assignment as best-effort. Hard
    failures (network, throttling) still bubble up.
    """
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
        msg = str(exc)
        if "Conflict" in msg or "RoleAssignmentExists" in msg:
            LOGGER.debug("Role already assigned, skipping: %s", assignment_name)
            return
        if "AuthorizationFailed" in msg or "InsufficientPermissions" in msg or "does not have authorization" in msg:
            LOGGER.warning(
                "Cannot self-grant role %s to principal %s on %s. "
                "Run as admin: az role assignment create --assignee-object-id %s "
                "--assignee-principal-type ServicePrincipal --role %s --scope '%s'",
                role_definition_id, principal_id, scope,
                principal_id, role_definition_id, scope,
            )
            return
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
# BLAST — custom database builder
# ---------------------------------------------------------------------------
@app.route(route="blast/databases/build", methods=["POST"])
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

    terminal_rg = body.get("terminal_resource_group", os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal"))
    terminal_vm = body.get("terminal_vm_name", "vm-elb-terminal")

    try:
        # Step 1: Upload FASTA to blob if inline
        blob_path = f"custom-db-build/{db_name}/input.fa"
        if fasta_data:
            storage_data_svc.upload_query_text(
                cred, storage_account, "blast-db", blob_path, fasta_data,
            )
        else:
            blob_path = fasta_blob_url  # type: ignore[assignment]

        # Step 2: Get VM SSH details
        vm_ip = compute_svc.get_vm_public_ip(cred, sub, terminal_rg, terminal_vm)
        if not vm_ip:
            return _error_response(400, "Terminal VM has no public IP. Provision and start it first.")

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

# Upload all DB files back to blob storage
for f in {safe_db_name}.*; do
  az storage blob upload \\
    --account-name '{storage_account}' \\
    --container-name 'blast-db' \\
    --name "{safe_db_name}/$f" \\
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
  --name "{safe_db_name}/{safe_db_name}-metadata.json" \\
  --file metadata.json \\
  --auth-mode login \\
  --overwrite \\
  --output none 2>&1

# Cleanup
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

        return _json_response({
            "db_name": db_name,
            "db_type": db_type,
            "title": title,
            "status": "completed",
            "file_count": file_count,
            "container": "blast-db",
            "path": f"{db_name}/",
        })
    except Exception as exc:
        msg = str(exc)
        if "AuthorizationFailure" in msg or "AuthorizationPermissionMismatch" in msg:
            return _error_response(403, "Storage or VM access denied. Check RBAC roles.")
        return _error_response(500, sanitise(msg[:500]))


# ---------------------------------------------------------------------------
# BLAST — results aggregation / analytics
# ---------------------------------------------------------------------------
@app.route(route="blast/jobs/{job_id}/results/aggregate", methods=["GET"])
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
            cred, params["storage_account"], "results", f"{job_id}/",
        )

        # Find .out files (BLAST output)
        out_blobs = [b for b in blobs if b["name"].endswith(".out")]
        if not out_blobs:
            return _json_response({
                "job_id": job_id,
                "status": "no_results",
                "message": "No .out result files found",
                "stats": None,
            })

        # Parse BLAST tabular output (outfmt 7)
        all_hits: list[dict] = []
        max_parse_bytes = 10 * 1024 * 1024  # 10MB cap per file

        for blob_info in out_blobs[:20]:  # cap at 20 files
            try:
                content = storage_data_svc.read_blob_text(
                    cred, params["storage_account"], "results",
                    blob_info["name"], max_bytes=max_parse_bytes,
                )
                hits = _parse_blast_tabular(content)
                all_hits.extend(hits)
            except Exception as exc:
                LOGGER.warning("Failed to parse %s: %s", blob_info["name"], exc)

        if not all_hits:
            return _json_response({
                "job_id": job_id,
                "status": "no_hits",
                "message": "No BLAST hits found in result files",
                "stats": {"total_hits": 0},
            })

        # Aggregate statistics
        stats = _aggregate_blast_hits(all_hits)
        stats["files_parsed"] = len(out_blobs[:20])
        stats["total_files"] = len(out_blobs)

        return _json_response({
            "job_id": job_id,
            "status": "ok",
            "stats": stats,
        })
    except Exception as exc:
        msg = str(exc)
        if "AuthorizationFailure" in msg:
            return _error_response(403, "Storage access denied.")
        return _error_response(500, sanitise(msg[:500]))


# ---------------------------------------------------------------------------
# BLAST — alignment detail
# ---------------------------------------------------------------------------
@app.route(route="blast/jobs/{job_id}/results/alignments", methods=["GET"])
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
                cred, params["storage_account"], "results", f"{job_id}/",
            )
            out_blobs = [b for b in blobs if b["name"].endswith(".out")]
            if not out_blobs:
                return _json_response({"job_id": job_id, "alignments": [], "message": "No result files"})
            blob_name = out_blobs[0]["name"]
        else:
            # Validate blob_name
            if ".." in blob_name or blob_name.startswith("/"):
                return _error_response(400, "invalid blob_name")

        # Read the result file
        content = storage_data_svc.read_blob_text(
            cred, params["storage_account"], "results",
            blob_name, max_bytes=20 * 1024 * 1024,  # 20MB cap
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

        return _json_response({
            "job_id": job_id,
            "blob_name": blob_name,
            "alignments": hits,
            "total_hits": len(all_hits),
            "returned": len(hits),
            "query_ids": query_ids[:100],  # cap at 100
        })
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
        "qseqid", "sseqid", "pident", "length", "mismatch", "gapopen",
        "qstart", "qend", "sstart", "send", "evalue", "bitscore",
    ]
    custom_columns: list[str] | None = None

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # Parse column header from comment line
        if line.startswith("# Fields:"):
            field_str = line[len("# Fields:"):].strip()
            field_map = {
                "query acc.ver": "qseqid", "subject acc.ver": "sseqid",
                "% identity": "pident", "alignment length": "length",
                "mismatches": "mismatch", "gap opens": "gapopen",
                "q. start": "qstart", "q. end": "qend",
                "s. start": "sstart", "s. end": "send",
                "evalue": "evalue", "bit score": "bitscore",
                "query acc.": "qseqid", "subject acc.": "sseqid",
                "query id": "qseqid", "subject id": "sseqid",
                "% positives": "ppos", "query length": "qlen",
                "subject length": "slen", "query seq": "qseq",
                "subject seq": "sseq",
            }
            raw_fields = [f.strip() for f in field_str.split(",")]
            custom_columns = [field_map.get(f, f.replace(" ", "_").replace(".", "")) for f in raw_fields]
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
            elif col in ("length", "mismatch", "gapopen", "qstart", "qend", "sstart", "send", "qlen", "slen"):
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
    import math

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
    evalue_bins: dict[str, int] = {"0": 0, "1e-200..1e-100": 0, "1e-100..1e-50": 0,
                                    "1e-50..1e-10": 0, "1e-10..1e-5": 0,
                                    "1e-5..0.01": 0, "0.01..1": 0, "1..10": 0, ">10": 0}
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
        label = f"{pct}-{pct+10}%"
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

        # Try Data Plane access
        dbs = storage_data_svc.list_databases(
            cred,
            params["storage_account"],
        )
    except Exception as exc:
        msg = str(exc)
        if "AuthorizationFailure" in msg or "AuthorizationPermissionMismatch" in msg:
            return _error_response(403, (
                "Storage data-plane access denied. "
                "Assign 'Storage Blob Data Reader' (or Contributor) "
                "role to your account on this storage account."
            ))
        return _error_response(500, sanitise(msg))
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


@app.orchestration_trigger(context_name="context")
def deploy_openapi_orchestrator(context):
    """Re-deploy the OpenAPI service to an existing AKS cluster."""
    from orchestrators import provision_aks as _prov_aks
    return _prov_aks.deploy_openapi_orchestrator(context)


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
def assign_vm_roles_activity(payload: dict) -> dict:
    return terminal_activities.activity_assign_vm_roles(payload)


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
def check_elastic_blast_prepare_activity(payload: dict) -> dict:
    return blast_activities.activity_check_elastic_blast_prepare(payload)


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
        "oidc_issuer_profile": {"enabled": True},
        "security_profile": {"workload_identity": {"enabled": True}},
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


@app.activity_trigger(input_name="payload")
def setup_workload_identity_activity(payload: dict) -> dict:
    """Activity: create User-Assigned MI, Federated Credential, and assign roles.

    Enables the OpenAPI pod to authenticate as an Azure identity without
    az login. Idempotent — safe to re-run on existing clusters.
    """
    import uuid as _uuid
    from services.azure_clients import credential_for_assertion
    cred = credential_for_assertion(payload["user_assertion"])
    sub = payload["subscription_id"]
    rg = payload["resource_group"]
    cluster_name = payload["cluster_name"]
    region = payload["region"]
    mi_name = payload.get("mi_name", "id-elb-openapi")
    k8s_sa_name = payload.get("k8s_sa_name", "elb-openapi-sa")
    k8s_namespace = payload.get("k8s_namespace", "default")
    fed_cred_name = payload.get("fed_cred_name", "fc-elb-openapi")

    # 1. Get OIDC issuer URL from AKS
    from azure.mgmt.containerservice import ContainerServiceClient
    aks_client = ContainerServiceClient(cred, sub)
    cluster = aks_client.managed_clusters.get(rg, cluster_name)
    oidc_url = ""
    if cluster.oidc_issuer_profile:
        oidc_url = cluster.oidc_issuer_profile.issuer_url or ""
    if not oidc_url:
        return {"error": "OIDC issuer not enabled on cluster"}

    # 2. Create User-Assigned Managed Identity
    from azure.mgmt.msi import ManagedServiceIdentityClient
    msi_client = ManagedServiceIdentityClient(cred, sub)
    mi = msi_client.user_assigned_identities.create_or_update(
        rg, mi_name,
        {"location": region, "tags": {"purpose": "elb-openapi-workload-identity"}},
    )
    mi_client_id = mi.client_id
    mi_principal_id = mi.principal_id

    # 3. Create Federated Identity Credential
    msi_client.federated_identity_credentials.create_or_update(
        rg, mi_name, fed_cred_name,
        {
            "issuer": oidc_url,
            "subject": f"system:serviceaccount:{k8s_namespace}:{k8s_sa_name}",
            "audiences": ["api://AzureADTokenExchange"],
        },
    )

    # 4. Assign roles to the MI
    from azure.mgmt.authorization import AuthorizationManagementClient
    auth_client = AuthorizationManagementClient(cred, sub)

    # Storage Blob Data Contributor on workload RG (for azcopy/blob access)
    storage_account = payload.get("storage_account", "")
    storage_rg = payload.get("storage_resource_group", rg)
    if storage_account:
        scope = f"/subscriptions/{sub}/resourceGroups/{storage_rg}/providers/Microsoft.Storage/storageAccounts/{storage_account}"
        _assign_role(auth_client, scope, mi_principal_id, "ba92f5b4-2d11-453d-a403-e96b0029c9fe")

    # Azure Kubernetes Service Cluster User Role on the cluster (for kubectl)
    cluster_scope = f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.ContainerService/managedClusters/{cluster_name}"
    _assign_role(auth_client, cluster_scope, mi_principal_id, "4abbcc35-e782-43d8-92c5-2d3f1bd2253f")

    return {
        "mi_name": mi_name,
        "mi_client_id": mi_client_id,
        "mi_principal_id": mi_principal_id,
        "oidc_issuer": oidc_url,
        "federated_credential": fed_cred_name,
    }


@app.activity_trigger(input_name="payload")
def deploy_openapi_activity(payload: dict) -> dict:
    """Activity: deploy elb-openapi to AKS with Workload Identity ServiceAccount.

    Creates a K8s ServiceAccount annotated with the MI client-id, then applies
    the Deployment + Service manifest. Idempotent.
    """
    import json as _json
    from services.azure_clients import credential_for_assertion
    from services.image_tags import IMAGE_TAGS
    cred = credential_for_assertion(payload["user_assertion"])
    sub = payload["subscription_id"]
    rg = payload["resource_group"]
    cluster_name = payload["cluster_name"]
    mi_client_id = payload.get("mi_client_id", "")
    k8s_sa_name = payload.get("k8s_sa_name", "elb-openapi-sa")
    acr_name = payload.get("acr_name", "")
    storage_account = payload.get("storage_account", "")
    image_tag = IMAGE_TAGS.get("elb-openapi", "2.0")
    image = f"{acr_name}.azurecr.io/elb-openapi:{image_tag}" if acr_name else f"elb-openapi:{image_tag}"

    from azure.mgmt.containerservice import ContainerServiceClient
    aks_client = ContainerServiceClient(cred, sub)

    # Get admin kubeconfig to apply manifests
    cred_result = aks_client.managed_clusters.list_cluster_admin_credentials(rg, cluster_name)
    kubeconfig = cred_result.kubeconfigs[0].value.decode("utf-8")

    import tempfile, subprocess
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as kf:
        kf.write(kubeconfig)
        kubeconfig_path = kf.name

    try:
        env = {**dict(__import__("os").environ), "KUBECONFIG": kubeconfig_path}

        # ServiceAccount with Workload Identity annotation
        sa_manifest = {
            "apiVersion": "v1", "kind": "ServiceAccount",
            "metadata": {
                "name": k8s_sa_name,
                "namespace": "default",
                "annotations": {"azure.workload.identity/client-id": mi_client_id} if mi_client_id else {},
                "labels": {"azure.workload.identity/use": "true"},
            },
        }

        # Deployment
        deploy_manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "elb-openapi", "namespace": "default"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "elb-openapi"}},
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "elb-openapi",
                            "azure.workload.identity/use": "true",
                        },
                    },
                    "spec": {
                        "serviceAccountName": k8s_sa_name,
                        "containers": [{
                            "name": "openapi",
                            "image": image,
                            "imagePullPolicy": "Always",
                            "ports": [{"containerPort": 8000}],
                            "env": [
                                {"name": "ELB_CLUSTER_NAME", "value": cluster_name},
                                {"name": "ELB_STORAGE_ACCOUNT", "value": storage_account},
                                {"name": "ELB_RESOURCE_GROUP", "value": rg},
                                {"name": "ELB_AZURE_REGION", "value": payload.get("region", "koreacentral")},
                                {"name": "AZURE_CLIENT_ID", "value": mi_client_id},
                                {"name": "AZCOPY_AUTO_LOGIN_TYPE", "value": "AZCLI"},
                                {"name": "AZCOPY_TENANT_ID", "value": payload.get("tenant_id", "")},
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                        }],
                    },
                },
            },
        }

        # Service
        svc_manifest = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "elb-openapi", "namespace": "default"},
            "spec": {
                "type": "LoadBalancer",
                "selector": {"app": "elb-openapi"},
                "ports": [{"port": 80, "targetPort": 8000}],
            },
        }

        # ClusterRole for OpenAPI pod K8s access
        role_manifest = {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
            "metadata": {"name": "elb-openapi-role"},
            "rules": [
                {"apiGroups": [""], "resources": ["nodes", "pods", "configmaps", "services"],
                 "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
                {"apiGroups": ["batch"], "resources": ["jobs"],
                 "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
                {"apiGroups": ["apps"], "resources": ["deployments"],
                 "verbs": ["get", "list", "watch"]},
            ],
        }

        # ClusterRoleBinding
        binding_manifest = {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
            "metadata": {"name": "elb-openapi-binding"},
            "subjects": [{"kind": "ServiceAccount", "name": k8s_sa_name, "namespace": "default"}],
            "roleRef": {"kind": "ClusterRole", "name": "elb-openapi-role", "apiGroup": "rbac.authorization.k8s.io"},
        }

        # Apply all manifests
        for manifest in [sa_manifest, role_manifest, binding_manifest, deploy_manifest, svc_manifest]:
            proc = subprocess.run(
                ["kubectl", "apply", "-f", "-"],
                input=_json.dumps(manifest), capture_output=True, text=True,
                timeout=30, env=env,
            )
            if proc.returncode != 0:
                LOGGER.warning("kubectl apply warning: %s", proc.stderr[:200])

        # Wait for external IP (up to 120s)
        import time
        external_ip = ""
        for _ in range(12):
            proc = subprocess.run(
                ["kubectl", "get", "svc", "elb-openapi", "-o", "jsonpath={.status.loadBalancer.ingress[0].ip}"],
                capture_output=True, text=True, timeout=10, env=env,
            )
            if proc.stdout.strip():
                external_ip = proc.stdout.strip()
                break
            time.sleep(10)

        return {"status": "deployed", "image": image, "external_ip": external_ip}
    finally:
        __import__("os").unlink(kubeconfig_path)


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

    # Delete KV secret — try the canonical (sub, rg, vm)-hash vault first,
    # then the legacy `kv-elb-{vm[-8:]}` fallback.
    candidate_vaults: list[str] = []
    env_uri = os.environ.get("KEY_VAULT_URI")
    if env_uri:
        candidate_vaults.append(env_uri.rstrip("/") + "/")
    try:
        from activities.terminal import _default_vault_name
        canonical = _default_vault_name(sub, rg, vm_name)
        candidate_vaults.append(f"https://{canonical}.vault.azure.net/")
    except Exception:
        pass
    legacy_suffix = vm_name[-8:] if len(vm_name) >= 8 else vm_name
    candidate_vaults.append(f"https://kv-elb-{legacy_suffix}.vault.azure.net/")
    for vault_uri in candidate_vaults:
        try:
            kv_svc.delete_secret(cred, vault_uri, f"vm-{vm_name}-password")
            break
        except Exception:
            continue

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


# ═══════════════════════════════════════════════════════════════════════
# P6 — Report Generator (CSV / JSON export)
# ═══════════════════════════════════════════════════════════════════════
@app.route(route="blast/jobs/{job_id}/results/export", methods=["GET"])
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
            cred, params["storage_account"], "results", f"{job_id}/",
        )
        out_blobs = [b for b in blobs if b["name"].endswith(".out")]
        all_hits: list[dict] = []
        for blob_info in out_blobs[:20]:
            try:
                content = storage_data_svc.read_blob_text(
                    cred, params["storage_account"], "results",
                    blob_info["name"], max_bytes=10 * 1024 * 1024,
                )
                all_hits.extend(_parse_blast_tabular(content))
            except Exception:
                pass

        if fmt == "json":
            body = json.dumps({"job_id": job_id, "hits": all_hits, "total": len(all_hits)}, default=str)
            return func.HttpResponse(body, status_code=200, mimetype="application/json",
                                     headers={"Content-Disposition": f'attachment; filename="{job_id}_results.json"'})

        # CSV / TSV
        import csv
        import io
        delimiter = "\t" if fmt == "tsv" else ","
        cols = ["qseqid", "sseqid", "pident", "length", "mismatch", "gapopen",
                "qstart", "qend", "sstart", "send", "evalue", "bitscore"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, delimiter=delimiter, extrasaction="ignore")
        writer.writeheader()
        for h in all_hits:
            writer.writerow(h)
        ext = "tsv" if fmt == "tsv" else "csv"
        mime = "text/tab-separated-values" if fmt == "tsv" else "text/csv"
        return func.HttpResponse(buf.getvalue(), status_code=200, mimetype=mime,
                                 headers={"Content-Disposition": f'attachment; filename="{job_id}_results.{ext}"'})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)[:500]))


# ═══════════════════════════════════════════════════════════════════════
# P9 — Audit Trail
# ═══════════════════════════════════════════════════════════════════════
@app.entity_trigger(context_name="context")
def audit_trail_entity(context):
    """Durable entity that stores an immutable audit log of BLAST operations."""
    state: list[dict] = context.get_state(lambda: [])
    op = context.operation_name

    if op == "log_event":
        entry = context.get_input()
        entry["timestamp"] = entry.get("timestamp") or __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat()
        state.append(entry)
        # Keep last 10000 entries
        if len(state) > 10000:
            state = state[-10000:]
        context.set_state(state)

    elif op == "list_events":
        context.set_result(state)


@app.route(route="audit/log", methods=["GET"])
def list_audit_log(req: func.HttpRequest) -> func.HttpResponse:
    """List audit trail events."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    limit = min(int(req.params.get("limit", "100")), 500)
    action_filter = req.params.get("action", "")

    client = df.DurableOrchestrationClient(req)
    try:
        entity_id = df.EntityId("audit_trail_entity", "global")
        resp = client.read_entity_state(entity_id)
        events: list = resp.entity_state if resp.entity_exists else []
    except Exception:
        events = []

    # Filter and limit
    if action_filter:
        events = [e for e in events if e.get("action") == action_filter]
    events = events[-limit:]
    events.reverse()  # newest first

    return _json_response({"events": events, "total": len(events)})


# ═══════════════════════════════════════════════════════════════════════
# P11 — Cost Estimator
# ═══════════════════════════════════════════════════════════════════════
_AZURE_VM_HOURLY_USD: dict[str, float] = {
    "Standard_D2s_v5": 0.096, "Standard_D4s_v5": 0.192,
    "Standard_D8s_v5": 0.384, "Standard_D16s_v5": 0.768,
    "Standard_E4s_v5": 0.252, "Standard_E8s_v5": 0.504,
    "Standard_E16s_v5": 1.008, "Standard_E32s_v5": 2.016,
    "Standard_E48s_v5": 3.024, "Standard_E64s_v5": 4.032,
    "Standard_D2s_v3": 0.096, "Standard_D4s_v3": 0.192,
}
_STORAGE_GB_MONTH_USD = 0.018  # Hot tier
_PD_GB_MONTH_USD = 0.040  # Managed SSD


@app.route(route="blast/cost-estimate", methods=["POST"])
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

    return _json_response({
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
    })


# ═══════════════════════════════════════════════════════════════════════
# P8 — Multi-DB Search
# ═══════════════════════════════════════════════════════════════════════
@app.route(route="blast/multi-submit", methods=["POST"])
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

    return _json_response({
        "group_id": group_id,
        "jobs": jobs,
        "total": len(jobs),
    })


# ═══════════════════════════════════════════════════════════════════════
# P4 — Taxonomy Annotation
# ═══════════════════════════════════════════════════════════════════════
@app.route(route="blast/taxonomy", methods=["POST"])
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
            pass

    return _json_response({
        "annotations": results,
        "found": len(results),
        "requested": len(accessions),
    })


# ═══════════════════════════════════════════════════════════════════════
# P5 — Query Preprocessor (FASTQ→FASTA, stats)
# ═══════════════════════════════════════════════════════════════════════
@app.route(route="blast/preprocess", methods=["POST"])
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
            return _error_response(400, "cannot detect format — must start with > (FASTA) or @ (FASTQ)")

    fasta_seqs: list[dict] = []
    stats = {"input_sequences": 0, "output_sequences": 0, "total_bases": 0,
             "filtered_short": 0, "filtered_quality": 0, "avg_length": 0.0,
             "min_len": 0, "max_len": 0, "gc_content": 0.0}

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
            stats["gc_content"] = round(gc_count / stats["total_bases"] * 100, 1) if stats["total_bases"] > 0 else 0

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
            stats["gc_content"] = round(gc_count / stats["total_bases"] * 100, 1) if stats["total_bases"] > 0 else 0

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
            fasta_output_lines.append(seq[j:j + 80])

    return _json_response({
        "fasta_output": "\n".join(fasta_output_lines),
        "stats": stats,
        "detected_format": input_format,
    })


# ═══════════════════════════════════════════════════════════════════════
# P2 — DB Version Registry
# ═══════════════════════════════════════════════════════════════════════
@app.route(route="blast/databases/versions", methods=["GET"])
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
                    meta["_last_modified"] = blob.last_modified.isoformat() if blob.last_modified else None
                    meta["_size_bytes"] = blob.size
                    versions.append(meta)
                except Exception:
                    pass

        # Sort by created_at or downloaded_at
        versions.sort(
            key=lambda v: v.get("created_at") or v.get("downloaded_at") or "",
            reverse=True,
        )
        return _json_response({"versions": versions, "total": len(versions)})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)[:500]))


@app.route(route="blast/databases/versions", methods=["POST"])
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
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "created_by": identity.upn or identity.oid,
    }

    try:
        blob_path = f"{db_name}/{db_name}-metadata.json"
        storage_data_svc.upload_query_text(
            cred, storage_account, "blast-db", blob_path,
            json.dumps(metadata, indent=2),
        )
        return _json_response({"db_name": db_name, "status": "saved", "metadata": metadata})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)[:500]))


# ═══════════════════════════════════════════════════════════════════════
# P7 — Scheduled / Triggered BLAST
# ═══════════════════════════════════════════════════════════════════════
@app.entity_trigger(context_name="context")
def scheduled_blast_entity(context):
    """Durable entity storing scheduled BLAST job configurations."""
    state: list[dict] = context.get_state(lambda: [])
    op = context.operation_name

    if op == "add_schedule":
        entry = context.get_input()
        entry["schedule_id"] = entry.get("schedule_id") or uuid.uuid4().hex[:12]
        entry["enabled"] = True
        entry["created_at"] = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat()
        entry["last_run"] = None
        entry["run_count"] = 0
        state.append(entry)
        context.set_state(state)
        context.set_result(entry)

    elif op == "list_schedules":
        context.set_result(state)

    elif op == "toggle_schedule":
        inp = context.get_input()
        sid = inp.get("schedule_id")
        for s in state:
            if s.get("schedule_id") == sid:
                s["enabled"] = not s.get("enabled", True)
                break
        context.set_state(state)

    elif op == "remove_schedule":
        sid = context.get_input().get("schedule_id")
        state = [s for s in state if s.get("schedule_id") != sid]
        context.set_state(state)

    elif op == "mark_run":
        inp = context.get_input()
        sid = inp.get("schedule_id")
        for s in state:
            if s.get("schedule_id") == sid:
                s["last_run"] = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat()
                s["run_count"] = s.get("run_count", 0) + 1
                break
        context.set_state(state)


@app.route(route="blast/schedules", methods=["GET"])
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


@app.route(route="blast/schedules", methods=["POST"])
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
            k: v for k, v in body.items()
            if k not in ("name", "trigger_type", "cron_expression", "watch_container", "watch_prefix")
        },
        "owner_upn": identity.upn,
    }

    client = df.DurableOrchestrationClient(req)
    entity_id = df.EntityId("scheduled_blast_entity", "global")
    client.signal_entity(entity_id, "add_schedule", schedule_config)

    return _json_response({"status": "created", "schedule": schedule_config})


@app.route(route="blast/schedules/{schedule_id}", methods=["DELETE"])
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


@app.route(route="blast/schedules/{schedule_id}/run", methods=["POST"])
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

    return _json_response({
        "job_id": job_id,
        "instance_id": instance_id,
        "schedule_id": schedule_id,
    })


# ═══════════════════════════════════════════════════════════════════════
# P12 — Primer Design (Primer3 on Terminal VM)
# ═══════════════════════════════════════════════════════════════════════
@app.route(route="blast/primer-design", methods=["POST"])
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

    terminal_rg = body.get("terminal_resource_group", os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal"))
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

        return _json_response({
            "primers": primers,
            "target": {"start": target_start, "length": target_length},
            "product_size_range": f"{product_min}-{product_max}",
            "sequence_length": len(clean_seq),
        })
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

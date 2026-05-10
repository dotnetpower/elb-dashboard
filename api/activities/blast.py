"""Activities for BLAST job submission and lifecycle management.

Each activity is single-purpose, idempotent, and side-effect tagged.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from services import compute as compute_svc
from services import storage_data as storage_data_svc
from services import monitoring as monitoring_svc
from services.azure_clients import credential_for_caller
from services.blast_config import generate_config
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

_SAFE_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]+$")


def _get_vm_ssh_password(credential, payload: dict[str, Any]) -> str | None:
    """Try to get VM SSH password for fast SSH execution.

    Returns password string or None if unavailable (falls back to Run Command).
    """
    vault_url = (payload.get("keyvault_url")
                or os.environ.get("ELB_KEYVAULT_URL", "")
                or os.environ.get("KEY_VAULT_URI", ""))
    vm_name = payload.get("terminal_vm_name", "vm-elb-terminal")
    if not vault_url:
        return None
    try:
        from services.keyvault import get_secret
        return get_secret(credential, vault_url, f"vm-{vm_name}-password")
    except Exception as exc:
        LOGGER.debug("Could not get SSH password from Key Vault: %s", exc)
        return None


def _validate_job_id(job_id: str) -> str:
    """Validate job_id is safe for shell interpolation."""
    if not job_id or not _SAFE_JOB_ID.match(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    return job_id


def activity_upload_query(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: uploads FASTA query text to blob storage.

    Retries up to 3 times with 5s delay to handle storage network propagation.
    """
    import time as _time
    cred = credential_for_caller(payload.get("user_assertion"))
    account = payload["storage_account"]
    job_id = _validate_job_id(payload["job_id"])
    blob_path = f"{job_id}/input.fa"

    last_exc = None
    for attempt in range(3):
        try:
            url = storage_data_svc.upload_query_text(
                cred, account, "queries", blob_path, payload["query_data"],
            )
            LOGGER.info("uploaded query to %s", url)
            return {"query_blob_url": url, "blob_path": blob_path}
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                LOGGER.warning("upload_query attempt %d failed (%s), retrying in 5s", attempt + 1, exc)
                _time.sleep(5)
    raise last_exc  # type: ignore[misc]


def activity_generate_blast_config(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: generates INI config and uploads to storage."""
    cred = credential_for_caller(payload.get("user_assertion"))
    account = payload["storage_account"]
    job_id = _validate_job_id(payload["job_id"])

    config_text = generate_config(payload)
    blob_path = f"{job_id}/elastic-blast.ini"
    url = storage_data_svc.upload_query_text(
        cred,
        account,
        "queries",
        blob_path,
        config_text,
    )
    LOGGER.info("uploaded config to %s", url)
    return {"config_blob_url": url, "config_text": config_text}


def activity_run_elastic_blast_submit(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: runs elastic-blast submit on the Remote Terminal VM.

    Instead of using azcopy to download the config (which requires az login),
    we write the config directly to the VM via Run Command. The config was
    already generated and is available in the payload.

    Config write + submit are combined into a single Run Command to avoid
    the ~30-60s Azure Run Command overhead per call.
    """
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])

    config_text = payload.get("config_text", "")
    if not config_text:
        config_text = generate_config(payload)

    import base64
    config_b64 = base64.b64encode(config_text.encode("utf-8")).decode("ascii")

    # Single Run Command: write config + run submit (saves ~30-60s overhead)
    # ELB_DISABLE_JOB_SUBMISSION_ON_THE_CLOUD=1 makes elastic-blast submit
    # BLAST jobs directly via kubectl instead of creating a submit-jobs K8s pod.
    # This avoids the batch_list.txt dependency that fails in warm-cluster mode.
    combined_script = (
        f"#!/bin/bash\n"
        f"printf '%s' '{config_b64}' | base64 -d > /tmp/elb-{job_id}.ini\n"
        f"chmod 644 /tmp/elb-{job_id}.ini\n"
        f"chown azureuser:azureuser /tmp/elb-{job_id}.ini\n"
        f"chown -R azureuser:azureuser /home/azureuser/.azcopy /home/azureuser/.azure 2>/dev/null\n"
        f"sudo -u azureuser bash -c '"
        f"export HOME=/home/azureuser && "
        f"export AZCOPY_AUTO_LOGIN_TYPE=AZCLI && "
        f"export ELB_DISABLE_JOB_SUBMISSION_ON_THE_CLOUD=1 && "
        f"if ! az account show -o none 2>/dev/null; then az login --identity -o none 2>/dev/null || true; fi && "
        f"cd /home/azureuser/elastic-blast-azure && export PYTHONPATH=src:$PYTHONPATH && "
        f"source venv/bin/activate && "
        f"python bin/elastic-blast submit --cfg /tmp/elb-{job_id}.ini 2>&1 | tail -50; "
        f"echo EXIT_CODE=$?'"
    )
    ssh_pw = _get_vm_ssh_password(cred, payload)
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        payload["terminal_resource_group"],
        payload["terminal_vm_name"],
        combined_script,
        ssh_password=ssh_pw,
    )
    sanitised = sanitise(output)[:4000]
    LOGGER.info("elastic-blast submit output: %s", sanitised[:500])

    exit_code = _parse_exit_code(output)
    success = exit_code == 0
    if not success:
        LOGGER.warning("elastic-blast submit failed with EXIT_CODE=%d", exit_code)
    return {
        "output": sanitised,
        "success": success,
        "job_id": job_id,
    }


def activity_check_blast_status(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: runs elastic-blast status on the Remote Terminal VM."""
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])

    script = (
        f"#!/bin/bash\n"
        f"sudo -u azureuser bash -c '"
        f"export HOME=/home/azureuser && "
        f"if ! az account show -o none 2>/dev/null; then az login --identity -o none 2>/dev/null || true; fi && "
        f"cd /home/azureuser/elastic-blast-azure && export PYTHONPATH=src:$PYTHONPATH && "
        f"source venv/bin/activate && "
        f"python bin/elastic-blast status --cfg /tmp/elb-{job_id}.ini --exit-code 2>&1 | tail -20; "
        f"echo EXIT_CODE=$?'"
    )
    ssh_pw = _get_vm_ssh_password(cred, payload)
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        payload["terminal_resource_group"],
        payload["terminal_vm_name"],
        script,
        ssh_password=ssh_pw,
    )
    sanitised = sanitise(output)[:4000]

    exit_code = _parse_exit_code(output)

    status_map = {
        0: "completed",
        1: "failed",
        2: "creating",
        3: "submitting",
        4: "running",
        5: "deleting",
        6: "unknown",
    }
    status = status_map.get(exit_code, "unknown")
    return {"status": status, "exit_code": exit_code, "output": sanitised}


def _parse_exit_code(output: str) -> int:
    """Extract EXIT_CODE=N from shell output."""
    for line in output.strip().split("\n"):
        if line.startswith("EXIT_CODE="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                LOGGER.warning("failed to parse EXIT_CODE from line: %s", line[:80])
    return 6  # UNKNOWN


def activity_run_elastic_blast_prepare(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: runs elastic-blast prepare on the Remote Terminal VM.

    Prepares the AKS cluster with DB shards for warm execution.
    This creates the cluster, downloads DB shards to local SSDs, but does NOT
    run BLAST jobs. Use submit with reuse=true afterwards.
    """
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])

    config_text = payload.get("config_text", "")
    if not config_text:
        config_text = generate_config(payload)

    import base64
    config_b64 = base64.b64encode(config_text.encode("utf-8")).decode("ascii")

    # Single Run Command: write config + run prepare (saves ~30-60s overhead)
    # ELB_DISABLE_JOB_SUBMISSION_ON_THE_CLOUD=1 ensures prepare doesn't
    # set up the submit-jobs K8s pod path (consistent with submit activity).
    combined_script = (
        f"#!/bin/bash\n"
        f"set -o pipefail\n"
        f"printf '%s' '{config_b64}' | base64 -d > /tmp/elb-{job_id}.ini\n"
        f"chmod 644 /tmp/elb-{job_id}.ini\n"
        f"chown azureuser:azureuser /tmp/elb-{job_id}.ini\n"
        f"chown -R azureuser:azureuser /home/azureuser/.azcopy /home/azureuser/.azure 2>/dev/null\n"
        f"sudo -u azureuser bash -c '"
        f"set -o pipefail && "
        f"export HOME=/home/azureuser && "
        f"export AZCOPY_AUTO_LOGIN_TYPE=AZCLI && "
        f"export ELB_DISABLE_JOB_SUBMISSION_ON_THE_CLOUD=1 && "
        f"if ! az account show -o none 2>/dev/null; then az login --identity -o none 2>/dev/null || true; fi && "
        f"cd /home/azureuser/elastic-blast-azure && export PYTHONPATH=src:$PYTHONPATH && "
        f"source venv/bin/activate && "
        f"python bin/elastic-blast prepare --cfg /tmp/elb-{job_id}.ini 2>&1 | tail -80; "
        f"echo EXIT_CODE=$?'"
    )
    ssh_pw = _get_vm_ssh_password(cred, payload)
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        payload["terminal_resource_group"],
        payload["terminal_vm_name"],
        combined_script,
        ssh_password=ssh_pw,
    )
    sanitised = sanitise(output)[:2000]
    LOGGER.info("elastic-blast prepare output: %s", sanitised[:500])

    exit_code = _parse_exit_code(output)
    # Also detect ERROR lines in output (exit code from piped commands may be 0)
    has_error = any(
        line.strip().startswith("ERROR:") for line in output.split("\n")
        if line.strip().startswith("ERROR:")
    )
    success = exit_code == 0 and not has_error
    return {
        "output": sanitised,
        "success": success,
        "job_id": job_id,
    }


def activity_run_elastic_blast_delete(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: runs elastic-blast delete on the Remote Terminal VM."""
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])

    script = (
        f"#!/bin/bash\n"
        f"sudo -u azureuser bash -c '"
        f"export HOME=/home/azureuser && "
        f"cd /home/azureuser/elastic-blast-azure && export PYTHONPATH=src:$PYTHONPATH && "
        f"source venv/bin/activate && "
        f"python bin/elastic-blast delete --cfg /tmp/elb-{job_id}.ini 2>&1 | tail -20'"
    )
    ssh_pw = _get_vm_ssh_password(cred, payload)
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        payload["terminal_resource_group"],
        payload["terminal_vm_name"],
        script,
        ssh_password=ssh_pw,
    )
    return {"output": sanitise(output)[:4000], "success": True}


def activity_export_blast_results(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: ensures BLAST results are saved to blob storage.

    elastic-blast should write results directly to the results URL in config.
    This activity serves as a fallback — captures elastic-blast status output
    and any remaining pod logs, uploading them to blob storage.
    Designed to complete quickly (< 60s) to avoid blocking the orchestrator.
    """
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])
    account = payload["storage_account"]

    # Export script: check az login, fallback to managed identity, list results, capture logs
    export_script = (
        f"#!/bin/bash\n"
        f"sudo -u azureuser bash -c '\n"
        f"export HOME=/home/azureuser\n"
        f"export AZCOPY_AUTO_LOGIN_TYPE=AZCLI\n"
        f"cd /home/azureuser/elastic-blast-azure\n"
        f"export PYTHONPATH=src:$PYTHONPATH\n"
        f"source venv/bin/activate\n"
        f"\n"
        f"CFG=/tmp/elb-{job_id}.ini\n"
        f"RESULTS_DIR=/tmp/blast-results-{job_id}\n"
        f"RESULTS_URL=\"https://{account}.blob.core.windows.net/results/{job_id}\"\n"
        f"mkdir -p $RESULTS_DIR\n"
        f"EXPORT_ERRORS=0\n"
        f"\n"
        f"# 0. Check az login — fallback to managed identity if expired\n"
        f"if ! az account show --query user.name -o tsv 2>/dev/null; then\n"
        f"  echo \"AZ_LOGIN=expired, trying managed identity...\"\n"
        f"  if az login --identity 2>/dev/null; then\n"
        f"    echo \"AZ_LOGIN=managed_identity\"\n"
        f"  else\n"
        f"    echo \"AZ_LOGIN=failed\"\n"
        f"    EXPORT_ERRORS=$((EXPORT_ERRORS+1))\n"
        f"  fi\n"
        f"else\n"
        f"  AZ_USER=$(az account show --query user.name -o tsv 2>/dev/null)\n"
        f"  echo \"AZ_LOGIN=ok user=$AZ_USER\"\n"
        f"fi\n"
        f"\n"
        f"# 1. Get kubeconfig for AKS access\n"
        f"AKS_RG=\"{payload.get('resource_group', 'rg-elb-0509')}\"\n"
        f"AKS_NAME=$(python3 -c \"import configparser; c=configparser.ConfigParser(); c.read(\\\"$CFG\\\"); print(c.get(\\\"cluster\\\",\\\"name\\\",fallback=\\\"unknown\\\"))\" 2>/dev/null | head -c 20)\n"
        f"echo \"AKS_CLUSTER_PREFIX=$AKS_NAME\"\n"
        f"# Find actual AKS cluster in the resource group\n"
        f"ACTUAL_AKS=$(az aks list -g $AKS_RG --query \"[0].name\" -o tsv 2>/dev/null || echo \"\")\n"
        f"if [ -n \"$ACTUAL_AKS\" ]; then\n"
        f"  echo \"AKS_CLUSTER=$ACTUAL_AKS\"\n"
        f"  az aks get-credentials -g $AKS_RG -n $ACTUAL_AKS --overwrite-existing 2>/dev/null && echo \"KUBECONFIG=ok\" || echo \"KUBECONFIG=failed\"\n"
        f"else\n"
        f"  echo \"AKS_CLUSTER=not_found\"\n"
        f"fi\n"
        f"\n"
        f"# 2. Capture elastic-blast status\n"
        f"timeout 30 python bin/elastic-blast status --cfg $CFG 2>&1 > $RESULTS_DIR/blast-status.txt || true\n"
        f"echo \"BLAST_STATUS=$(cat $RESULTS_DIR/blast-status.txt | tail -1)\"\n"
        f"\n"
        f"# 2. Parse results URL from config\n"
        f"CFG_RESULTS=$(python3 -c \"import configparser; c=configparser.ConfigParser(); c.read(\\\"$CFG\\\"); print(c.get(\\\"blast\\\",\\\"results\\\",fallback=\\\"\\\"))\" 2>/dev/null || echo \"\")\n"
        f"echo \"CONFIG_RESULTS_URL=$CFG_RESULTS\"\n"
        f"\n"
        f"# 3. List existing result blobs (using az storage since azcopy may lack auth)\n"
        f"EXISTING=$(az storage blob list --account-name {account} --container-name results --prefix \"{job_id}/\" --auth-mode login --query \"length([])\" -o tsv 2>/dev/null || echo 0)\n"
        f"echo \"EXISTING_RESULT_BLOBS=$EXISTING\"\n"
        f"\n"
        f"# 4. Get kubeconfig and capture pod logs\n"
        f"AKS_RG=\"{payload.get('resource_group', 'rg-elb-0509')}\"\n"
        f"NAMESPACE=$(python3 -c \"import configparser; c=configparser.ConfigParser(); c.read(\\\"$CFG\\\"); print(c.get(\\\"cluster\\\",\\\"name\\\",fallback=\\\"unknown\\\"))\" 2>/dev/null || echo \"unknown\")\n"
        f"echo \"K8S_NAMESPACE=$NAMESPACE\"\n"
        f"ACTUAL_AKS=$(az aks list -g $AKS_RG --query \"[0].name\" -o tsv 2>/dev/null || echo \"\")\n"
        f"if [ -n \"$ACTUAL_AKS\" ]; then\n"
        f"  echo \"AKS_CLUSTER=$ACTUAL_AKS\"\n"
        f"  az aks get-credentials -g $AKS_RG -n $ACTUAL_AKS --overwrite-existing 2>/dev/null\n"
        f"  # Find elastic-blast namespace\n"
        f"  ELB_NS=$(kubectl get ns --no-headers -o custom-columns=\":metadata.name\" 2>/dev/null | grep \"$NAMESPACE\" | head -1)\n"
        f"  [ -z \"$ELB_NS\" ] && ELB_NS=$NAMESPACE\n"
        f"  echo \"ELB_NAMESPACE=$ELB_NS\"\n"
        f"  timeout 10 kubectl get pods -n $ELB_NS -o wide 2>&1 > $RESULTS_DIR/pods.txt || true\n"
        f"  timeout 10 kubectl get jobs -n $ELB_NS -o wide 2>&1 > $RESULTS_DIR/jobs.txt || true\n"
        f"  echo \"PODS=$(cat $RESULTS_DIR/pods.txt | grep -c -v NAME 2>/dev/null || echo 0)\"\n"
        f"  for POD in $(timeout 5 kubectl get pods -n $ELB_NS -o jsonpath=\"{{.items[*].metadata.name}}\" 2>/dev/null || true); do\n"
        f"    echo \"POD_LOG=$POD\"\n"
        f"    timeout 10 kubectl logs $POD -n $ELB_NS --tail=500 > $RESULTS_DIR/$POD.log 2>/dev/null || true\n"
        f"  done\n"
        f"else\n"
        f"  echo \"AKS_CLUSTER=not_found\"\n"
        f"fi\n"
        f"\n"
        f"# 5. Upload logs/status artifacts using az storage (more reliable than azcopy)\n"
        f"UPLOADED=0\n"
        f"for FILE in $RESULTS_DIR/*.txt $RESULTS_DIR/*.log; do\n"
        f"  [ -f \"$FILE\" ] || continue\n"
        f"  BNAME=$(basename \"$FILE\")\n"
        f"  if timeout 15 az storage blob upload --account-name {account} --container-name results --name \"{job_id}/$BNAME\" --file \"$FILE\" --auth-mode login --overwrite 2>/dev/null; then\n"
        f"    UPLOADED=$((UPLOADED+1))\n"
        f"  fi\n"
        f"done\n"
        f"echo \"UPLOADED_ARTIFACTS=$UPLOADED\"\n"
        f"\n"
        f"# 6. Final count\n"
        f"FINAL=$(az storage blob list --account-name {account} --container-name results --prefix \"{job_id}/\" --auth-mode login --query \"length([])\" -o tsv 2>/dev/null || echo 0)\n"
        f"echo \"FINAL_RESULT_BLOBS=$FINAL\"\n"
        f"\n"
        f"if [ \"$FINAL\" -gt 0 ] 2>/dev/null; then\n"
        f"  echo \"EXPORT_OK\"\n"
        f"elif [ \"$EXPORT_ERRORS\" -gt 0 ]; then\n"
        f"  echo \"EXPORT_AUTH_FAILED\"\n"
        f"else\n"
        f"  echo \"EXPORT_EMPTY\"\n"
        f"fi\n"
        f"rm -rf $RESULTS_DIR\n"
        f"'"
    )

    ssh_pw = _get_vm_ssh_password(cred, payload)
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        payload["terminal_resource_group"],
        payload["terminal_vm_name"],
        export_script,
        ssh_password=ssh_pw,
    )
    sanitised = sanitise(output)[:2000]
    LOGGER.info("export_blast_results output: %s", sanitised[:500])

    success = "EXPORT_OK" in output
    auth_failed = "EXPORT_AUTH_FAILED" in output
    return {
        "output": sanitised,
        "success": success,
        "auth_failed": auth_failed,
        "job_id": job_id,
    }


def activity_list_result_blobs(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: none (read-only). Lists result blobs for a job."""
    cred = credential_for_caller(payload.get("user_assertion"))
    blobs = storage_data_svc.list_result_blobs(
        cred,
        payload["storage_account"],
        "results",
        payload.get("prefix", ""),
    )
    return {"blobs": blobs}


def activity_k8s_check_blast_status(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: none (read-only). Check BLAST job status via direct K8s API.

    ~1-3s vs ~30s for VM Run Command. Uses AKS kubeconfig to query pod/job status directly.
    """
    cred = credential_for_caller(payload.get("user_assertion"))
    namespace = payload.get("namespace", "")
    cluster_name = payload.get("cluster_name", "")
    if not namespace or not cluster_name:
        return {"status": "unknown", "error": "namespace or cluster_name missing"}

    result = monitoring_svc.k8s_check_blast_status(
        cred,
        payload["subscription_id"],
        payload["resource_group"],
        cluster_name,
        namespace,
    )
    LOGGER.info("K8s BLAST status for %s: %s", namespace, result.get("status"))
    return result


def activity_k8s_check_warmup_ready(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: none (read-only). Check if cluster namespace already has pods (warm).

    Returns {"warm": True/False}. If warm, warmup/prepare can be skipped.
    """
    cred = credential_for_caller(payload.get("user_assertion"))
    namespace = payload.get("namespace", "")
    cluster_name = payload.get("cluster_name", "")
    if not namespace or not cluster_name:
        return {"warm": False}

    is_warm = monitoring_svc.k8s_check_namespace_exists(
        cred,
        payload["subscription_id"],
        payload["resource_group"],
        cluster_name,
        namespace,
    )
    LOGGER.info("Warmup check for %s/%s: warm=%s", cluster_name, namespace, is_warm)
    return {"warm": is_warm}


def activity_list_databases(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: none (read-only). Lists available BLAST databases."""
    cred = credential_for_caller(payload.get("user_assertion"))
    dbs = storage_data_svc.list_databases(
        cred,
        payload["storage_account"],
        payload.get("container", "blast-db"),
    )
    return {"databases": dbs}


def activity_ensure_vm_running(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: starts VM if not running. Returns {started: bool, power_state: str}."""
    cred = credential_for_caller(payload.get("user_assertion"))
    from azure.mgmt.compute import ComputeManagementClient
    compute = ComputeManagementClient(cred, payload["subscription_id"])
    rg = payload["resource_group"]
    vm_name = payload["vm_name"]

    # Check current power state
    vm = compute.virtual_machines.instance_view(rg, vm_name)
    power_state = "unknown"
    for status in (vm.statuses or []):
        if status.code and status.code.startswith("PowerState/"):
            power_state = status.code.replace("PowerState/", "")
            break

    if power_state == "running":
        return {"started": False, "power_state": power_state}

    # Start the VM
    LOGGER.info("VM %s is %s, starting...", vm_name, power_state)
    try:
        poller = compute.virtual_machines.begin_start(rg, vm_name)
        poller.wait(timeout=180)
        LOGGER.info("VM %s started", vm_name)
        return {"started": True, "power_state": "running"}
    except Exception as exc:
        LOGGER.error("VM %s failed to start: %s", vm_name, exc)
        raise RuntimeError(f"VM {vm_name} failed to start: {exc}") from exc

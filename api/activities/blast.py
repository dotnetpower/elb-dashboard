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

_DEFAULT_TERMINAL_RG = "rg-elb-terminal"
_DEFAULT_TERMINAL_VM = "vm-elb-terminal"


def _terminal_rg(payload: dict[str, Any]) -> str:
    return payload.get("terminal_resource_group") or os.environ.get("TERMINAL_DEFAULT_RG", _DEFAULT_TERMINAL_RG)


def _terminal_vm(payload: dict[str, Any]) -> str:
    return payload.get("terminal_vm_name") or _DEFAULT_TERMINAL_VM

_SAFE_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]+$")


def _get_vm_ssh_password(credential, payload: dict[str, Any]) -> str | None:
    """Try to get VM SSH password for fast SSH execution.

    Returns password string or None if unavailable (falls back to Run Command).
    """
    import time as _time
    t0 = _time.time()
    vault_url = (payload.get("keyvault_url")
                or os.environ.get("ELB_KEYVAULT_URL", "")
                or os.environ.get("KEY_VAULT_URI", ""))
    vm_name = payload.get("terminal_vm_name", "vm-elb-terminal")
    if not vault_url:
        LOGGER.info("No Key Vault URL — SSH password unavailable (%.1fms)", (_time.time() - t0) * 1000)
        return None
    try:
        from services.keyvault import get_secret
        pw = get_secret(credential, vault_url, f"vm-{vm_name}-password")
        LOGGER.info("SSH password retrieved from KV in %.1fms", (_time.time() - t0) * 1000)
        return pw
    except Exception as exc:
        LOGGER.warning("Could not get SSH password from Key Vault (%.1fms): %s", (_time.time() - t0) * 1000, exc)
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
    query_data = payload["query_data"]
    query_bytes = len(query_data.encode("utf-8"))
    query_lines = len(query_data.splitlines())

    last_exc = None
    for attempt in range(3):
        try:
            url = storage_data_svc.upload_query_text(
                cred, account, "queries", blob_path, query_data,
            )
            LOGGER.info("uploaded query to %s", url)
            return {
                "query_blob_url": url,
                "blob_path": blob_path,
                "query_size_bytes": query_bytes,
                "query_line_count": query_lines,
                "attempts": attempt + 1,
            }
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
    return {
        "config_blob_url": url,
        "config_blob_path": blob_path,
        "config_text": config_text,
        "config_size_bytes": len(config_text.encode("utf-8")),
    }


def activity_run_elastic_blast_submit(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: runs elastic-blast submit via K8s exec on elb-openapi pod.

    Fast path (~30s): K8s exec on the pre-booted elb-openapi pod in AKS.
    Fallback (~60s): VM Run Command on the Remote Terminal VM.

    The elb-openapi pod has elastic-blast CLI + kubectl + azcopy pre-installed,
    eliminating VM Run Command overhead (~30s) and SSH GLIBC issues.
    """
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])

    config_text = payload.get("config_text", "")
    if not config_text:
        config_text = generate_config(payload)

    import base64
    config_b64 = base64.b64encode(config_text.encode("utf-8")).decode("ascii")

    aks_cluster = payload.get("aks_cluster_name", "")

    # Fast path: K8s exec on elb-openapi pod (~1s overhead vs ~30s Run Command)
    if aks_cluster:
        try:
            return _submit_via_k8s_exec(cred, payload, job_id, config_b64)
        except Exception as exc:
            LOGGER.warning("K8s exec submit failed (%s) — falling back to VM Run Command", exc)

    # Fallback: VM Run Command
    return _submit_via_vm(cred, payload, job_id, config_b64)


def _build_submit_args(config_b64: str, job_id: str) -> str:
    """Compose the bash one-liner that runs `elastic-blast submit` inside a pod.

    Hardened against the failure modes we hit in production:
    * Workload Identity federated token race on first scheduling — retry az
      login 5×5s.
    * elastic-blast lives in /usr/local/lib (system python) but azure.mgmt.*
      lives in /opt/venv. PYTHONPATH bridges the two.
    * Print EXIT_CODE marker so the polling activity can parse it from logs.
    """
    return (
        f'set -o pipefail; '
        f'printf "%s" "{config_b64}" | base64 -d > /tmp/elb-{job_id}.ini && '
        f'export PATH=/opt/venv/bin:$PATH && '
        f'AZ_OK=0; for i in 1 2 3 4 5; do '
        f'  if az login --service-principal -u "$AZURE_CLIENT_ID" --tenant "$AZURE_TENANT_ID" --federated-token "$(cat \"$AZURE_FEDERATED_TOKEN_FILE\")" -o none; then '
        f'    AZ_OK=1; break; '
        f'  fi; echo "az login attempt $i failed — sleeping 5s"; sleep 5; '
        f'done; '
        f'if [ "$AZ_OK" -ne 1 ]; then echo "az login failed after 5 attempts"; exit 2; fi; '
        f'export PYTHONPATH=/opt/venv/lib/python3.11/site-packages; '
        f'/usr/local/bin/elastic-blast submit --cfg /tmp/elb-{job_id}.ini 2>&1; '
        f'echo EXIT_CODE=$?'
    )


def _cleanup_stale_blast_jobs(session, server: str) -> None:
    """Best-effort: delete leftover blast/submit/setup/finalizer Jobs in default ns.

    elastic-blast's reuse-mode cleanup only runs when its `_db_already_loaded()`
    check passes. If a previous submit failed mid-flight (e.g. YAML parse
    error, OOM, network blip) the BLAST/setup/finalizer Jobs survive but the
    DB-loaded marker may not exist, so the next submit hits
    `field is immutable` on the Job names. Force-clean as a safety net.
    """
    for label in ("app=blast", "app=submit", "app=setup", "app=finalizer"):
        try:
            session.delete(
                f"{server}/apis/batch/v1/namespaces/default/jobs",
                params={"labelSelector": label, "propagationPolicy": "Background"},
                timeout=15,
            )
        except Exception as exc:
            LOGGER.debug("stale-job cleanup label=%s: %s", label, exc)


def activity_start_elastic_blast_submit(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: starts elastic-blast submit and returns immediately when possible."""
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])

    config_text = payload.get("config_text", "")
    if not config_text:
        config_text = generate_config(payload)

    import base64

    config_b64 = base64.b64encode(config_text.encode("utf-8")).decode("ascii")

    aks_cluster = payload.get("aks_cluster_name", "")
    if aks_cluster:
        try:
            return _start_submit_via_k8s_job(cred, payload, job_id, config_b64)
        except Exception as exc:
            LOGGER.warning("K8s submit start failed (%s) - falling back to VM Run Command", exc)

    result = _submit_via_vm(cred, payload, job_id, config_b64)
    return {**result, "done": True, "status": "succeeded" if result.get("success") else "failed"}


def activity_check_elastic_blast_submit(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: none (read-only). Polls a submit helper K8s Job and returns tail logs."""
    if payload.get("method") != "k8s_job":
        return {"done": True, "success": False, "status": "unsupported", "output": "Unsupported submit method for live polling."}
    cred = credential_for_caller(payload.get("user_assertion"))
    submit_job_name = payload.get("submit_job_name", "")
    if not submit_job_name:
        return {"done": True, "success": False, "status": "failed", "output": "Submit helper job name missing."}

    session, server = monitoring_svc._get_k8s_session(
        cred,
        payload["subscription_id"],
        payload["resource_group"],
        payload["aks_cluster_name"],
    )
    try:
        status_resp = session.get(
            f"{server}/apis/batch/v1/namespaces/default/jobs/{submit_job_name}",
            timeout=10,
        )
        if status_resp.status_code == 404:
            return {"done": True, "success": False, "status": "lost", "output": f"Submit helper job {submit_job_name} was not found."}
        if status_resp.status_code != 200:
            return {
                "done": False,
                "success": None,
                "status": "unknown",
                "output": f"Could not read submit helper job {submit_job_name}: HTTP {status_resp.status_code}",
            }

        job_status = status_resp.json().get("status", {})
        logs = _get_submit_job_logs(session, server, submit_job_name)
        output = sanitise(logs)[:6000]
        if job_status.get("succeeded", 0) > 0:
            exit_code = _parse_exit_code(logs)
            return {
                "done": True,
                "success": exit_code == 0,
                "status": "succeeded" if exit_code == 0 else "failed",
                "output": output,
                "submit_job_name": submit_job_name,
                "exit_code": exit_code,
                "method": "k8s_job",
            }
        if job_status.get("failed", 0) > 0:
            return {
                "done": True,
                "success": False,
                "status": "failed",
                "output": output or f"Submit helper job {submit_job_name} failed without logs.",
                "submit_job_name": submit_job_name,
                "method": "k8s_job",
            }
        return {
            "done": False,
            "success": None,
            "status": "running",
            "output": output or f"Submit helper job {submit_job_name} is starting...",
            "submit_job_name": submit_job_name,
            "method": "k8s_job",
        }
    finally:
        session.close()


def _submit_job_name(job_id: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", f"elb-submit-{job_id[:20]}".lower())[:63]


def _start_submit_via_k8s_job(
    cred, payload: dict[str, Any], job_id: str, config_b64: str,
) -> dict[str, Any]:
    import time as _time

    t0 = _time.time()
    session, server = monitoring_svc._get_k8s_session(
        cred,
        payload["subscription_id"],
        payload["resource_group"],
        payload["aks_cluster_name"],
    )
    try:
        resp = session.get(
            f"{server}/api/v1/namespaces/default/pods",
            params={"labelSelector": "app=elb-openapi", "limit": "1"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to list elb-openapi pods: {resp.status_code}")
        pods = resp.json().get("items", [])
        if not pods:
            raise RuntimeError("No elb-openapi pod found")

        submit_job_name = _submit_job_name(job_id)
        session.delete(
            f"{server}/apis/batch/v1/namespaces/default/jobs/{submit_job_name}",
            params={"propagationPolicy": "Background"},
            timeout=10,
        )

        # Pre-clean stale BLAST/submit jobs left from a previous incomplete
        # submit on this reused cluster. Without this, kubectl apply hits
        # `field is immutable` because the same Job names already exist.
        _cleanup_stale_blast_jobs(session, server)

        elb_image = pods[0]["spec"]["containers"][0]["image"]
        sa_name = pods[0]["spec"].get("serviceAccountName", "default")
        # Copy Workload Identity + ELB context env vars from the running pod.
        # Without ELB_AZURE_REGION / ELB_RESOURCE_GROUP / ELB_STORAGE_ACCOUNT
        # the elastic-blast CLI falls back to discovery code that fails inside
        # the submit pod (no IMDS for pod identity).
        pod_env: dict[str, str] = {}
        for container in pods[0]["spec"].get("containers", []):
            for env in container.get("env", []):
                name = env.get("name", "")
                if name.startswith(("AZURE_", "AZCOPY_", "ELB_")):
                    pod_env[name] = env.get("value", "")

        submit_env = [
            {"name": "ELB_DISABLE_JOB_SUBMISSION_ON_THE_CLOUD", "value": "1"},
            {"name": "ELB_SKIP_DB_VERIFY", "value": "true"},
            {"name": "PATH", "value": "/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        ]
        for key, value in pod_env.items():
            submit_env.append({"name": key, "value": value})

        submit_job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": submit_job_name,
                "labels": {"app": "elb-submit", "job-id": job_id[:63]},
            },
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 300,
                "template": {
                    "metadata": {"labels": {"app": "elb-submit", "azure.workload.identity/use": "true"}},
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": sa_name,
                        "containers": [{
                            "name": "submit",
                            "image": elb_image,
                            "command": ["bash", "-c"],
                            "args": [_build_submit_args(config_b64, job_id)],
                            "env": submit_env,
                            "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}},
                        }],
                    },
                },
            },
        }
        create_resp = session.post(
            f"{server}/apis/batch/v1/namespaces/default/jobs",
            json=submit_job_body,
            timeout=15,
        )
        if create_resp.status_code not in (200, 201):
            raise RuntimeError(f"Failed to create submit helper job: {create_resp.status_code} {create_resp.text[:200]}")
        LOGGER.info("Created submit job %s (%.1fs)", submit_job_name, _time.time() - t0)
        return {
            "done": False,
            "success": None,
            "status": "running",
            "method": "k8s_job",
            "submit_job_name": submit_job_name,
            "output": f"Created K8s submit helper job {submit_job_name}. Waiting for logs...",
            "job_id": job_id,
        }
    finally:
        session.close()


def _get_submit_job_logs(session, server: str, submit_job_name: str) -> str:
    pods_resp = session.get(
        f"{server}/api/v1/namespaces/default/pods",
        params={"labelSelector": f"job-name={submit_job_name}"},
        timeout=10,
    )
    if pods_resp.status_code != 200:
        return f"Waiting for submit pod list: HTTP {pods_resp.status_code}"

    lines: list[str] = []
    pods = pods_resp.json().get("items", [])
    if not pods:
        return "Waiting for submit pod to be scheduled..."

    for pod in pods:
        pod_name = pod.get("metadata", {}).get("name", "unknown")
        pod_status = pod.get("status", {})
        phase = pod_status.get("phase", "Unknown")
        lines.append(f"pod={pod_name} phase={phase}")
        for container_status in pod_status.get("containerStatuses", []) or []:
            state = container_status.get("state", {})
            waiting = state.get("waiting")
            terminated = state.get("terminated")
            if waiting:
                lines.append(
                    f"container={container_status.get('name', 'submit')} waiting={waiting.get('reason', 'Waiting')} {waiting.get('message', '')}".strip()
                )
            elif terminated:
                lines.append(
                    f"container={container_status.get('name', 'submit')} terminated={terminated.get('reason', 'Completed')} exit={terminated.get('exitCode', '?')}"
                )
        log_resp = session.get(
            f"{server}/api/v1/namespaces/default/pods/{pod_name}/log",
            params={"container": "submit", "tailLines": "120"},
            timeout=10,
        )
        if log_resp.status_code == 200 and log_resp.text:
            lines.append("--- Submit Console ---")
            lines.append(log_resp.text)
        elif log_resp.status_code not in (200, 400):
            lines.append(f"log read returned HTTP {log_resp.status_code}")
    return "\n".join(lines)


def _submit_via_k8s_exec(
    cred, payload: dict[str, Any], job_id: str, config_b64: str,
) -> dict[str, Any]:
    """Execute elastic-blast submit inside the elb-openapi pod via K8s API exec."""
    import time as _time
    t0 = _time.time()

    session, server = monitoring_svc._get_k8s_session(
        cred, payload["subscription_id"], payload["resource_group"],
        payload["aks_cluster_name"],
    )
    try:
        # Find elb-openapi pod
        resp = session.get(
            f"{server}/api/v1/namespaces/default/pods",
            params={"labelSelector": "app=elb-openapi", "limit": "1"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to list pods: {resp.status_code}")
        pods = resp.json().get("items", [])
        if not pods:
            raise RuntimeError("No elb-openapi pod found")
        pod_name = pods[0]["metadata"]["name"]
        LOGGER.info("Using elb-openapi pod %s for submit", pod_name)

        # Run submit as a short-lived K8s Job instead of using WebSocket exec.
        # This keeps the Function App out of SPDY/WebSocket details and gives
        # us normal pod logs for diagnostics.
        submit_job_name = f"elb-submit-{job_id[:20]}"
        submit_job_name = re.sub(r"[^a-z0-9-]", "-", submit_job_name.lower())[:63]

        # Delete any previous submit job for this job_id
        session.delete(
            f"{server}/apis/batch/v1/namespaces/default/jobs/{submit_job_name}",
            params={"propagationPolicy": "Background"},
            timeout=10,
        )

        # Pre-clean stale BLAST/submit/setup/finalizer Jobs from earlier failed
        # submits on this reused cluster. Without this, kubectl apply hits
        # `field is immutable` because the same Job names already exist.
        _cleanup_stale_blast_jobs(session, server)

        # Get the elb-openapi image and auth config from the running pod
        elb_image = pods[0]["spec"]["containers"][0]["image"]
        sa_name = pods[0]["spec"].get("serviceAccountName", "default")
        # Copy Workload Identity + ELB context env vars from the running pod.
        # Without ELB_AZURE_REGION / ELB_RESOURCE_GROUP / ELB_STORAGE_ACCOUNT
        # the elastic-blast CLI falls back to its own discovery code path
        # which fails inside the submit pod (no IMDS for the pod identity in
        # the way it expects).
        pod_env = {}
        for c in pods[0]["spec"]["containers"]:
            for env in c.get("env", []):
                name = env.get("name", "")
                if name.startswith(("AZURE_", "AZCOPY_", "ELB_")):
                    pod_env[name] = env.get("value", "")

        submit_env = [
            {"name": "ELB_DISABLE_JOB_SUBMISSION_ON_THE_CLOUD", "value": "1"},
            {"name": "ELB_SKIP_DB_VERIFY", "value": "true"},
            {"name": "PATH", "value": "/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        ]
        for k, v in pod_env.items():
            submit_env.append({"name": k, "value": v})

        submit_job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": submit_job_name,
                "labels": {"app": "elb-submit", "job-id": job_id[:63]},
            },
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 300,
                "template": {
                    "metadata": {"labels": {"app": "elb-submit", "azure.workload.identity/use": "true"}},
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": sa_name,
                        "containers": [{
                            "name": "submit",
                            "image": elb_image,
                            "command": ["bash", "-c"],
                            "args": [_build_submit_args(config_b64, job_id)],
                            "env": submit_env,
                            "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}},
                        }],
                    },
                },
            },
        }
        resp = session.post(
            f"{server}/apis/batch/v1/namespaces/default/jobs",
            json=submit_job_body,
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Failed to create submit job: {resp.status_code} {resp.text[:200]}")

        LOGGER.info("Created submit job %s (%.1fs)", submit_job_name, _time.time() - t0)

        # Poll for completion (10s intervals, max 3 min)
        for attempt in range(18):
            import time
            time.sleep(10)
            resp = session.get(
                f"{server}/apis/batch/v1/namespaces/default/jobs/{submit_job_name}",
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            status = resp.json().get("status", {})
            if status.get("succeeded", 0) > 0:
                # Get logs
                pods_resp = session.get(
                    f"{server}/api/v1/namespaces/default/pods",
                    params={"labelSelector": f"job-name={submit_job_name}"},
                    timeout=10,
                )
                output = ""
                if pods_resp.status_code == 200:
                    for p in pods_resp.json().get("items", []):
                        log_resp = session.get(
                            f"{server}/api/v1/namespaces/default/pods/{p['metadata']['name']}/log",
                            params={"container": "submit", "tailLines": "50"},
                            timeout=10,
                        )
                        if log_resp.status_code == 200:
                            output = log_resp.text

                sanitised = sanitise(output)[:4000]
                exit_code = _parse_exit_code(output)
                elapsed = _time.time() - t0
                LOGGER.info("K8s exec submit completed in %.1fs (exit=%d)", elapsed, exit_code)
                return {"output": sanitised, "success": exit_code == 0, "job_id": job_id, "method": "k8s_exec"}

            if status.get("failed", 0) > 0:
                raise RuntimeError(f"Submit job {submit_job_name} failed")

        raise RuntimeError(f"Submit job {submit_job_name} timed out")
    finally:
        session.close()


def _submit_via_vm(
    cred, payload: dict[str, Any], job_id: str, config_b64: str,
) -> dict[str, Any]:
    """Execute elastic-blast submit on the Remote Terminal VM via Run Command."""
    combined_script = (
        f"#!/bin/bash\n"
        f"printf '%s' '{config_b64}' | base64 -d > /tmp/elb-{job_id}.ini\n"
        f"chmod 644 /tmp/elb-{job_id}.ini\n"
        f"chown azureuser:azureuser /tmp/elb-{job_id}.ini\n"
        f"chown -R azureuser:azureuser /home/azureuser/.azcopy /home/azureuser/.azure 2>/dev/null\n"
        f"sudo -u azureuser bash -c '"
        f"export HOME=/home/azureuser && "
        f"export AZCOPY_AUTO_LOGIN_TYPE=MSI && "
        f"export ELB_DISABLE_JOB_SUBMISSION_ON_THE_CLOUD=1 && "
        f"export ELB_SKIP_DB_VERIFY=true && "
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
        _terminal_rg(payload),
        _terminal_vm(payload),
        combined_script,
        ssh_password=ssh_pw,
    )
    sanitised = sanitise(output)[:4000]
    LOGGER.info("VM submit output: %s", sanitised[:500])
    exit_code = _parse_exit_code(output)
    success = exit_code == 0
    if not success:
        LOGGER.warning("elastic-blast submit failed with EXIT_CODE=%d", exit_code)
    return {"output": sanitised, "success": success, "job_id": job_id, "method": "vm_run_command"}


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
        _terminal_rg(payload),
        _terminal_vm(payload),
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
    """side-effect: starts elastic-blast prepare on the Remote Terminal VM (background).

    Prepares the AKS cluster with DB shards for warm execution. This creates
    the cluster, downloads DB shards to local SSDs, but does NOT run BLAST
    jobs. Use submit with reuse=true afterwards.

    The actual `elastic-blast prepare` can take 10–30 minutes for large DBs,
    well beyond the Consumption-plan activity timeout (5 min hard cap on Y1).
    To stay within the timeout we **launch the prepare as a detached nohup
    process** on the VM and return immediately. The orchestrator then polls
    `activity_check_elastic_blast_prepare` (cheap, sub-30s) until a marker
    file (`/tmp/elb-<job_id>.done`) appears.

    Returns:
      {"started": True, "marker_path": "/tmp/elb-<job_id>.done",
       "log_path": "/tmp/elb-<job_id>.log", "pid_path": "/tmp/elb-<job_id>.pid"}
    """
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])

    config_text = payload.get("config_text", "")
    if not config_text:
        config_text = generate_config(payload)

    import base64
    config_b64 = base64.b64encode(config_text.encode("utf-8")).decode("ascii")

    marker = f"/tmp/elb-{job_id}.done"
    log_path = f"/tmp/elb-{job_id}.log"
    pid_path = f"/tmp/elb-{job_id}.pid"
    cfg_path = f"/tmp/elb-{job_id}.ini"

    # Fire-and-forget: write config, then launch prepare under nohup. The
    # outer `setsid` detaches from the SSH session; `&> log` captures both
    # streams. We write the marker file with the exit status so the poller
    # can detect both completion and error path.
    combined_script = (
        f"#!/bin/bash\n"
        f"set -o pipefail\n"
        f"printf '%s' '{config_b64}' | base64 -d > {cfg_path}\n"
        f"chmod 644 {cfg_path}\n"
        f"chown azureuser:azureuser {cfg_path}\n"
        f"chown -R azureuser:azureuser /home/azureuser/.azcopy /home/azureuser/.azure 2>/dev/null || true\n"
        f"# Skip if already running for this job\n"
        f"if [ -f {pid_path} ] && kill -0 \"$(cat {pid_path})\" 2>/dev/null; then\n"
        f"  echo ALREADY_RUNNING pid=$(cat {pid_path})\n"
        f"  exit 0\n"
        f"fi\n"
        f"# Launch prepare detached. The runner script writes its own PID and\n"
        f"# the marker on completion (with EXIT_CODE=N inside the marker).\n"
        f"sudo -u azureuser bash -c 'cat > /tmp/elb-{job_id}.runner.sh' <<'RUNNER'\n"
        f"#!/bin/bash\n"
        f"set -o pipefail\n"
        f"echo $$ > {pid_path}\n"
        f"export HOME=/home/azureuser\n"
        f"export AZCOPY_AUTO_LOGIN_TYPE=MSI\n"
        f"export ELB_DISABLE_JOB_SUBMISSION_ON_THE_CLOUD=1\n"
        f"if ! az account show -o none 2>/dev/null; then az login --identity -o none 2>/dev/null || true; fi\n"
        f"cd /home/azureuser/elastic-blast-azure\n"
        f"export PYTHONPATH=src:$PYTHONPATH\n"
        f"source venv/bin/activate\n"
        f"python bin/elastic-blast prepare --cfg {cfg_path} 2>&1\n"
        f"RC=$?\n"
        f"echo EXIT_CODE=$RC\n"
        f"echo EXIT_CODE=$RC > {marker}\n"
        f"exit $RC\n"
        f"RUNNER\n"
        f"chmod +x /tmp/elb-{job_id}.runner.sh\n"
        f"chown azureuser:azureuser /tmp/elb-{job_id}.runner.sh\n"
        f"# Detach: setsid + nohup + close fds. Output goes to {log_path}.\n"
        f"sudo -u azureuser bash -c 'rm -f {marker}; setsid nohup /tmp/elb-{job_id}.runner.sh </dev/null > {log_path} 2>&1 & echo START_PID=$!'\n"
        f"sleep 1\n"
        f"echo LAUNCHED job={job_id} marker={marker} log={log_path}\n"
    )
    ssh_pw = _get_vm_ssh_password(cred, payload)
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        _terminal_rg(payload),
        _terminal_vm(payload),
        combined_script,
        ssh_password=ssh_pw,
    )
    sanitised = sanitise(output)[:1000]
    LOGGER.info("elastic-blast prepare launched: %s", sanitised[:500])
    return {
        "started": True,
        "output": sanitised,
        "marker_path": marker,
        "log_path": log_path,
        "pid_path": pid_path,
    }


def activity_check_elastic_blast_prepare(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: read-only check whether the background prepare has finished.

    Polled by the orchestrator. Returns one of:
      {"status": "running"}                      — pid alive, no marker yet
      {"status": "succeeded", "output": "..."}    — marker says EXIT_CODE=0
      {"status": "failed", "output": "...",
       "exit_code": N}                           — marker says EXIT_CODE!=0
      {"status": "lost"}                          — pid dead and no marker
                                                    (worker crashed before
                                                    writing exit code)
    """
    cred = credential_for_caller(payload.get("user_assertion"))
    job_id = _validate_job_id(payload["job_id"])

    marker = f"/tmp/elb-{job_id}.done"
    log_path = f"/tmp/elb-{job_id}.log"
    pid_path = f"/tmp/elb-{job_id}.pid"

    # Cheap probe: check marker first, fall back to pid liveness, then read
    # last 80 lines of the log for the orchestrator's UI surface.
    probe = (
        f"#!/bin/bash\n"
        f"if [ -f {marker} ]; then\n"
        f"  echo MARKER\n"
        f"  cat {marker}\n"
        f"  echo ---LOG---\n"
        f"  tail -100 {log_path} 2>/dev/null || true\n"
        f"  exit 0\n"
        f"fi\n"
        f"if [ -f {pid_path} ] && kill -0 \"$(cat {pid_path})\" 2>/dev/null; then\n"
        f"  echo RUNNING pid=$(cat {pid_path})\n"
        f"  echo ---LOG---\n"
        f"  tail -40 {log_path} 2>/dev/null || true\n"
        f"  exit 0\n"
        f"fi\n"
        f"echo LOST\n"
        f"echo ---LOG---\n"
        f"tail -100 {log_path} 2>/dev/null || true\n"
    )
    ssh_pw = _get_vm_ssh_password(cred, payload)
    output = compute_svc.run_shell(
        cred,
        payload["subscription_id"],
        _terminal_rg(payload),
        _terminal_vm(payload),
        probe,
        ssh_password=ssh_pw,
    )
    head = output.splitlines()[0].strip() if output else ""
    sanitised = sanitise(output)[:4000]
    if head == "MARKER":
        # Look for EXIT_CODE=N in the marker section
        exit_code = _parse_exit_code(output)
        # Also detect ERROR lines in the log section
        log_section = output.split("---LOG---", 1)[1] if "---LOG---" in output else ""
        has_error = any(
            line.strip().startswith("ERROR:") for line in log_section.split("\n")
            if line.strip().startswith("ERROR:")
        )
        if exit_code == 0 and not has_error:
            return {"status": "succeeded", "output": sanitised, "exit_code": 0}
        return {"status": "failed", "output": sanitised, "exit_code": exit_code}
    if head.startswith("RUNNING"):
        return {"status": "running", "output": sanitised}
    return {"status": "lost", "output": sanitised}


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
        _terminal_rg(payload),
        _terminal_vm(payload),
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

    # Export script: use managed identity, list results, capture logs.
    export_script = (
        f"#!/bin/bash\n"
        f"sudo -u azureuser bash -c '\n"
        f"export HOME=/home/azureuser\n"
        f"export AZCOPY_AUTO_LOGIN_TYPE=MSI\n"
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
        f"# 0. Check az login — use managed identity if expired\n"
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
        _terminal_rg(payload),
        _terminal_vm(payload),
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


def activity_k8s_warmup_db(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: applies a K8s DaemonSet to download DB from blob to every node's local SSD.

    Inspired by elastic-blast-azure benchmark v3 PreloadedStrategy.
    Uses K8s API directly (no VM SSH required).
    DaemonSet automatically runs on all nodes — no node-ordinal labels needed.

    Input: subscription_id, resource_group, cluster_name, db_url, db_name,
           acr_name, acr_resource_group, num_nodes
    Returns: {"applied": True, "daemonset_name": str, "db_name": str}
    """
    cred = credential_for_caller(payload.get("user_assertion"))
    sub = payload["subscription_id"]
    rg = payload["resource_group"]
    cluster = payload["cluster_name"]
    db_url = payload["db_url"]          # e.g. https://stgelbdemo.blob.core.windows.net/blast-db/16S_ribosomal_RNA
    db_name = payload["db_name"]        # e.g. 16S_ribosomal_RNA
    acr_name = payload.get("acr_name", "")
    elb_image = f"{acr_name}.azurecr.io/ncbi/elb:1.4.0" if acr_name else "mcr.microsoft.com/azure-cli:latest"

    ds_name = f"warmup-{re.sub(r'[^a-z0-9-]', '-', db_name.lower())}"[:63]
    safe_db_label = re.sub(r"[^a-zA-Z0-9._-]", "-", db_name)[:63]

    session, server = monitoring_svc._get_k8s_session(cred, sub, rg, cluster)
    try:
        # Delete previous DaemonSet for this DB if exists
        session.delete(
            f"{server}/apis/apps/v1/namespaces/default/daemonsets/{ds_name}",
            params={"propagationPolicy": "Background"},
            timeout=10,
        )
        # Also clean up old Job-based warmups
        resp = session.get(
            f"{server}/apis/batch/v1/namespaces/default/jobs",
            params={"labelSelector": f"app=db-warmup,db={safe_db_label}"},
            timeout=10,
        )
        if resp.status_code == 200:
            for old_job in resp.json().get("items", []):
                old_name = old_job["metadata"]["name"]
                session.delete(
                    f"{server}/apis/batch/v1/namespaces/default/jobs/{old_name}",
                    params={"propagationPolicy": "Background"},
                    timeout=10,
                )

        # Create DaemonSet — runs initContainer on every node to download DB
        ds_body = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {
                "name": ds_name,
                "labels": {"app": "db-warmup", "db": safe_db_label},
            },
            "spec": {
                "selector": {"matchLabels": {"app": "db-warmup", "db": safe_db_label}},
                "template": {
                    "metadata": {"labels": {"app": "db-warmup", "db": safe_db_label}},
                    "spec": {
                        "initContainers": [{
                            "name": "download-db",
                            "image": elb_image,
                            "command": ["bash", "-c"],
                            "args": [
                                f'set -e; '
                                f'DB_DIR="/workspace/blast"; '
                                f'mkdir -p "$DB_DIR"; '
                                f'if compgen -G "$DB_DIR/{db_name}*.nsq" >/dev/null 2>&1 || '
                                f'   compgen -G "$DB_DIR/{db_name}*.psq" >/dev/null 2>&1 || '
                                f'   compgen -G "$DB_DIR/{db_name}*.nal" >/dev/null 2>&1 || '
                                f'   compgen -G "$DB_DIR/{db_name}*.pal" >/dev/null 2>&1; then '
                                f'  echo "DB already present at $DB_DIR ($(ls $DB_DIR/{db_name}* 2>/dev/null | wc -l) files)"; exit 0; '
                                f'fi; '
                                f'echo "Downloading {db_name} from blob to $DB_DIR ..."; '
                                f'export AZCOPY_AUTO_LOGIN_TYPE=MSI; '
                                f'TMP_DIR=$(mktemp -d); '
                                # Retry azcopy up to 6 times with 30s sleep to ride out
                                # AKS kubelet RBAC propagation (Storage Blob Data
                                # Contributor) which can take 60-180s after cluster
                                # creation. Without this the DaemonSet pod hits
                                # CrashLoopBackOff (5min backoff) and the orchestrator
                                # times out before the role is effective.
                                f'AZCOPY_OK=0; '
                                f'for attempt in 1 2 3 4 5 6; do '
                                f'  echo "azcopy attempt $attempt/6 ..."; '
                                f'  if azcopy cp "{db_url}/*" "$TMP_DIR/" '
                                f'    --recursive --log-level=WARNING --output-level=essential; then '
                                f'    AZCOPY_OK=1; break; '
                                f'  fi; '
                                f'  echo "azcopy failed (attempt $attempt) — sleeping 30s before retry"; '
                                f'  sleep 30; '
                                f'done; '
                                f'if [ "$AZCOPY_OK" -ne 1 ]; then '
                                f'  echo "ERROR: azcopy failed after 6 attempts (kubelet RBAC propagation)"; '
                                f'  exit 1; '
                                f'fi; '
                                f'find "$TMP_DIR" -type f -name "{db_name}*" -exec mv {{}} "$DB_DIR/" \\; ; '
                                f'rm -rf "$TMP_DIR"; '
                                f'FILE_COUNT=$(ls "$DB_DIR/{db_name}"* 2>/dev/null | wc -l); '
                                f'echo "Download complete: $FILE_COUNT files"; '
                                f'if [ "$FILE_COUNT" -eq 0 ]; then echo "ERROR: no files downloaded"; exit 1; fi; '
                                f'echo "Generating DB metadata..."; '
                                f'cd "$DB_DIR" && blastdbcmd -db {db_name} -info -json > {db_name}.njs 2>/dev/null || true; '
                            ],
                            "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
                            "resources": {
                                "requests": {"cpu": "1", "memory": "1Gi"},
                                "limits": {"memory": "4Gi"},
                            },
                        }],
                        "containers": [{
                            "name": "pause",
                            "image": "registry.k8s.io/pause:3.9",
                        }],
                        "volumes": [{
                            "name": "workspace",
                            "hostPath": {"path": "/workspace", "type": "DirectoryOrCreate"},
                        }],
                    },
                },
            },
        }
        resp = session.post(
            f"{server}/apis/apps/v1/namespaces/default/daemonsets",
            json=ds_body,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            LOGGER.info("Created warmup DaemonSet: %s", ds_name)
        elif resp.status_code == 409:
            LOGGER.info("Warmup DaemonSet already exists: %s", ds_name)
        else:
            LOGGER.error("Failed to create DaemonSet %s: %d %s", ds_name, resp.status_code, resp.text[:300])
            return {"applied": False, "error": resp.text[:500], "db_name": db_name}

        return {"applied": True, "daemonset_name": ds_name, "db_name": db_name}
    finally:
        session.close()


def activity_k8s_check_warmup_db(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: none (read-only). Polls K8s warmup DaemonSet pod status.

    Returns {"status": "running"|"succeeded"|"failed", "ready": N, "total": N,
             "init_failed": int, "restart_max": int, "logs": str}

    Failure semantics: only declared "failed" once a pod has accumulated
    >= 5 init container restarts (k8s backoff peaks at 5 min, so this gives
    ~10-15 min for transient RBAC propagation to clear). On declared failure
    the most recent init container logs are surfaced for diagnostics.
    """
    cred = credential_for_caller(payload.get("user_assertion"))
    sub = payload["subscription_id"]
    rg = payload["resource_group"]
    cluster = payload["cluster_name"]
    db_name = payload["db_name"]
    safe_db_label = re.sub(r"[^a-zA-Z0-9._-]", "-", db_name)[:63]
    ds_name = f"warmup-{re.sub(r'[^a-z0-9-]', '-', db_name.lower())}"[:63]

    session, server = monitoring_svc._get_k8s_session(cred, sub, rg, cluster)
    try:
        # Check DaemonSet status
        resp = session.get(
            f"{server}/apis/apps/v1/namespaces/default/daemonsets/{ds_name}",
            timeout=10,
        )
        if resp.status_code != 200:
            return {"status": "unknown", "error": f"DaemonSet not found: {resp.status_code}"}

        ds = resp.json()
        status = ds.get("status", {})
        desired = status.get("desiredNumberScheduled", 0)
        ready = status.get("numberReady", 0)

        if desired == 0:
            return {"status": "running", "ready": 0, "total": 0}

        # Inspect individual pod statuses
        resp2 = session.get(
            f"{server}/api/v1/namespaces/default/pods",
            params={"labelSelector": f"app=db-warmup,db={safe_db_label}"},
            timeout=10,
        )
        init_done = 0
        init_failed_persistent = 0  # only counts pods stuck (>= 5 restarts)
        restart_max = 0
        failed_pod_name = ""
        if resp2.status_code == 200:
            for pod in resp2.json().get("items", []):
                init_statuses = pod.get("status", {}).get("initContainerStatuses", [])
                for cs in init_statuses:
                    rc = int(cs.get("restartCount", 0))
                    restart_max = max(restart_max, rc)
                    term = cs.get("state", {}).get("terminated", {})
                    last = cs.get("lastState", {}).get("terminated", {})
                    if term.get("exitCode") == 0:
                        init_done += 1
                    elif rc >= 5 and (term.get("exitCode", 0) != 0 or last.get("exitCode", 0) != 0):
                        # Stuck after 5 retries → declare persistent failure
                        init_failed_persistent += 1
                        if not failed_pod_name:
                            failed_pod_name = pod.get("metadata", {}).get("name", "")

        if ready == desired and desired > 0:
            return {"status": "succeeded", "ready": ready, "total": desired,
                    "restart_max": restart_max}

        if init_failed_persistent > 0:
            # Surface the init container logs for the first persistently-failed pod
            logs = ""
            if failed_pod_name:
                try:
                    log_resp = session.get(
                        f"{server}/api/v1/namespaces/default/pods/{failed_pod_name}/log",
                        params={"container": "download-db", "tailLines": "60", "previous": "true"},
                        timeout=10,
                    )
                    if log_resp.status_code == 200:
                        logs = log_resp.text[-3000:]
                    else:
                        # Fall back to current container logs
                        log_resp = session.get(
                            f"{server}/api/v1/namespaces/default/pods/{failed_pod_name}/log",
                            params={"container": "download-db", "tailLines": "60"},
                            timeout=10,
                        )
                        if log_resp.status_code == 200:
                            logs = log_resp.text[-3000:]
                except Exception:
                    pass
            return {
                "status": "failed",
                "ready": init_done,
                "total": desired,
                "init_failed": init_failed_persistent,
                "restart_max": restart_max,
                "failed_pod": failed_pod_name,
                "logs": sanitise(logs) if logs else "",
            }

        return {"status": "running", "ready": init_done, "total": desired,
                "restart_max": restart_max}
    finally:
        session.close()


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

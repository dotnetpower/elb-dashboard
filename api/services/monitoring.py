"""Read-only monitoring helpers for AKS, Storage, ACR, and the Remote Terminal."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from azure.core.credentials import TokenCredential

from services.azure_clients import (
    acr_client,
    aks_client,
    compute_client,
    storage_client,
)
from services.image_tags import IMAGE_TAGS

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AKS
# ---------------------------------------------------------------------------
def list_aks_clusters(
    credential: TokenCredential, subscription_id: str, resource_group: str
) -> list[dict[str, Any]]:
    client = aks_client(credential, subscription_id)
    out: list[dict[str, Any]] = []
    for cluster in client.managed_clusters.list_by_resource_group(resource_group):
        pools = cluster.agent_pool_profiles or []
        agent_pool = pools[0] if pools else None
        pool_details = []
        for p in pools:
            pool_details.append({
                "name": p.name,
                "vm_size": p.vm_size,
                "count": p.count,
                "min_count": p.min_count,
                "max_count": p.max_count,
                "os_type": p.os_type,
                "mode": p.mode,
                "power_state": p.power_state.code if p.power_state else None,
                "enable_auto_scaling": p.enable_auto_scaling,
            })
        out.append(
            {
                "name": cluster.name,
                "resource_group": resource_group,
                "region": cluster.location,
                "k8s_version": cluster.kubernetes_version,
                "provisioning_state": cluster.provisioning_state,
                "power_state": cluster.power_state.code if cluster.power_state else None,
                "node_count": agent_pool.count if agent_pool else None,
                "node_sku": agent_pool.vm_size if agent_pool else None,
                "kubelet_object_id": (
                    cluster.identity_profile.get("kubeletidentity").object_id
                    if cluster.identity_profile and "kubeletidentity" in cluster.identity_profile
                    else None
                ),
                "agent_pools": pool_details,
                "network_plugin": cluster.network_profile.network_plugin if cluster.network_profile else None,
                "fqdn": cluster.fqdn,
            }
        )
    return out


def run_aks_command(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    command: str,
) -> dict[str, Any]:
    """Run a kubectl command on the AKS cluster via Azure Run Command API.

    Only allows read-only commands (get, top, describe, logs).
    Returns the command output as text.
    SLOW (~30s) — use k8s_* functions below for direct API access.
    """
    # Allowlist: only read-only kubectl commands, reject shell metacharacters
    _SHELL_META = re.compile(r"[;&|`$(){}\\!\n\r<>~\[\]?*]")
    if _SHELL_META.search(command):
        raise ValueError("Command contains forbidden shell metacharacters")
    allowed_prefixes = ("kubectl get ", "kubectl top ", "kubectl describe ", "kubectl version", "kubectl logs ")
    if not any(command.startswith(p) for p in allowed_prefixes):
        raise ValueError("Command not allowed: only read-only kubectl commands are accepted")

    client = aks_client(credential, subscription_id)
    from azure.mgmt.containerservice.models import RunCommandRequest
    run_req = RunCommandRequest(command=command)
    poller = client.managed_clusters.begin_run_command(
        resource_group, cluster_name, run_req
    )
    result = poller.result(timeout=60)
    return {
        "exit_code": result.exit_code,
        "output": result.logs or "",
        "started_at": result.started_at.isoformat() if result.started_at else None,
        "finished_at": result.finished_at.isoformat() if result.finished_at else None,
    }


# ---------------------------------------------------------------------------
# Direct Kubernetes API access (fast — uses kubeconfig credentials)
# ---------------------------------------------------------------------------
import base64
import tempfile
import ssl
import yaml  # type: ignore[import-untyped]

# AKS AAD server application ID (fixed for all AKS clusters)
_AKS_SERVER_APP_ID = "6dae42f8-4368-4678-94ff-3960e28e3630"


def _get_k8s_session(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> tuple[Any, str]:
    """Get a requests.Session configured for direct K8s API access.

    Returns (session, server_url).
    Uses AKS user credentials (kubeconfig) — supports both client-certificate
    and AAD token-based auth.
    """
    import requests as _req

    client = aks_client(credential, subscription_id)
    creds = client.managed_clusters.list_cluster_user_credentials(
        resource_group, cluster_name,
    )
    kubeconfig_bytes = creds.kubeconfigs[0].value
    kc = yaml.safe_load(bytes(kubeconfig_bytes))

    cluster_info = kc["clusters"][0]["cluster"]
    server = cluster_info["server"]
    ca_data = cluster_info.get("certificate-authority-data", "")
    user_info = kc["users"][0]["user"]

    session = _req.Session()

    # Track temp files for cleanup
    _temp_files: list[str] = []

    # Write CA cert (restricted permissions)
    ca_file = None
    if ca_data:
        ca_bytes = base64.b64decode(ca_data)
        ca_file = tempfile.NamedTemporaryFile(suffix=".crt", delete=False)
        ca_file.write(ca_bytes)
        ca_file.flush()
        os.chmod(ca_file.name, 0o600)
        _temp_files.append(ca_file.name)
        session.verify = ca_file.name
    else:
        session.verify = True

    # Auth: client certificate or AAD token
    client_cert = user_info.get("client-certificate-data")
    client_key = user_info.get("client-key-data")
    if client_cert and client_key:
        cert_file = tempfile.NamedTemporaryFile(suffix=".crt", delete=False)
        cert_file.write(base64.b64decode(client_cert))
        cert_file.flush()
        os.chmod(cert_file.name, 0o600)
        _temp_files.append(cert_file.name)
        key_file = tempfile.NamedTemporaryFile(suffix=".key", delete=False)
        key_file.write(base64.b64decode(client_key))
        key_file.flush()
        os.chmod(key_file.name, 0o600)
        _temp_files.append(key_file.name)
        session.cert = (cert_file.name, key_file.name)
    else:
        # Fallback: AAD token
        token = credential.get_token(f"{_AKS_SERVER_APP_ID}/.default")
        session.headers["Authorization"] = f"Bearer {token.token}"

    # Register cleanup of temp files on session close
    _orig_close = session.close
    def _cleanup_close() -> None:
        try:
            _orig_close()
        finally:
            for f in _temp_files:
                try:
                    os.unlink(f)
                except OSError:
                    pass
    session.close = _cleanup_close  # type: ignore[assignment]

    return session, server


def k8s_get_nodes(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> list[dict[str, Any]]:
    """Get nodes via direct K8s API."""
    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        resp = session.get(f"{server}/api/v1/nodes", timeout=10)
        resp.raise_for_status()
        nodes = []
        for item in resp.json().get("items", []):
            meta = item.get("metadata", {})
            status = item.get("status", {})
            conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
            info = status.get("nodeInfo", {})
            addrs = {a["type"]: a["address"] for a in status.get("addresses", [])}
            nodes.append({
                "name": meta.get("name", ""),
                "status": "Ready" if conditions.get("Ready") == "True" else "NotReady",
                "roles": ",".join(k.replace("node-role.kubernetes.io/", "") for k in meta.get("labels", {}) if k.startswith("node-role.kubernetes.io/")) or "<none>",
                "age": meta.get("creationTimestamp", ""),
                "version": info.get("kubeletVersion", ""),
                "internal_ip": addrs.get("InternalIP", ""),
                "os_image": info.get("osImage", ""),
                "kernel": info.get("kernelVersion", ""),
                "runtime": info.get("containerRuntimeVersion", ""),
            })
        return nodes
    finally:
        session.close()


def k8s_get_pods(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    """Get pods via direct K8s API."""
    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        url = f"{server}/api/v1/pods" if not namespace else f"{server}/api/v1/namespaces/{namespace}/pods"
        resp = session.get(url, params={"fieldSelector": "status.phase!=Succeeded"}, timeout=10)
        resp.raise_for_status()
        pods = []
        for item in resp.json().get("items", []):
            meta = item.get("metadata", {})
            spec = item.get("spec", {})
            status = item.get("status", {})
            containers = status.get("containerStatuses", [])
            ready = sum(1 for c in containers if c.get("ready"))
            total = len(spec.get("containers", []))
            restarts = sum(c.get("restartCount", 0) for c in containers)
            pods.append({
                "namespace": meta.get("namespace", ""),
                "name": meta.get("name", ""),
                "ready": f"{ready}/{total}",
                "status": status.get("phase", "Unknown"),
                "restarts": restarts,
                "age": meta.get("creationTimestamp", ""),
                "node": spec.get("nodeName", ""),
            })
        return pods
    finally:
        session.close()


def k8s_top_nodes(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> list[dict[str, Any]]:
    """Get node resource usage via K8s metrics API."""
    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        # Get capacity from nodes API
        nodes_resp = session.get(f"{server}/api/v1/nodes", timeout=10)
        nodes_resp.raise_for_status()
        capacity: dict[str, dict[str, int]] = {}
        for item in nodes_resp.json().get("items", []):
            name = item["metadata"]["name"]
            cap = item.get("status", {}).get("capacity", {})
            cpu_str = cap.get("cpu", "0")
            mem_str = cap.get("memory", "0")
            cpu_m = int(cpu_str) * 1000 if not cpu_str.endswith("m") else int(cpu_str[:-1])
            mem_ki = int(mem_str.replace("Ki", "")) if mem_str.endswith("Ki") else int(mem_str) // 1024
            capacity[name] = {"cpu_m": cpu_m, "mem_ki": mem_ki}

        # Get metrics
        metrics_resp = session.get(f"{server}/apis/metrics.k8s.io/v1beta1/nodes", timeout=10)
        metrics_resp.raise_for_status()
        result = []
        for item in metrics_resp.json().get("items", []):
            name = item["metadata"]["name"]
            usage = item.get("usage", {})
            cpu_raw = usage.get("cpu", "0")
            mem_raw = usage.get("memory", "0")

            # Parse CPU (may be "123456789n" nanocores or "150m" millicores)
            if cpu_raw.endswith("n"):
                cpu_m = int(cpu_raw[:-1]) // 1_000_000
            elif cpu_raw.endswith("m"):
                cpu_m = int(cpu_raw[:-1])
            else:
                cpu_m = int(cpu_raw) * 1000

            # Parse memory (may be "1234Ki" or bytes)
            if mem_raw.endswith("Ki"):
                mem_ki = int(mem_raw[:-2])
            elif mem_raw.endswith("Mi"):
                mem_ki = int(mem_raw[:-2]) * 1024
            elif mem_raw.endswith("Gi"):
                mem_ki = int(mem_raw[:-2]) * 1024 * 1024
            else:
                mem_ki = int(mem_raw) // 1024

            cap = capacity.get(name, {"cpu_m": 1, "mem_ki": 1})
            cpu_pct = round(cpu_m / cap["cpu_m"] * 100) if cap["cpu_m"] > 0 else 0
            mem_mi = mem_ki // 1024
            mem_total_mi = cap["mem_ki"] // 1024
            mem_pct = round(mem_ki / cap["mem_ki"] * 100) if cap["mem_ki"] > 0 else 0

            result.append({
                "name": name,
                "cpu": f"{cpu_m}m",
                "cpu_pct": cpu_pct,
                "memory": f"{mem_mi}Mi",
                "memory_pct": mem_pct,
                "memory_total": f"{mem_total_mi}Mi",
            })
        return result
    finally:
        session.close()


def k8s_pod_logs(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    pod_name: str,
    tail_lines: int = 200,
) -> str:
    """Get pod logs via direct K8s API."""
    # Validate namespace/pod_name — K8s names: lowercase alphanumeric + hyphens, start/end alphanumeric
    _SAFE_K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    if not _SAFE_K8S_NAME.match(namespace) or not _SAFE_K8S_NAME.match(pod_name):
        raise ValueError("Invalid namespace or pod name")
    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        resp = session.get(
            f"{server}/api/v1/namespaces/{namespace}/pods/{pod_name}/log",
            params={"tailLines": tail_lines},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.text
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def get_storage_summary(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
) -> dict[str, Any]:
    client = storage_client(credential, subscription_id)
    account = client.storage_accounts.get_properties(resource_group, account_name)
    containers = list(client.blob_containers.list(resource_group, account_name))
    return {
        "name": account.name,
        "region": account.location,
        "sku": account.sku.name if account.sku else None,
        "kind": account.kind,
        "public_network_access": account.public_network_access,
        "is_hns_enabled": account.is_hns_enabled,
        "containers": [
            {
                "name": c.name,
                "public_access": c.public_access,
                "last_modified_time": c.last_modified_time,
            }
            for c in containers
        ],
    }


def set_storage_public_access(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    enabled: bool,
) -> dict[str, Any]:
    """Toggle storage account public network access.

    If the storage account has VNet/service-endpoint rules configured
    (networkRuleSet.virtualNetworkRules is non-empty), toggling
    publicNetworkAccess to Disabled would break VNet-based access.
    In that case, keep publicNetworkAccess=Enabled and only toggle
    the defaultAction between Allow/Deny.
    """
    client = storage_client(credential, subscription_id)
    LOGGER.info("set_storage_public_access account=%s enabled=%s", account_name, enabled)

    # Check if service endpoint / VNet rules are configured
    acct = client.storage_accounts.get_properties(resource_group, account_name)
    vnet_rules = getattr(acct.network_rule_set, "virtual_network_rules", None) or []

    if vnet_rules:
        # Service Endpoint is configured — keep publicNetworkAccess=Enabled,
        # toggle defaultAction only (Allow = open to allowed IPs/subnets,
        # Deny = restrict to VNet rules + IP rules only)
        LOGGER.info("VNet rules detected (%d) — toggling defaultAction only", len(vnet_rules))
        from azure.mgmt.storage.models import NetworkRuleSet, DefaultAction
        new_action = DefaultAction.ALLOW if enabled else DefaultAction.DENY
        update = client.storage_accounts.update(
            resource_group,
            account_name,
            {"public_network_access": "Enabled", "network_rule_set": {"default_action": new_action.value}},
        )
        return {"public_network_access": update.public_network_access, "default_action": new_action.value}
    else:
        # No VNet rules — use the original publicNetworkAccess toggle
        update = client.storage_accounts.update(
            resource_group,
            account_name,
            {"public_network_access": "Enabled" if enabled else "Disabled"},
        )
        return {"public_network_access": update.public_network_access}


# ---------------------------------------------------------------------------
# ACR
# ---------------------------------------------------------------------------
def list_acr_repositories(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
) -> dict[str, Any]:
    """Returns registry metadata with actual vs expected image tag status."""
    mgmt = acr_client(credential, subscription_id)
    registry = mgmt.registries.get(resource_group, registry_name)
    login_server = registry.login_server or f"{registry_name}.azurecr.io"

    # Check which expected images actually exist via ACR mgmt API
    actual_tags: dict[str, list[str]] = {}
    building_images: list[str] = []
    try:
        from azure.mgmt.containerregistry import ContainerRegistryManagementClient
        acr_preview = ContainerRegistryManagementClient(
            credential, subscription_id, api_version="2019-06-01-preview"
        )
        for run in acr_preview.runs.list(resource_group, registry_name):
            if run.status == "Succeeded" and run.output_images:
                for img in run.output_images:
                    repo = img.repository or ""
                    tag = img.tag or ""
                    if repo and tag:
                        actual_tags.setdefault(repo, [])
                        if tag not in actual_tags[repo]:
                            actual_tags[repo].append(tag)
            elif run.status in ("Queued", "Started", "Running") and run.output_images:
                for img in run.output_images:
                    full = f"{img.repository or ''}:{img.tag or ''}"
                    if full not in building_images:
                        building_images.append(full)
    except Exception as exc:
        LOGGER.warning("ACR runs query failed (non-fatal): %s", type(exc).__name__)

    return {
        "name": registry.name,
        "login_server": login_server,
        "sku": registry.sku.name if registry.sku else None,
        "expected_image_tags": IMAGE_TAGS,
        "actual_tags": actual_tags,
        "building_images": building_images,
    }


# ---------------------------------------------------------------------------
# Remote Terminal VM
# ---------------------------------------------------------------------------
def get_vm_status(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
) -> dict[str, Any]:
    client = compute_client(credential, subscription_id)
    vm = client.virtual_machines.get(resource_group, vm_name, expand="instanceView")
    statuses = vm.instance_view.statuses if vm.instance_view else []
    power_state = next(
        (s.display_status for s in statuses if s.code and s.code.startswith("PowerState/")),
        None,
    )
    return {
        "name": vm.name,
        "region": vm.location,
        "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else None,
        "provisioning_state": vm.provisioning_state,
        "power_state": power_state,
    }


# ---------------------------------------------------------------------------
# Resource creation (idempotent)
# ---------------------------------------------------------------------------
def ensure_storage_account(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    region: str,
) -> None:
    """Create a Standard_LRS HNS-enabled storage account. Idempotent."""
    client = storage_client(credential, subscription_id)
    LOGGER.info("ensure_storage_account account=%s rg=%s", account_name, resource_group)
    poller = client.storage_accounts.begin_create(
        resource_group,
        account_name,
        {
            "location": region,
            "sku": {"name": "Standard_LRS"},
            "kind": "StorageV2",
            "properties": {
                "is_hns_enabled": True,
                "public_network_access": "Disabled",
                "minimum_tls_version": "TLS1_2",
            },
            "tags": {"managed-by": "elastic-blast-azure-functionapp"},
        },
    )
    poller.result()

    # Create default containers
    blob_client = client.blob_containers
    for container_name in ("blast-db", "queries", "results"):
        try:
            blob_client.create(resource_group, account_name, container_name, {})
        except Exception:
            pass  # container may already exist


def ensure_acr(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
    region: str,
) -> None:
    """Create a Standard SKU ACR. Idempotent."""
    client = acr_client(credential, subscription_id)
    LOGGER.info("ensure_acr registry=%s rg=%s", registry_name, resource_group)
    poller = client.registries.begin_create(
        resource_group,
        registry_name,
        {
            "location": region,
            "sku": {"name": "Standard"},
            "admin_user_enabled": False,
            "tags": {"managed-by": "elastic-blast-azure-functionapp"},
        },
    )
    poller.result()

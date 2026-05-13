"""Azure Functions Python v2 entry point.

Registers HTTP triggers, the Durable Functions orchestrator, and activities.
All HTTP triggers are anonymous at the platform level — auth is enforced by
`auth.token.validate_bearer_token` so the SPA can use MSAL bearer tokens.

Route groups extracted to ``api/routes/*`` and registered as
``df.Blueprint`` instances. New extractions should follow the same pattern:
move handlers + their tests, then ``app.register_functions(bp)`` here.
"""

from __future__ import annotations

import logging
import uuid

import azure.durable_functions as df
import azure.functions as func

from _http_utils import (
    _error_response,
    _json_response,
)
from activities import blast as blast_activities
from activities import storage as storage_activities
from activities import terminal as terminal_activities
from auth.token import AuthError, validate_bearer_token
from entities import job_registry as _job_reg
from models.terminal import HealthResponse
from orchestrators import delete_blast as _del_blast
from orchestrators import provision_terminal as _prov_term
from orchestrators import storage_window as _stor_win
from orchestrators import submit_blast as _sub_blast
from routes import aks as _aks_routes
from routes import arm as _arm_routes
from routes import blast as _blast_routes
from routes import blast_jobs as _blast_jobs_routes
from routes import blast_tools as _blast_tools_routes
from routes import data_plane as _data_plane_routes
from routes import monitor as _monitor_routes
from routes import resources as _resources_routes
from routes import terminal as _terminal_routes
from routes.blast_tools import _parse_primer3_output
from services.blast_config import AZURE_VM_HOURLY_USD as _AZURE_VM_HOURLY_USD

LOGGER = logging.getLogger(__name__)

__all__ = ["_AZURE_VM_HOURLY_USD", "_parse_primer3_output"]

app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Register Blueprints (one per route group). Add new groups here as they are
# extracted out of this file.
app.register_functions(_monitor_routes.bp)
app.register_functions(_terminal_routes.bp)
app.register_functions(_arm_routes.bp)
app.register_functions(_resources_routes.bp)
app.register_functions(_aks_routes.bp)
app.register_functions(_data_plane_routes.bp)
app.register_functions(_blast_routes.bp)
app.register_functions(_blast_jobs_routes.bp)
app.register_functions(_blast_tools_routes.bp)


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
# Monitoring (read-only)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# BLAST HTTP routes are registered from routes.blast
# ---------------------------------------------------------------------------


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
        "tags": {
            "created-by": "elastic-blast-control-plane",
            "owner-oid": payload.get("owner_oid", ""),
        },
        "identity": {"type": "SystemAssigned"},
        "dns_prefix": payload["cluster_name"],
        "auto_upgrade_profile": {"upgrade_channel": "none"},
        "oidc_issuer_profile": {"enabled": True},
        "security_profile": {"workload_identity": {"enabled": True}},
        "agent_pool_profiles": [
            {
                "name": "nodepool1",
                "count": payload.get("node_count", 10),
                "vm_size": payload.get("node_sku", "Standard_E32s_v5"),
                "os_disk_type": "Managed",
                "mode": "System",
                "enable_auto_scaling": False,
                "type": "VirtualMachineScaleSets",
            }
        ],
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

    from azure.mgmt.authorization import AuthorizationManagementClient
    from azure.mgmt.containerservice import ContainerServiceClient

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
        scope = (
            f"/subscriptions/{sub}/resourceGroups/{acr_rg}/providers/"
            f"Microsoft.ContainerRegistry/registries/{acr_name}"
        )
        _aks_routes._assign_role(
            auth_client, scope, kubelet_oid, "7f951dda-4ed3-4680-a7ca-43fe172d538d"
        )
        assigned.append("AcrPull")
    if storage_rg and storage_account:
        scope = (
            f"/subscriptions/{sub}/resourceGroups/{storage_rg}/providers/"
            f"Microsoft.Storage/storageAccounts/{storage_account}"
        )
        _aks_routes._assign_role(
            auth_client, scope, kubelet_oid, "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
        )
        assigned.append("StorageBlobDataContributor")

    return {"kubelet_oid": kubelet_oid, "roles_assigned": assigned}


@app.activity_trigger(input_name="payload")
def setup_workload_identity_activity(payload: dict) -> dict:
    """Activity: create User-Assigned MI, Federated Credential, and assign roles.

    Enables the OpenAPI pod to authenticate as an Azure identity without
    az login. Idempotent — safe to re-run on existing clusters.
    """
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
        rg,
        mi_name,
        {"location": region, "tags": {"purpose": "elb-openapi-workload-identity"}},
    )
    mi_client_id = mi.client_id
    mi_principal_id = mi.principal_id

    # 3. Create Federated Identity Credential
    msi_client.federated_identity_credentials.create_or_update(
        rg,
        mi_name,
        fed_cred_name,
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
        scope = (
            f"/subscriptions/{sub}/resourceGroups/{storage_rg}/providers/"
            f"Microsoft.Storage/storageAccounts/{storage_account}"
        )
        _aks_routes._assign_role(
            auth_client, scope, mi_principal_id, "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
        )

    # Azure Kubernetes Service Cluster User Role on the cluster (for kubectl)
    cluster_scope = (
        f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
        f"Microsoft.ContainerService/managedClusters/{cluster_name}"
    )
    _aks_routes._assign_role(
        auth_client, cluster_scope, mi_principal_id, "4abbcc35-e782-43d8-92c5-2d3f1bd2253f"
    )

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

    Uses AKS Run Command (begin_run_command) instead of local kubectl so it
    works from any environment including Azure Functions consumption plan.
    """
    import json as _json
    import time

    from azure.mgmt.containerservice import ContainerServiceClient
    from azure.mgmt.containerservice.models import RunCommandRequest

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
    image = (
        f"{acr_name}.azurecr.io/elb-openapi:{image_tag}" if acr_name else f"elb-openapi:{image_tag}"
    )

    aks_client = ContainerServiceClient(cred, sub)

    def _run_kubectl(command: str, timeout: int = 60) -> str:
        """Execute a kubectl command on AKS via Run Command API."""
        poller = aks_client.managed_clusters.begin_run_command(
            rg, cluster_name, RunCommandRequest(command=command)
        )
        result = poller.result(timeout=timeout)
        if result.exit_code != 0:
            LOGGER.warning("AKS run-command failed (exit %d): %s", result.exit_code, (result.logs or "")[:300])
        return result.logs or ""

    # Build all manifests as a single multi-document YAML
    sa_manifest = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": k8s_sa_name,
            "namespace": "default",
            "annotations": ({"azure.workload.identity/client-id": mi_client_id} if mi_client_id else {}),
            "labels": {"azure.workload.identity/use": "true"},
        },
    }

    deploy_manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "elb-openapi", "namespace": "default"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "elb-openapi"}},
            "template": {
                "metadata": {
                    "labels": {"app": "elb-openapi", "azure.workload.identity/use": "true"},
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

    svc_manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "elb-openapi", "namespace": "default"},
        "spec": {
            "type": "LoadBalancer",
            "selector": {"app": "elb-openapi"},
            "ports": [{"port": 80, "targetPort": 8000}],
        },
    }

    role_manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
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

    binding_manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": "elb-openapi-binding"},
        "subjects": [{"kind": "ServiceAccount", "name": k8s_sa_name, "namespace": "default"}],
        "roleRef": {"kind": "ClusterRole", "name": "elb-openapi-role", "apiGroup": "rbac.authorization.k8s.io"},
    }

    # Apply all manifests via AKS Run Command (no local kubectl needed)
    all_manifests = [sa_manifest, role_manifest, binding_manifest, deploy_manifest, svc_manifest]
    combined_json = "\n---\n".join(_json.dumps(m) for m in all_manifests)
    apply_cmd = f"cat <<'MANIFEST_EOF' | kubectl apply -f -\n{combined_json}\nMANIFEST_EOF"
    apply_output = _run_kubectl(apply_cmd, timeout=120)
    LOGGER.info("OpenAPI manifests applied: %s", apply_output[:300])

    # Wait for external IP (poll via Run Command, up to 120s)
    external_ip = ""
    for _ in range(6):
        time.sleep(20)
        ip_output = _run_kubectl(
            "kubectl get svc elb-openapi -o jsonpath='{.status.loadBalancer.ingress[0].ip}'",
            timeout=30,
        )
        ip = ip_output.strip().strip("'")
        if ip and ip != "<pending>":
            external_ip = ip
            break

    return {"status": "deployed", "image": image, "external_ip": external_ip}


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
        entry["timestamp"] = (
            entry.get("timestamp")
            or __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        )
        state.append(entry)
        # Keep last 10000 entries
        if len(state) > 10000:
            state = state[-10000:]
        context.set_state(state)

    elif op == "list_events":
        context.set_result(state)


@app.route(route="audit/log", methods=["GET"])
@app.durable_client_input(client_name="client")
async def list_audit_log(req: func.HttpRequest, client) -> func.HttpResponse:
    """List audit trail events."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    limit = min(int(req.params.get("limit", "100")), 500)
    action_filter = req.params.get("action", "")

    try:
        entity_id = df.EntityId("audit_trail_entity", "global")
        resp = await client.read_entity_state(entity_id)
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
        entry["created_at"] = (
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        )
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
                s["last_run"] = (
                    __import__("datetime")
                    .datetime.now(__import__("datetime").timezone.utc)
                    .isoformat()
                )
                s["run_count"] = s.get("run_count", 0) + 1
                break
        context.set_state(state)


# BLAST schedule HTTP routes are registered from routes.blast

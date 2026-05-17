"""Deploy ``elb-openapi`` to an existing AKS cluster.

Side effects (idempotent):
  * Create / update User-Assigned Managed Identity ``id-elb-openapi``.
  * Create / update a Federated Identity Credential binding the AKS OIDC
    issuer to the ``elb-openapi-sa`` ServiceAccount.
  * Assign Contributor / Storage Blob Data Contributor / AKS Cluster User
    roles to the MI (best-effort — non-fatal on conflict).
  * Apply 5 Kubernetes manifests (ServiceAccount, Deployment, Service,
    ClusterRole, ClusterRoleBinding) via the terminal sidecar's allowlisted
    ``kubectl apply -f -``.
  * Poll the LoadBalancer for an external IP for up to ~120 s.

Translated 1:1 from
``legacy/functionapp/function_app.py::setup_workload_identity_activity``
and ``deploy_openapi_activity`` (which used the now-banned AKS Run
Command). The repo policy is:

  * Workload-identity / role wiring → Azure SDK (this file).
  * Manifest application → terminal sidecar's ``kubectl`` (terminal_exec).
  * External IP polling → direct K8s API (``k8s_get_service_ip``).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC
from typing import Any

from celery import shared_task

from api.services import get_credential
from api.services.azure_clients import aks_client
from api.services.image_tags import IMAGE_TAGS
from api.services.k8s_monitoring import k8s_get_service_ip

LOGGER = logging.getLogger(__name__)

# Workload-identity / K8s naming — must match the legacy values so existing
# clusters do not see a duplicate MI / SA pair.
MI_NAME = "id-elb-openapi"
K8S_SA_NAME = "elb-openapi-sa"
K8S_NAMESPACE = "default"
FED_CRED_NAME = "fc-elb-openapi"

# Built-in role definition IDs (well-known).
_ROLE_CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
_ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
_ROLE_AKS_CLUSTER_USER = "4abbcc35-e782-43d8-92c5-2d3f1bd2253f"


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _record_progress(self, phase: str, **extra: Any) -> None:
    """Push a Celery PROGRESS update so the SPA can render the phase.

    The status route maps any non-terminal Celery state plus the
    ``custom_status`` ``phase`` field into the orchestrator-style envelope
    the SPA was originally written against (Pending / Running / Completed
    / Failed / Terminated).
    """

    LOGGER.info("openapi_deploy phase=%s extra=%s", phase, extra)
    try:
        self.update_state(state="PROGRESS", meta={"phase": phase, **extra})
    except Exception as exc:
        # State backend is best-effort; never let a backend hiccup fail the task.
        LOGGER.debug("update_state failed for phase=%s: %s", phase, exc)


def _assign_role_idempotent(
    auth_client: Any,
    scope: str,
    principal_id: str,
    role_definition_id: str,
    label: str,
) -> bool:
    """Create a role assignment; return True on create / already-exists."""

    role_def = f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{role_definition_id}"
    name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}:{principal_id}:{role_definition_id}"))
    try:
        auth_client.role_assignments.create(
            scope,
            name,
            {
                "role_definition_id": role_def,
                "principal_id": principal_id,
                "principal_type": "ServicePrincipal",
            },
        )
        LOGGER.info(
            "RBAC role=%s principal=%s scope=%s assigned",
            label,
            principal_id[:8],
            scope.split("/")[-1],
        )
        return True
    except Exception as exc:
        msg = str(exc)
        if "RoleAssignmentExists" in msg or "Conflict" in msg:
            LOGGER.info("RBAC role=%s already assigned", label)
            return True
        LOGGER.warning("RBAC role=%s failed: %s", label, msg[:200])
        return False


def _setup_workload_identity(
    cred: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    region: str,
    storage_account: str,
    storage_resource_group: str,
) -> dict[str, Any]:
    """Create MI + Federated Credential + role assignments. Idempotent."""

    from azure.mgmt.authorization import AuthorizationManagementClient
    from azure.mgmt.msi import ManagedServiceIdentityClient

    # 1. OIDC issuer URL from the cluster (must already be enabled).
    aks = aks_client(cred, subscription_id)
    cluster = aks.managed_clusters.get(resource_group, cluster_name)
    oidc_url = (cluster.oidc_issuer_profile.issuer_url if cluster.oidc_issuer_profile else "") or ""
    if not oidc_url:
        raise RuntimeError(
            f"AKS cluster {cluster_name!r} does not expose an OIDC issuer "
            "URL. Re-provision the cluster with oidc_issuer_profile.enabled "
            "and security_profile.workload_identity.enabled set to True, "
            "then retry the OpenAPI deployment."
        )

    # 2. User-Assigned Managed Identity (idempotent create_or_update).
    msi = ManagedServiceIdentityClient(cred, subscription_id)
    mi = msi.user_assigned_identities.create_or_update(
        resource_group,
        MI_NAME,
        {
            "location": region,
            "tags": {
                "purpose": "elb-openapi-workload-identity",
                "managedBy": "elb-dashboard",
            },
        },
    )

    # 3. Federated Identity Credential — AKS OIDC ↔ K8s ServiceAccount.
    msi.federated_identity_credentials.create_or_update(
        resource_group,
        MI_NAME,
        FED_CRED_NAME,
        {
            "issuer": oidc_url,
            "subject": f"system:serviceaccount:{K8S_NAMESPACE}:{K8S_SA_NAME}",
            "audiences": ["api://AzureADTokenExchange"],
        },
    )

    # 4. Role assignments (best-effort — never fatal).
    auth = AuthorizationManagementClient(cred, subscription_id)
    roles_assigned: list[str] = []
    roles_failed: list[str] = []

    def _try(scope: str, role_id: str, label: str) -> None:
        if _assign_role_idempotent(auth, scope, mi.principal_id, role_id, label):
            roles_assigned.append(label)
        else:
            roles_failed.append(label)

    _try(
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}",
        _ROLE_CONTRIBUTOR,
        "Contributor",
    )
    if storage_account:
        storage_rg = storage_resource_group or resource_group
        _try(
            (
                f"/subscriptions/{subscription_id}/resourceGroups/{storage_rg}/"
                f"providers/Microsoft.Storage/storageAccounts/{storage_account}"
            ),
            _ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR,
            "StorageBlobDataContributor",
        )
    _try(
        (
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/"
            f"providers/Microsoft.ContainerService/managedClusters/{cluster_name}"
        ),
        _ROLE_AKS_CLUSTER_USER,
        "AzureKubernetesServiceClusterUserRole",
    )

    return {
        "mi_name": MI_NAME,
        "mi_client_id": mi.client_id,
        "mi_principal_id": mi.principal_id,
        "oidc_issuer": oidc_url,
        "federated_credential": FED_CRED_NAME,
        "roles_assigned": roles_assigned,
        "roles_failed": roles_failed,
    }


def _build_manifests(
    *,
    image: str,
    mi_client_id: str,
    cluster_name: str,
    resource_group: str,
    storage_account: str,
    region: str,
    tenant_id: str,
    acr_name: str,
    acr_resource_group: str,
) -> str:
    """Return the multi-document JSON payload to feed ``kubectl apply -f -``.

    kubectl happily accepts JSON documents separated by ``---`` (it parses
    them as YAML). Building JSON sidesteps the need for PyYAML.
    """

    sa_manifest = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": K8S_SA_NAME,
            "namespace": K8S_NAMESPACE,
            "annotations": (
                {"azure.workload.identity/client-id": mi_client_id} if mi_client_id else {}
            ),
            "labels": {"azure.workload.identity/use": "true"},
        },
    }

    deploy_manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "elb-openapi", "namespace": K8S_NAMESPACE},
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
                    "serviceAccountName": K8S_SA_NAME,
                    "containers": [
                        {
                            "name": "openapi",
                            "image": image,
                            "imagePullPolicy": "Always",
                            "ports": [{"containerPort": 8000}],
                            "env": [
                                {"name": "ELB_CLUSTER_NAME", "value": cluster_name},
                                {"name": "ELB_STORAGE_ACCOUNT", "value": storage_account},
                                {"name": "ELB_RESOURCE_GROUP", "value": resource_group},
                                {"name": "ELB_AZURE_REGION", "value": region},
                                {"name": "ELB_ACR_NAME", "value": acr_name},
                                {"name": "ELB_ACR_RESOURCE_GROUP", "value": acr_resource_group},
                                {
                                    "name": "PATH",
                                    "value": (
                                        "/opt/venv/bin:/usr/local/sbin:"
                                        "/usr/local/bin:/usr/sbin:/usr/bin:"
                                        "/sbin:/bin"
                                    ),
                                },
                                {"name": "AZURE_CLIENT_ID", "value": mi_client_id},
                                # Leave azcopy mode to the image/runtime.
                                # The sibling OpenAPI service now downgrades
                                # from WORKLOAD to MSI if the AKS webhook has
                                # not injected a federated token.
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                        }
                    ],
                    # Sibling repo `constants.py` (commit a2d2f0a) splits
                    # the cluster into a `systempool` (taint
                    # `CriticalAddonsOnly=true:NoSchedule`, AKS add-ons
                    # only) and a `blastpool` (taint
                    # `workload=blast:NoSchedule`, label
                    # `workload=blast`, "runs every ElasticBLAST workload
                    # pod"). Without these the deployment lands on
                    # `0/N nodes are available: untolerated taint(s)` and
                    # the LoadBalancer IP serves nothing. Pin the pod to
                    # the blast pool — it's part of the BLAST control
                    # surface, not an AKS add-on.
                    "tolerations": [
                        {
                            "key": "workload",
                            "operator": "Equal",
                            "value": "blast",
                            "effect": "NoSchedule",
                        },
                    ],
                    "nodeSelector": {"workload": "blast"},
                },
            },
        },
    }

    svc_manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "elb-openapi", "namespace": K8S_NAMESPACE},
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
            {
                "apiGroups": [""],
                "resources": ["nodes", "pods", "configmaps", "services"],
                "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
            },
            {
                "apiGroups": ["batch"],
                "resources": ["jobs"],
                "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
            },
            {
                "apiGroups": ["apps"],
                "resources": ["deployments"],
                "verbs": ["get", "list", "watch"],
            },
        ],
    }

    binding_manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": "elb-openapi-binding"},
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": K8S_SA_NAME,
                "namespace": K8S_NAMESPACE,
            }
        ],
        "roleRef": {
            "kind": "ClusterRole",
            "name": "elb-openapi-role",
            "apiGroup": "rbac.authorization.k8s.io",
        },
    }

    docs = [sa_manifest, role_manifest, binding_manifest, deploy_manifest, svc_manifest]
    return "\n---\n".join(json.dumps(d) for d in docs)


def _kubectl_apply(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    manifest: str,
) -> str:
    """Apply a multi-doc manifest via the terminal sidecar's kubectl.

    Strategy: fetch a one-shot kubeconfig with ``az aks get-credentials
    --file -`` into a temp path, then ``KUBECONFIG=… kubectl apply -f -``.
    Both invocations run in the terminal sidecar where the allowlisted
    binaries live; the api / worker images intentionally do not ship them.

    Returns kubectl's stdout. Raises on non-zero exit.
    """

    from api.services.terminal_exec import TerminalExecError
    from api.services.terminal_exec import run as exec_run

    # /tmp/exec is the shared writable scratch dir on the terminal sidecar's
    # exec server (configurable via EXEC_TMP_DIR). Random uuid prevents
    # collisions across concurrent deploys.
    kubeconfig_path = f"/tmp/exec/kubeconfig-{uuid.uuid4().hex}"  # noqa: S108
    az_argv = [
        "az",
        "aks",
        "get-credentials",
        "--subscription",
        subscription_id,
        "--resource-group",
        resource_group,
        "--name",
        cluster_name,
        "--file",
        kubeconfig_path,
        "--overwrite-existing",
        "--admin",  # bypasses AAD interactive login from inside the sidecar
        "--only-show-errors",
    ]
    try:
        az_result = exec_run(az_argv, timeout_seconds=120)
    except TerminalExecError as exc:
        raise RuntimeError(
            "Cannot reach the terminal sidecar's exec server — the "
            "OpenAPI deploy needs `az` and `kubectl` from there. Make "
            f"sure the `terminal` sidecar is running. ({exc})"
        ) from exc
    if az_result.get("exit_code", 1) != 0:
        raise RuntimeError(
            "az aks get-credentials failed: "
            f"{(az_result.get('stderr') or az_result.get('stdout') or '').strip()[:500]}"
        )

    apply_argv = ["kubectl", "--kubeconfig", kubeconfig_path, "apply", "-f", "-"]
    apply_result = exec_run(apply_argv, stdin=manifest, timeout_seconds=180)
    if apply_result.get("exit_code", 1) != 0:
        raise RuntimeError(
            "kubectl apply failed: "
            f"{(apply_result.get('stderr') or apply_result.get('stdout') or '').strip()[:500]}"
        )
    return str(apply_result.get("stdout") or "")


@shared_task(
    name="api.tasks.openapi.deploy_openapi_service",
    bind=True,
    max_retries=0,
)
def deploy_openapi_service(
    self,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    acr_name: str,
    acr_resource_group: str = "",
    storage_account: str = "",
    storage_resource_group: str = "",
    tenant_id: str = "",
    caller_oid: str = "",
) -> dict[str, Any]:
    """Re-deploy ``elb-openapi`` to an existing AKS cluster.

    Returns the orchestrator-style payload the SPA's ``OpenApiDeployPanel``
    consumes: ``{status, cluster_name, resource_group, workload_identity,
    openapi_deploy: {image, external_ip, ...}}``.
    """

    started = time.time()
    cred = get_credential()

    # Resolve region from the cluster (avoids forcing the SPA to send it).
    aks = aks_client(cred, subscription_id)
    cluster = aks.managed_clusters.get(resource_group, cluster_name)
    region = cluster.location

    image_tag = IMAGE_TAGS.get("elb-openapi", "4.9")
    effective_acr_resource_group = acr_resource_group or "rg-elbacr-01"
    image = (
        f"{acr_name}.azurecr.io/elb-openapi:{image_tag}" if acr_name else f"elb-openapi:{image_tag}"
    )

    # ----- 1. Workload Identity (MI + federated cred + roles) -------------
    _record_progress(self, "setup_workload_identity", cluster_name=cluster_name)
    try:
        wi_result = _setup_workload_identity(
            cred,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            region=region,
            storage_account=storage_account,
            storage_resource_group=storage_resource_group,
        )
    except Exception as exc:
        LOGGER.exception("workload identity setup failed")
        return {
            "status": "failed",
            "cluster_name": cluster_name,
            "resource_group": resource_group,
            "workload_identity": {"error": str(exc)[:500]},
            "openapi_deploy": {
                "error": "workload identity setup failed; "
                "OpenAPI pod would have no AZURE_CLIENT_ID."
            },
        }

    mi_client_id = wi_result.get("mi_client_id") or ""
    if not mi_client_id:
        return {
            "status": "failed",
            "cluster_name": cluster_name,
            "resource_group": resource_group,
            "workload_identity": wi_result,
            "openapi_deploy": {
                "error": "Workload Identity setup did not return an "
                "MI client id — refusing to deploy elb-openapi with an "
                "empty AZURE_CLIENT_ID."
            },
        }

    # ----- 2. kubectl apply --------------------------------------------------
    _record_progress(self, "applying_manifests", image=image, mi_client_id=mi_client_id[:8])
    manifest = _build_manifests(
        image=image,
        mi_client_id=mi_client_id,
        cluster_name=cluster_name,
        resource_group=resource_group,
        storage_account=storage_account,
        region=region,
        tenant_id=tenant_id,
        acr_name=acr_name,
        acr_resource_group=effective_acr_resource_group,
    )
    try:
        apply_output = _kubectl_apply(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            manifest=manifest,
        )
    except Exception as exc:
        LOGGER.exception("kubectl apply failed")
        return {
            "status": "failed",
            "cluster_name": cluster_name,
            "resource_group": resource_group,
            "workload_identity": wi_result,
            "openapi_deploy": {"image": image, "error": str(exc)[:500]},
        }

    # ----- 3. Wait for LoadBalancer external IP -----------------------------
    _record_progress(self, "waiting_for_external_ip", image=image)
    external_ip = ""
    for _ in range(12):  # ~120 s
        try:
            ip = k8s_get_service_ip(
                cred,
                subscription_id,
                resource_group,
                cluster_name,
                "elb-openapi",
            )
        except Exception:
            ip = None
        if ip:
            external_ip = ip
            break
        time.sleep(10)

    elapsed = int(time.time() - started)
    LOGGER.info(
        "openapi deploy done image=%s external_ip=%s elapsed=%ss",
        image,
        external_ip or "<pending>",
        elapsed,
    )
    return {
        "status": "succeeded",
        "cluster_name": cluster_name,
        "resource_group": resource_group,
        "workload_identity": wi_result,
        "openapi_deploy": {
            "status": "deployed",
            "image": image,
            "external_ip": external_ip,
            "apply_output": apply_output[:1000],
        },
        "elapsed_seconds": elapsed,
    }

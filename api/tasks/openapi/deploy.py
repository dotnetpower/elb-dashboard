"""`deploy_openapi_service` Celery task — re-deploy ``elb-openapi`` to AKS.

Responsibility: Orchestrate the full OpenAPI deploy pipeline (workload-identity setup
    → manifest build → kubectl apply → external IP wait → persist base URL) and shape
    the orchestrator-style payload the SPA's ``OpenApiDeployPanel`` consumes.
Edit boundaries: Wiring only. Each step lives in a dedicated sibling module — do not
    duplicate manifest construction, RBAC writes, or kubectl logic here.
Key entry points: `deploy_openapi_service` (Celery task
    `api.tasks.openapi.deploy_openapi_service`).
Risky contracts: Task name must remain `api.tasks.openapi.deploy_openapi_service` —
    routes and SPA references depend on it. Returned payload must keep the
    `{status, cluster_name, workload_identity, openapi_deploy:{...}}` shape.
Validation: `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence
from typing import Any

from celery import shared_task

from api.services import get_credential
from api.services.azure_clients import aks_client
from api.services.image_tags import IMAGE_TAGS
from api.services.k8s.monitoring import k8s_get_deployment_ready_replicas, k8s_get_service_ip
from api.services.k8s.observability import k8s_list_events
from api.tasks.openapi.helpers import blast_node_count, record_progress
from api.tasks.openapi.kubectl import kubectl_apply
from api.tasks.openapi.manifests import build_manifests
from api.tasks.openapi.rbac import setup_workload_identity

LOGGER = logging.getLogger(__name__)


def _openapi_ready_failure_diagnostics(
    cred: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for namespace in ("default", "kube-system"):
        try:
            events.extend(
                k8s_list_events(
                    cred,
                    subscription_id,
                    resource_group,
                    cluster_name,
                    namespace=namespace,
                    limit=50,
                )
            )
        except Exception as exc:
            LOGGER.warning("openapi deploy: event probe failed namespace=%s: %s", namespace, exc)

    diagnostic_events = _filter_ready_failure_events(events)
    likely_cause = _classify_ready_failure(diagnostic_events)
    if likely_cause == "workload_identity_webhook_unavailable":
        message = (
            "Deployment applied but no pod reached Ready because the AKS Workload Identity "
            "webhook is unavailable. Check `azure-wi-webhook-controller-manager` in "
            "kube-system; if it is Pending with `Insufficient cpu`, increase the systempool "
            "node count or use a larger systempool SKU, then re-run Deploy elb-openapi."
        )
    elif likely_cause == "image_pull_failed":
        message = (
            "Deployment applied but no pod reached Ready because the image pull failed. "
            "Verify AcrPull on the AKS kubelet identity and that the elb-openapi image tag "
            "exists in the target ACR."
        )
    elif likely_cause == "unschedulable":
        message = (
            "Deployment applied but no pod reached Ready because Kubernetes could not "
            "schedule it. Verify the blastpool label `workload=blast`, taint "
            "`workload=blast:NoSchedule`, and OpenAPI toleration/nodeSelector."
        )
    else:
        message = (
            "Deployment applied but no pod reached Ready. Check the diagnostic_events "
            "field for Kubernetes warning events, then inspect pod logs if a pod was created."
        )
    return {
        "likely_cause": likely_cause,
        "message": message,
        "events": diagnostic_events[:8],
    }


def _filter_ready_failure_events(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("type") or "") != "Warning":
            continue
        involved_name = str(event.get("involved_name") or "")
        message = str(event.get("message") or "")
        haystack = f"{involved_name}\n{message}".lower()
        if any(
            token in haystack
            for token in (
                "elb-openapi",
                "azure-wi-webhook",
                "azure-workload-identity",
                "imagepullbackoff",
                "errimagepull",
                "insufficient cpu",
                "untolerated taint",
                "failedscheduling",
                "no endpoints available",
            )
        ):
            matched.append(
                {
                    "namespace": event.get("namespace") or "",
                    "kind": event.get("involved_kind") or "",
                    "name": involved_name,
                    "reason": event.get("reason") or "",
                    "message": message[:500],
                    "last_timestamp": event.get("last_timestamp") or "",
                    "count": event.get("count") or 1,
                }
            )
    return matched


def _classify_ready_failure(events: Sequence[dict[str, Any]]) -> str:
    text = "\n".join(
        f"{event.get('name') or ''}\n{event.get('message') or ''}" for event in events
    ).lower()
    if "azure-workload-identity" in text or "azure-wi-webhook" in text:
        return "workload_identity_webhook_unavailable"
    if "imagepullbackoff" in text or "errimagepull" in text or "failed to pull image" in text:
        return "image_pull_failed"
    if "failedscheduling" in text or "untolerated taint" in text or "insufficient cpu" in text:
        return "unschedulable"
    return "unknown"


@shared_task(
    name="api.tasks.openapi.deploy_openapi_service",
    bind=True,
    max_retries=0,
)
def deploy_openapi_service(
    self: Any,
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
    num_nodes = blast_node_count(cluster)

    image_tag = IMAGE_TAGS.get("elb-openapi", "4.9")
    effective_acr_resource_group = acr_resource_group or "rg-elbacr-01"
    image = (
        f"{acr_name}.azurecr.io/elb-openapi:{image_tag}" if acr_name else f"elb-openapi:{image_tag}"
    )

    # ----- 1. Workload Identity (MI + federated cred + roles) -------------
    record_progress(self, "setup_workload_identity", cluster_name=cluster_name)
    try:
        wi_result = setup_workload_identity(
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

    api_token = os.environ.get("ELB_OPENAPI_API_TOKEN", "").strip()
    if not api_token:
        from api.services.openapi.runtime import get_openapi_api_token

        api_token = get_openapi_api_token()

    # ----- 2. kubectl apply --------------------------------------------------
    record_progress(self, "applying_manifests", image=image, mi_client_id=mi_client_id[:8])
    manifest = build_manifests(
        image=image,
        mi_client_id=mi_client_id,
        cluster_name=cluster_name,
        resource_group=resource_group,
        storage_account=storage_account,
        region=region,
        tenant_id=tenant_id,
        acr_name=acr_name,
        acr_resource_group=effective_acr_resource_group,
        num_nodes=num_nodes,
        api_token=api_token,
    )
    try:
        apply_output = kubectl_apply(
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
    record_progress(self, "waiting_for_external_ip", image=image)
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

    # ----- 4. Wait for at least one Ready pod -------------------------------
    # An external IP only means the LoadBalancer was provisioned; it does not
    # mean any pod is actually serving traffic. Without this gate the task
    # returns ``status: succeeded`` even when the pod is stuck in
    # ImagePullBackOff / CrashLoopBackOff / has no node to land on, and the
    # user only discovers the failure on the first submit (502).
    record_progress(self, "waiting_for_ready_replicas", image=image)
    ready_replicas = 0
    desired_replicas = 0
    for _ in range(24):  # ~120 s additional
        try:
            ready_replicas, desired_replicas = k8s_get_deployment_ready_replicas(
                cred,
                subscription_id,
                resource_group,
                cluster_name,
                "elb-openapi",
            )
        except Exception as exc:
            LOGGER.warning("ready-replica probe transient error: %s", exc)
            ready_replicas, desired_replicas = (0, 0)
        if ready_replicas >= 1:
            break
        time.sleep(5)

    if ready_replicas < 1:
        elapsed = int(time.time() - started)
        diagnostics = _openapi_ready_failure_diagnostics(
            cred,
            subscription_id,
            resource_group,
            cluster_name,
        )
        LOGGER.warning(
            "openapi deploy: no Ready replica after probe window image=%s "
            "external_ip=%s ready=%s desired=%s cause=%s",
            image,
            external_ip or "<pending>",
            ready_replicas,
            desired_replicas,
            diagnostics.get("likely_cause"),
        )
        return {
            "status": "failed",
            "cluster_name": cluster_name,
            "resource_group": resource_group,
            "workload_identity": wi_result,
            "openapi_deploy": {
                "status": "no_ready_replica",
                "image": image,
                "external_ip": external_ip,
                "ready_replicas": ready_replicas,
                "desired_replicas": desired_replicas,
                "error": diagnostics["message"],
                "diagnostics": diagnostics,
                "apply_output": apply_output[:1000],
            },
            "elapsed_seconds": elapsed,
        }

    elapsed = int(time.time() - started)
    if external_ip:
        from api.services.openapi.runtime import save_openapi_base_url

        save_openapi_base_url(
            f"http://{external_ip}",
            metadata={
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
                "service_name": "elb-openapi",
                "image": image,
            },
        )
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
            "ready_replicas": ready_replicas,
            "desired_replicas": desired_replicas,
            "apply_output": apply_output[:1000],
        },
        "elapsed_seconds": elapsed,
    }

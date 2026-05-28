"""Kubernetes manifest builder for the ``elb-openapi`` deploy.

Responsibility: Produce the multi-document JSON payload (ServiceAccount + RBAC +
    Deployment + PodDisruptionBudget + Service) consumed by ``kubectl apply -f -`` in
    the kubectl module.
Edit boundaries: Pure manifest construction — no Azure or Kubernetes I/O. If you need a
    new env var, label, or tolerations rule, edit it here and rely on the deploy task to
    pipe it through.
Key entry points: `build_manifests`.
Risky contracts: The blast-pool toleration and `nodeSelector workload=blast` are load-
    bearing — without them the deployment lands on tainted system nodes and serves no
    traffic. `AZURE_CLIENT_ID` is deliberately NOT set in the pod env (workload-identity
    webhook injects it from the annotated ServiceAccount). The Deployment carries
    `replicas: 2` + readiness/liveness probes on `/healthz` + a PodDisruptionBudget
    (`minAvailable: 1`) so node drains, rollouts, and crashing pods cannot all take
    the BLAST submit path down at once. Single-node blast pools still work because
    `topologySpreadConstraints.whenUnsatisfiable` is `ScheduleAnyway`.
Validation: `uv run pytest -q api/tests/test_smoke.py api/tests/test_openapi_task.py`.
"""

from __future__ import annotations

import json

from api.tasks.openapi.constants import K8S_NAMESPACE, K8S_SA_NAME, PlsConfig


def build_manifests(
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
    num_nodes: int = 10,
    api_token: str = "",
    pls: PlsConfig | None = None,
) -> str:
    """Return the multi-document JSON payload to feed ``kubectl apply -f -``.

    kubectl happily accepts JSON documents separated by ``---`` (it parses
    them as YAML). Building JSON sidesteps the need for PyYAML.

    ``api_token`` is required. Shipping a deployment without the
    ``ELB_OPENAPI_API_TOKEN`` env entry is the root cause of the
    recurring "API token not visible" SPA bug — the pre-9d4e549 guard
    silently emitted a broken manifest when the caller passed an empty
    token. Refuse to build instead so the failure is loud at the deploy
    task boundary, not buried as ``configured=false`` in the API panel
    hours later.
    """
    if not api_token or not api_token.strip():
        raise ValueError(
            "build_manifests: api_token must be a non-empty string. "
            "The elb-openapi deployment fails-closed when "
            "ELB_OPENAPI_API_TOKEN is unset, so this builder refuses to "
            "emit a manifest without it. Resolve / mint a token in the "
            "calling task (api.tasks.openapi.deploy.deploy_openapi_service) "
            "before invoking build_manifests."
        )

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

    openapi_env = [
        {"name": "ELB_CLUSTER_NAME", "value": cluster_name},
        {"name": "ELB_STORAGE_ACCOUNT", "value": storage_account},
        {"name": "ELB_RESOURCE_GROUP", "value": resource_group},
        {"name": "ELB_AZURE_REGION", "value": region},
        {"name": "ELB_ACR_NAME", "value": acr_name},
        {"name": "ELB_ACR_RESOURCE_GROUP", "value": acr_resource_group},
        {"name": "ELB_NUM_NODES", "value": str(max(1, num_nodes))},
        {"name": "ELB_CORE_NT_SHARDS", "value": str(max(1, num_nodes))},
        {
            "name": "PATH",
            "value": (
                "/opt/venv/bin:/usr/local/sbin:"
                "/usr/local/bin:/usr/sbin:/usr/bin:"
                "/sbin:/bin"
            ),
        },
        # api_token is guaranteed non-empty by the build_manifests guard
        # above — no conditional append. Shipping a deployment without
        # this env entry is what produced the recurring "API token not
        # visible" SPA bug; keeping the entry unconditional makes the
        # contract obvious to future readers.
        {"name": "ELB_OPENAPI_API_TOKEN", "value": api_token},
    ]

    deploy_manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "elb-openapi", "namespace": K8S_NAMESPACE},
        "spec": {
            # Two replicas + PDB(minAvailable=1, below) so a single node
            # restart / cordon / pod eviction does not take the BLAST submit
            # path down. The blast pool has 10 nodes; the topology spread
            # constraint below prefers different nodes for the two replicas.
            "replicas": 2,
            "selector": {"matchLabels": {"app": "elb-openapi"}},
            # Surge one extra pod and never go below the running count so
            # an image bump rolls smoothly even though the new pod must
            # pass the readiness probe first.
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
            },
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
                                *openapi_env,
                                # Do not set AZURE_CLIENT_ID manually here. If the
                                # AKS Workload Identity webhook is present it
                                # injects AZURE_CLIENT_ID / AZURE_TENANT_ID /
                                # AZURE_FEDERATED_TOKEN_FILE from the annotated
                                # ServiceAccount. If the webhook is absent, a
                                # manual AZURE_CLIENT_ID makes `az login --identity`
                                # fail with "Identity not found" instead of
                                # falling back to the node managed identity.
                                # Leave azcopy mode to the image/runtime.
                                # The sibling OpenAPI service now downgrades
                                # from WORKLOAD to MSI if the AKS webhook has
                                # not injected a federated token.
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                            # Without probes the Service routes traffic to a
                            # crashlooping pod and the dashboard sees only
                            # 502/connection-reset. `/healthz` is the
                            # sibling OpenAPI service's health endpoint
                            # (same path the dashboard "Try it" allowlist
                            # already proxies).
                            "readinessProbe": {
                                "httpGet": {"path": "/healthz", "port": 8000},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                                "timeoutSeconds": 3,
                                "failureThreshold": 3,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/healthz", "port": 8000},
                                "initialDelaySeconds": 30,
                                "periodSeconds": 30,
                                "timeoutSeconds": 5,
                                "failureThreshold": 3,
                            },
                            # Drain in-flight submit requests before SIGTERM
                            # to avoid orphan jobs on rollout.
                            "lifecycle": {
                                "preStop": {
                                    "exec": {"command": ["sleep", "10"]},
                                },
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
                    # Prefer different nodes for the two replicas so a
                    # single node drain / restart cannot take both down.
                    # ScheduleAnyway (not DoNotSchedule) keeps single-node
                    # blast pools functional while still spreading on
                    # multi-node pools.
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "kubernetes.io/hostname",
                            "whenUnsatisfiable": "ScheduleAnyway",
                            "labelSelector": {
                                "matchLabels": {"app": "elb-openapi"},
                            },
                        },
                    ],
                    "terminationGracePeriodSeconds": 30,
                },
            },
        },
    }

    # PodDisruptionBudget so AKS upgrades / node drains / `kubectl drain`
    # cannot evict the last running elb-openapi pod, which would otherwise
    # take the BLAST submit path down even with two replicas.
    pdb_manifest = {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": "elb-openapi", "namespace": K8S_NAMESPACE},
        "spec": {
            "minAvailable": 1,
            "selector": {"matchLabels": {"app": "elb-openapi"}},
        },
    }

    svc_annotations: dict[str, str] = {
        "service.beta.kubernetes.io/azure-load-balancer-internal": "true",
    }
    if pls is not None and pls.enabled:
        # AKS-managed Private Link Service: enabling these annotations makes
        # the cloud-provider controller stand up a PLS in front of the ILB
        # so callers in non-peered VNets (or other subscriptions) can reach
        # the Service through a Private Endpoint. The annotations are only
        # honoured on Service create; switching an existing Service from
        # ILB-only → PLS requires `kubectl delete svc elb-openapi` first
        # (handled by the deploy task's transition guard).
        svc_annotations["service.beta.kubernetes.io/azure-pls-create"] = "true"
        svc_annotations["service.beta.kubernetes.io/azure-pls-name"] = pls.name
        svc_annotations[
            "service.beta.kubernetes.io/azure-pls-ip-configuration-subnet"
        ] = pls.lb_subnet
        svc_annotations["service.beta.kubernetes.io/azure-pls-visibility"] = (
            pls.visibility
        )
        if pls.auto_approval:
            svc_annotations[
                "service.beta.kubernetes.io/azure-pls-auto-approval"
            ] = pls.auto_approval

    svc_manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "elb-openapi",
            "namespace": K8S_NAMESPACE,
            "annotations": svc_annotations,
        },
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

    docs = [
        sa_manifest,
        role_manifest,
        binding_manifest,
        deploy_manifest,
        pdb_manifest,
        svc_manifest,
    ]
    return "\n---\n".join(json.dumps(d) for d in docs)

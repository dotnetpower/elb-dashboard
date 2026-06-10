"""Kubernetes manifest builder for the ``elb-openapi`` deploy.

Responsibility: Produce the multi-document JSON payload (ServiceAccount + RBAC +
    in-cluster kubeconfig ConfigMap + Deployment + PodDisruptionBudget + Service)
    consumed by ``kubectl apply -f -`` in the kubectl module.
Edit boundaries: Pure manifest construction — no Azure or Kubernetes I/O. If you need a
    new env var, label, or tolerations rule, edit it here and rely on the deploy task to
    pipe it through.
Key entry points: `build_manifests`.
Risky contracts: The blast-pool toleration and `nodeSelector workload=blast` are load-
    bearing — without them the deployment lands on tainted system nodes and serves no
    traffic. `AZURE_CLIENT_ID` is deliberately NOT set in the pod env (workload-identity
    webhook injects it from the annotated ServiceAccount). The Deployment runs
    `replicas: 1` ON PURPOSE: the sibling OpenAPI service holds its job queue in a
    process-local in-memory dict and enforces `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS`
    against that local view only (it never re-reads peers' ConfigMaps), so a second
    replica would (a) multiply the effective run-concurrency ceiling by the replica
    count and (b) strand queued jobs on whichever replica the LoadBalancer happened to
    route them to. One replica = one authoritative queue owner. To preserve that
    invariant even mid-rollout the strategy is `maxUnavailable: 1` + `maxSurge: 0`
    (the old pod terminates before the new one starts, so two queue owners never
    coexist); the brief submit-path gap is covered by the sibling reloading job state
    from its ConfigMaps on startup. The PodDisruptionBudget is `maxUnavailable: 1`
    (NOT `minAvailable: 1`, which on a single replica would block every voluntary node
    drain / AKS upgrade forever). Readiness/liveness probes on `/healthz` restart a
    wedged pod. Single-node blast pools still work because
    `topologySpreadConstraints.whenUnsatisfiable` is `ScheduleAnyway`. The pod's
    own kubectl (used by the service's `/v1/ready` -> `kubectl get --raw /readyz`
    probe) does NOT auto-load in-cluster config, so an `elb-openapi-kubeconfig`
    ConfigMap is mounted read-only at `/etc/elb/kube` and `KUBECONFIG` points at
    it; it authenticates via the ServiceAccount `tokenFile` (auto-rotated) against
    `https://kubernetes.default.svc`. Without it kubectl falls back to
    localhost:8080 and `/v1/ready` returns 503 `k8s_unreachable`.
Validation: `uv run pytest -q api/tests/test_smoke.py api/tests/test_openapi_task.py`.
"""

from __future__ import annotations

import json

from api.tasks.openapi.constants import (
    K8S_NAMESPACE,
    K8S_SA_NAME,
    OPENAPI_MANIFEST_REVISION,
    OPENAPI_MANIFEST_REVISION_ANNOTATION,
    PlsConfig,
)


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
    max_active_submissions: int = 2,
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
        # Server-side cap on concurrently-running BLAST jobs. The hard
        # ceiling is NOT CPU utilisation but the Kubernetes scheduler's
        # CPU-*request* reservation: each shard pod requests cpu=6 and a
        # Standard_E16s_v5 node exposes ~15.74 allocatable CPU, so
        # floor(15.74/6)=2 shard pods fit per node. Because every job
        # spreads exactly one shard pod per node (shards == nodes), the
        # per-node pod ceiling equals the concurrent-job ceiling = 2.
        # A 3rd job's pods request 18 CPU/node > 15.74 and stay Pending
        # even while CPU sits idle. Empirically confirmed 2026-06-03
        # (burst-6 -> peak running_jobs=2, 20 pods, 0 Pending). Default 2
        # is throughput-optimal for this 10x E16 pool; raising it only
        # helps if per-shard CPU request drops or nodes are added.
        {
            "name": "ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS",
            "value": str(max(1, max_active_submissions)),
        },
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
        # The elb-openapi service shells out to
        # `kubectl get --raw /readyz` for its `/v1/ready` probe. The
        # kubectl CLI does NOT auto-load in-cluster config the way the
        # client-go libraries do, so without an explicit kubeconfig it
        # falls back to localhost:8080 and every cluster call fails with
        # "connection refused" — surfacing as the recurring `/v1/ready`
        # 503 `k8s_unreachable` bug even when the cluster is healthy.
        # Point KUBECONFIG at the in-cluster kubeconfig ConfigMap mounted
        # read-only below; it authenticates via the ServiceAccount token
        # (`tokenFile`, auto-rotated) against the standard in-cluster API
        # endpoint `https://kubernetes.default.svc`.
        {"name": "KUBECONFIG", "value": "/etc/elb/kube/config"},
    ]

    # In-cluster kubeconfig consumed by the pod's own kubectl. The CLI
    # needs an explicit kubeconfig (unlike client-go's InClusterConfig).
    # `tokenFile` re-reads the projected ServiceAccount token on every
    # call so token rotation is handled transparently, and
    # `https://kubernetes.default.svc` is the standard in-cluster API
    # endpoint (always present in the API server certificate SAN).
    incluster_kubeconfig = (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "- name: incluster\n"
        "  cluster:\n"
        "    certificate-authority: "
        "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt\n"
        "    server: https://kubernetes.default.svc\n"
        "contexts:\n"
        "- name: incluster\n"
        "  context:\n"
        "    cluster: incluster\n"
        "    user: incluster\n"
        f"    namespace: {K8S_NAMESPACE}\n"
        "current-context: incluster\n"
        "users:\n"
        "- name: incluster\n"
        "  user:\n"
        "    tokenFile: "
        "/var/run/secrets/kubernetes.io/serviceaccount/token\n"
    )
    kubeconfig_manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "elb-openapi-kubeconfig",
            "namespace": K8S_NAMESPACE,
        },
        "data": {"config": incluster_kubeconfig},
    }

    deploy_manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "elb-openapi",
            "namespace": K8S_NAMESPACE,
            # Manifest generation stamp. The dashboard reads this back from the
            # live Deployment to detect a manifest that predates a
            # redeploy-only change (see constants.OPENAPI_MANIFEST_REVISION)
            # and prompt a redeploy. Stringified because K8s annotation values
            # must be strings.
            "annotations": {
                OPENAPI_MANIFEST_REVISION_ANNOTATION: str(OPENAPI_MANIFEST_REVISION),
            },
        },
        "spec": {
            # SINGLE replica on purpose — the sibling OpenAPI service keeps its
            # job queue in a process-local in-memory dict and counts active
            # submissions against that local view only (no cross-replica
            # ConfigMap re-read). A 2nd replica would multiply the effective
            # MAX_ACTIVE_SUBMISSIONS ceiling and strand queued jobs on whichever
            # replica the LoadBalancer routed them to, which is exactly the
            # "/v1/jobs queueing doesn't work" symptom. One replica = one
            # authoritative queue owner.
            "replicas": 1,
            "selector": {"matchLabels": {"app": "elb-openapi"}},
            # maxUnavailable:1 + maxSurge:0 => the old pod terminates BEFORE the
            # new one starts, so two queue owners never coexist even mid-rollout
            # (the opposite of the usual surge-first rollout). The brief
            # submit-path gap is covered by the sibling reloading job state from
            # its ConfigMaps on startup.
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0},
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
                            # Mount the in-cluster kubeconfig (read-only)
                            # so the pod's kubectl can reach the API
                            # server. KUBECONFIG (in openapi_env above)
                            # points at /etc/elb/kube/config.
                            "volumeMounts": [
                                {
                                    "name": "incluster-kubeconfig",
                                    "mountPath": "/etc/elb/kube",
                                    "readOnly": True,
                                },
                            ],
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
                    # Harmless on a single replica today, but retained so that
                    # if the sibling ever moves its queue to a shared store and
                    # this deployment scales back to >1 replica, the pods spread
                    # across nodes. ScheduleAnyway (not DoNotSchedule) keeps
                    # single-node blast pools functional.
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
                    # ConfigMap-backed in-cluster kubeconfig consumed by
                    # the container's kubectl via the KUBECONFIG env +
                    # volumeMount above. The ServiceAccount token it
                    # references is the default projected token mount
                    # (automountServiceAccountToken is left at the
                    # default `true`).
                    "volumes": [
                        {
                            "name": "incluster-kubeconfig",
                            "configMap": {"name": "elb-openapi-kubeconfig"},
                        },
                    ],
                },
            },
        },
    }

    # PodDisruptionBudget uses maxUnavailable:1 (NOT minAvailable:1). On a
    # single-replica deployment minAvailable:1 would forbid EVERY voluntary
    # eviction, so `kubectl drain` / AKS node-image upgrades would hang forever
    # on this pod. maxUnavailable:1 permits the drain (the queue owner is
    # intentionally not HA — it recovers its job state from ConfigMaps on the
    # rescheduled pod's startup).
    pdb_manifest = {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": "elb-openapi", "namespace": K8S_NAMESPACE},
        "spec": {
            "maxUnavailable": 1,
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

    # The elb-openapi pod runs `elastic-blast submit`, which applies a
    # broad set of cluster-scoped objects on every submit: a janitor
    # ClusterRoleBinding that binds the default ServiceAccount to
    # `cluster-admin` (`elb-janitor-rbac.yaml`), a `create-workspace`
    # DaemonSet in `kube-system`, PersistentVolumes, a StorageClass, and
    # the per-batch BLAST Jobs. A previously-shipped narrow custom
    # ClusterRole (`elb-openapi-role`, granting only nodes/pods/configmaps/
    # services + batch/jobs + apps/deployments) made every openapi-driven
    # core_nt submit fail mid-flight — the job marched through one 403 after
    # another (`clusterrolebindings` forbidden -> `serviceaccounts` ->
    # `daemonsets`) and ultimately died as "submit produced no BLAST jobs
    # before stuck timeout". Only terminal/CLI submissions (which carry the
    # cluster-admin kubeconfig) ever succeeded.
    #
    # Scoping below cluster-admin is also security theater here: to apply
    # the janitor binding the SA must hold `bind`/`escalate` on the
    # `cluster-admin` ClusterRole, which already lets it grant itself
    # cluster-admin at will. Binding `elb-openapi-sa` directly to the
    # built-in `cluster-admin` ClusterRole is therefore both the honest
    # representation of its privilege level and the only configuration that
    # keeps pace with elastic-blast's evolving manifest set without
    # whack-a-mole RBAC patches. The pod is internal-only (private
    # LoadBalancer, no public ingress) and is the trusted BLAST control
    # plane, so this is consistent with elastic-blast's own design.
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
            "name": "cluster-admin",
            "apiGroup": "rbac.authorization.k8s.io",
        },
    }

    docs = [
        sa_manifest,
        binding_manifest,
        kubeconfig_manifest,
        deploy_manifest,
        pdb_manifest,
        svc_manifest,
    ]
    return "\n---\n".join(json.dumps(d) for d in docs)

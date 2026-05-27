"""ingress-nginx + cert-manager manifest builders for the public HTTPS path.

Responsibility: Pure manifest construction for the public HTTPS surface in front of
    `elb-openapi` — Ingress, Certificate, and ClusterIssuer YAML/JSON shapes plus the
    public, pinned upstream installer URLs for ingress-nginx and cert-manager.
Edit boundaries: No Azure or Kubernetes I/O here. The Celery task pipes these strings
    into `kubectl apply -f <url>` (for the upstream installers) or
    `kubectl apply -f -` (for the per-cluster Ingress / Issuer). If a new annotation,
    backend, or TLS option is needed, edit it here and trust the task to push it
    through unchanged.
Key entry points: `INGRESS_NGINX_INSTALL_URL`, `CERT_MANAGER_INSTALL_URL`,
    `build_cluster_issuer`, `build_openapi_ingress`, `dns_label_for_cluster`,
    `cloudapp_fqdn`, `patch_manifest_for_system_pool`,
    `fetch_install_manifest_for_system_pool`.
Risky contracts: The pinned installer URLs MUST match versions tested against the
    repo's AKS K8s baseline (1.34+). Bumping either pin without rerunning the
    cert-manager webhook readiness probe + Certificate issuance test will silently
    break the public HTTPS task.
Validation: `uv run pytest -q api/tests/test_openapi_public_https.py`.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any

import yaml as _yaml

# Pinned to the latest minor that supports K8s 1.27+ (current AKS LTS baseline
# is 1.34). The cloud installer file ships ingress-nginx Deployment +
# Service(type=LoadBalancer) + RBAC + ConfigMap in one apply. Idempotent
# re-apply is safe.
INGRESS_NGINX_VERSION = "controller-v1.11.3"
INGRESS_NGINX_INSTALL_URL = (
    "https://raw.githubusercontent.com/kubernetes/ingress-nginx/"
    f"{INGRESS_NGINX_VERSION}/deploy/static/provider/cloud/deploy.yaml"
)

# cert-manager v1.16.x supports K8s 1.28+. The single-file manifest creates
# CRDs + webhook + controller + cainjector. Idempotent re-apply is safe.
CERT_MANAGER_VERSION = "v1.16.2"
CERT_MANAGER_INSTALL_URL = (
    "https://github.com/cert-manager/cert-manager/releases/download/"
    f"{CERT_MANAGER_VERSION}/cert-manager.yaml"
)

# Namespaces created by the installers above. Used by the task to wait for
# webhook readiness and to scope the DNS label patch.
INGRESS_NGINX_NAMESPACE = "ingress-nginx"
INGRESS_NGINX_SERVICE_NAME = "ingress-nginx-controller"
# Deployment name shipped by the upstream ingress-nginx cloud installer.
# The admission-webhook Service (``ingress-nginx-controller-admission``)
# selects this Deployment's Pods, so until at least one controller Pod
# is Ready the webhook Service has no endpoints — and any Ingress
# ``kubectl apply`` returns ``failed calling webhook ... no endpoints
# available for service "ingress-nginx-controller-admission"``. The
# task waits on this Deployment becoming Available before applying the
# elb-openapi Ingress.
INGRESS_NGINX_CONTROLLER_DEPLOYMENT = "ingress-nginx-controller"
CERT_MANAGER_NAMESPACE = "cert-manager"
CERT_MANAGER_WEBHOOK_DEPLOYMENT = "cert-manager-webhook"

OPENAPI_INGRESS_NAME = "elb-openapi-tls"
OPENAPI_TLS_SECRET_NAME = "elb-openapi-tls"  # noqa: S105 - secret name, not a credential.
OPENAPI_CLUSTER_ISSUER_NAME = "letsencrypt-prod"
OPENAPI_NAMESPACE = "default"
OPENAPI_SERVICE_NAME = "elb-openapi"
OPENAPI_SERVICE_PORT = 80

# AKS systempool / blastpool both carry a NoSchedule taint
# (`CriticalAddonsOnly=true` on systempool, `workload=blast` on blastpool).
# The upstream cert-manager and ingress-nginx install manifests carry no
# tolerations, so every pod they ship lands in `Pending` forever on these
# clusters. ``patch_manifest_for_system_pool`` injects the minimum
# toleration + nodeSelector that lets these control-plane add-ons land on
# the systempool only — we deliberately do **not** add a `workload=blast`
# toleration because cert-manager / ingress-nginx must not consume CPU on
# nodes reserved for the BLAST workload.
SYSTEM_POOL_TOLERATION: dict[str, str] = {
    "key": "CriticalAddonsOnly",
    "operator": "Exists",
    "effect": "NoSchedule",
}
SYSTEM_POOL_NODE_SELECTOR: dict[str, str] = {
    "kubernetes.azure.com/mode": "system",
}
# Lowered resource *requests* injected on every container of the patched
# workloads so cert-manager + ingress-nginx fit on the AKS default
# systempool node (Standard_D2s_v3 = 2 vCPU; the upstream ingress-nginx
# manifest requests 100m CPU which on a single-node systempool already
# carrying coredns / azure-wi-webhook / Container Insights ama-metrics
# overflows allocation and leaves the controller Pod in `Pending`
# forever — observed in production on elb-cluster-01: scheduler reported
# "0/3 nodes are available: 1 Insufficient cpu, 2 node(s) had untolerated
# taint(s)"). The lowered numbers reflect idle usage measured by
# kubectl top pod on a steady-state install (controller ~5-15m CPU,
# webhook ~2-5m CPU, ~30-80Mi memory). We only lower the *request* — any
# existing `resources.limits` is left untouched so peak usage is not
# capped. The patch is monotonic-decreasing: if the upstream already
# requests less than these floors, the existing (lower) request wins.
SYSTEM_POOL_LOW_CPU_REQUEST = "20m"
SYSTEM_POOL_LOW_MEMORY_REQUEST = "64Mi"
# Workload kinds whose podTemplate must carry the systempool patch. We
# patch Jobs too because ingress-nginx ships admission-webhook bootstrap
# Jobs whose Pods would otherwise be Pending.
_SYSTEM_POOL_WORKLOAD_KINDS: frozenset[str] = frozenset(
    {"Deployment", "DaemonSet", "StatefulSet", "Job", "ReplicaSet"}
)


def _parse_cpu_to_millicores(value: object) -> int | None:
    """Best-effort parse of a Kubernetes CPU quantity into millicores.

    Returns ``None`` for shapes we don't recognise so the caller leaves
    the existing request untouched (safer than guessing). Handles the
    two forms used by upstream manifests: bare CPU count (``"1"``,
    ``"0.5"``) and millicore suffix (``"100m"``). Other valid Kubernetes
    suffixes (``n``, ``u``) are rejected — we have not seen them in any
    ingress-nginx / cert-manager release.
    """
    if isinstance(value, int | float):
        return int(float(value) * 1000)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("m"):
        try:
            return int(text[:-1])
        except ValueError:
            return None
    try:
        return int(float(text) * 1000)
    except ValueError:
        return None


def _parse_memory_to_bytes(value: object) -> int | None:
    """Best-effort parse of a Kubernetes memory quantity into bytes.

    Returns ``None`` for unrecognised shapes so the caller leaves the
    existing request untouched. Handles the IEC suffixes that upstream
    manifests actually use (``Ki``, ``Mi``, ``Gi``) plus bare integers.
    """
    if isinstance(value, int | float):
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    multipliers = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}
    for suffix, mult in multipliers.items():
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * mult)
            except ValueError:
                return None
    try:
        return int(text)
    except ValueError:
        return None


_LOW_CPU_MILLICORES = _parse_cpu_to_millicores(SYSTEM_POOL_LOW_CPU_REQUEST) or 20
_LOW_MEMORY_BYTES = _parse_memory_to_bytes(SYSTEM_POOL_LOW_MEMORY_REQUEST) or (64 * 1024**2)


def _shrink_container_requests(container: Any) -> None:
    """Lower the container's CPU/memory *requests* to fit the systempool.

    Monotonic-decreasing: if an upstream container is already requesting
    less than the floor, the existing request is preserved. Limits and
    every other field are left alone. Containers without
    ``resources.requests`` get the floors written in.
    """
    if not isinstance(container, dict):
        return
    resources = container.setdefault("resources", {})
    if not isinstance(resources, dict):
        return
    requests = resources.setdefault("requests", {})
    if not isinstance(requests, dict):
        return

    cpu_existing = _parse_cpu_to_millicores(requests.get("cpu"))
    if cpu_existing is None or cpu_existing > _LOW_CPU_MILLICORES:
        requests["cpu"] = SYSTEM_POOL_LOW_CPU_REQUEST

    mem_existing = _parse_memory_to_bytes(requests.get("memory"))
    if mem_existing is None or mem_existing > _LOW_MEMORY_BYTES:
        requests["memory"] = SYSTEM_POOL_LOW_MEMORY_REQUEST
# Label selector for the ingress-nginx admission-webhook Jobs. Jobs are
# spec-immutable, so an earlier failed install (toleration-less) leaves
# Pending pods behind that ``kubectl apply -f -`` cannot reconcile. The
# task pre-deletes them by this label before applying the patched
# manifest so the systempool-tolerating Jobs can be recreated cleanly.
INGRESS_NGINX_ADMISSION_JOB_SELECTOR = "app.kubernetes.io/component=admission-webhook"


def patch_manifest_for_system_pool(raw_manifest: str) -> str:
    """Inject systempool toleration + nodeSelector into every workload doc.

    Pure transform — accepts a multi-doc YAML string (the kind shipped
    by the upstream cert-manager / ingress-nginx install URLs) and
    returns the same documents with each Deployment / DaemonSet /
    StatefulSet / Job / ReplicaSet podTemplate carrying:

    - tolerations: an entry equivalent to ``SYSTEM_POOL_TOLERATION``
      (added only if no existing entry already keys on
      ``CriticalAddonsOnly``).
    - nodeSelector: ``kubernetes.azure.com/mode=system`` (added only if
      that key is not already set; other selector keys are preserved).

    Non-workload kinds (CRDs, ServiceAccounts, RBAC, Services, ConfigMaps,
    Secrets, WebhookConfigurations, …) pass through unchanged. Empty
    `---` separator docs are preserved.

    Network-free so callers can unit-test it without hitting GitHub.
    """
    docs: list[Any] = list(_yaml.safe_load_all(raw_manifest))
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") not in _SYSTEM_POOL_WORKLOAD_KINDS:
            continue
        spec = doc.setdefault("spec", {})
        if not isinstance(spec, dict):
            continue
        template = spec.setdefault("template", {})
        if not isinstance(template, dict):
            continue
        pod_spec = template.setdefault("spec", {})
        if not isinstance(pod_spec, dict):
            continue

        tolerations = pod_spec.get("tolerations")
        if not isinstance(tolerations, list):
            tolerations = []
            pod_spec["tolerations"] = tolerations
        if not any(
            isinstance(t, dict) and t.get("key") == SYSTEM_POOL_TOLERATION["key"]
            for t in tolerations
        ):
            tolerations.append(dict(SYSTEM_POOL_TOLERATION))

        node_selector = pod_spec.get("nodeSelector")
        if not isinstance(node_selector, dict):
            node_selector = {}
            pod_spec["nodeSelector"] = node_selector
        for k, v in SYSTEM_POOL_NODE_SELECTOR.items():
            node_selector.setdefault(k, v)

        # Shrink CPU/memory requests so the controller + webhook Pods
        # fit on the default single-node D2s_v3 systempool (the upstream
        # 100m CPU request was the actual blocker on elb-cluster-01).
        # See SYSTEM_POOL_LOW_CPU_REQUEST docstring for the measurement
        # basis. Init-containers get the same treatment because they
        # also count against the node's allocatable CPU at scheduling
        # time.
        for container in pod_spec.get("containers", []) or []:
            _shrink_container_requests(container)
        for init_container in pod_spec.get("initContainers", []) or []:
            _shrink_container_requests(init_container)

    # Drop empty / null documents before re-serialising. Upstream
    # cert-manager.yaml ends with a trailing `---` whose `safe_load`
    # value is ``None``; `safe_dump_all` would then emit `--- null\n`
    # which kubectl rejects with
    # `invalid Yaml document separator: null` and aborts the apply.
    return _yaml.safe_dump_all([d for d in docs if d is not None], sort_keys=False)


def fetch_install_manifest_for_system_pool(url: str, *, timeout_seconds: int = 60) -> str:
    """Fetch an upstream install manifest and inject the systempool patch.

    Thin network wrapper around :func:`patch_manifest_for_system_pool`.
    The patched bytes are what the public-HTTPS Celery task pipes into
    ``kubectl apply -f -`` so cert-manager / ingress-nginx land on the
    only node pool that will admit them.
    """
    with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:  # noqa: S310 - pinned upstream URL list.
        raw = resp.read().decode("utf-8")
    return patch_manifest_for_system_pool(raw)


def dns_label_for_cluster(*, subscription_id: str, cluster_name: str) -> str:
    """Stable, region-unique DNS label for the public HTTPS endpoint.

    Azure requires the DNS label to be unique within a region. Embedding a
    sub-id + cluster-name hash gives the same label across idempotent
    re-runs (so the existing Public IP and cert are reused) while keeping
    different clusters in the same subscription collision-free.
    """
    digest = hashlib.sha256(
        f"{subscription_id}/{cluster_name}".encode()
    ).hexdigest()[:10]
    return f"elb-openapi-{digest}"


def cloudapp_fqdn(*, dns_label: str, region: str) -> str:
    """Return the public FQDN Azure auto-assigns to a labelled Public IP."""
    return f"{dns_label}.{region}.cloudapp.azure.com"


def build_cluster_issuer(*, email: str) -> str:
    """Return the ClusterIssuer manifest (JSON, kubectl-accepted) for LE prod.

    HTTP-01 with the nginx ingress class. DNS-01 would need workload-identity
    + an Azure DNS Zone; HTTP-01 only needs port 80 reachable to the
    ingress-nginx LB, which the AKS-provisioned Standard LB allows by default.
    """
    issuer = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "ClusterIssuer",
        "metadata": {"name": OPENAPI_CLUSTER_ISSUER_NAME},
        "spec": {
            "acme": {
                "server": "https://acme-v02.api.letsencrypt.org/directory",
                "email": email,
                "privateKeySecretRef": {"name": f"{OPENAPI_CLUSTER_ISSUER_NAME}-key"},
                "solvers": [
                    {
                        "http01": {
                            "ingress": {"class": "nginx"},
                        },
                    },
                ],
            },
        },
    }
    return json.dumps(issuer, separators=(",", ":"))


def build_openapi_ingress(*, fqdn: str) -> str:
    """Return the Ingress manifest (JSON) routing `<fqdn>` → svc/elb-openapi:80.

    Annotations cover the load-bearing bits:
    - `cert-manager.io/cluster-issuer` triggers automatic cert issuance.
    - `nginx.ingress.kubernetes.io/ssl-redirect=true` forces HTTPS so clients
      that send the admin token over a plain-HTTP redirect path cannot leak it.
    - `nginx.ingress.kubernetes.io/proxy-body-size=100m` matches the existing
      api-sidecar streaming proxy ceiling so large BLAST query uploads do not
      get 413'd at the ingress layer.
    """
    ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": OPENAPI_INGRESS_NAME,
            "namespace": OPENAPI_NAMESPACE,
            "annotations": {
                "cert-manager.io/cluster-issuer": OPENAPI_CLUSTER_ISSUER_NAME,
                "nginx.ingress.kubernetes.io/ssl-redirect": "true",
                "nginx.ingress.kubernetes.io/proxy-body-size": "100m",
            },
        },
        "spec": {
            "ingressClassName": "nginx",
            "tls": [{"hosts": [fqdn], "secretName": OPENAPI_TLS_SECRET_NAME}],
            "rules": [
                {
                    "host": fqdn,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": OPENAPI_SERVICE_NAME,
                                        "port": {"number": OPENAPI_SERVICE_PORT},
                                    },
                                },
                            },
                        ],
                    },
                },
            ],
        },
    }
    return json.dumps(ingress, separators=(",", ":"))


def build_dns_label_patch(*, dns_label: str) -> str:
    """Return a strategic-merge patch that assigns the Azure DNS label.

    Applied to the ingress-nginx Service (type=LoadBalancer). AKS's cloud
    controller reads `service.beta.kubernetes.io/azure-dns-label-name`
    and configures the DNS label on the auto-created Public IP, without
    needing any extra Network Contributor RBAC on the dashboard MI.
    """
    patch = {
        "metadata": {
            "annotations": {
                "service.beta.kubernetes.io/azure-dns-label-name": dns_label,
            },
        },
    }
    return json.dumps(patch, separators=(",", ":"))

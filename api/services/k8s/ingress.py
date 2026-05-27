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
    `cloudapp_fqdn`.
Risky contracts: The pinned installer URLs MUST match versions tested against the
    repo's AKS K8s baseline (1.34+). Bumping either pin without rerunning the
    cert-manager webhook readiness probe + Certificate issuance test will silently
    break the public HTTPS task.
Validation: `uv run pytest -q api/tests/test_openapi_public_https.py`.
"""

from __future__ import annotations

import hashlib
import json

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
CERT_MANAGER_NAMESPACE = "cert-manager"
CERT_MANAGER_WEBHOOK_DEPLOYMENT = "cert-manager-webhook"

OPENAPI_INGRESS_NAME = "elb-openapi-tls"
OPENAPI_TLS_SECRET_NAME = "elb-openapi-tls"  # noqa: S105 - secret name, not a credential.
OPENAPI_CLUSTER_ISSUER_NAME = "letsencrypt-prod"
OPENAPI_NAMESPACE = "default"
OPENAPI_SERVICE_NAME = "elb-openapi"
OPENAPI_SERVICE_PORT = 80


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

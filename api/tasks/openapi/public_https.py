"""`setup_openapi_public_https` Celery task — turn on a public HTTPS endpoint.

Responsibility: Drive the idempotent 9-step pipeline that puts ingress-nginx +
    cert-manager + a Let's Encrypt-signed Ingress in front of the in-cluster
    `elb-openapi` Service, exposing it at `<dns-label>.<region>.cloudapp.azure.com`
    over HTTPS so external Azure VMs in non-peered VNets can call the API directly
    (`curl https://...` with `X-ELB-API-Token`). Persists the resulting public base
    URL so the dashboard flips its API Reference / Try-It to the HTTPS endpoint
    without needing a Container App revision swap.
Edit boundaries: Wiring only. Manifest construction lives in
    `api.services.k8s.ingress`; kubectl auth + invocation lives in
    `api.tasks.openapi.kubectl`; runtime cache lives in
    `api.services.openapi.runtime`. New steps go into a sibling helper, not here.
Key entry points: `setup_openapi_public_https`, `disable_openapi_public_https`,
    `get_openapi_public_https_status`.
Risky contracts: Task names `api.tasks.openapi.setup_openapi_public_https` /
    `api.tasks.openapi.disable_openapi_public_https` must not change — the routes
    + SPA reference them. The 9 steps MUST stay idempotent so the task can be
    retried (e.g. after a transient ACME failure) without leaving partial state.
    Never log the cert private key or Let's Encrypt account key — both live only
    in K8s Secrets and never leave the cluster.
Validation: `uv run pytest -q api/tests/test_openapi_public_https.py`.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from celery import shared_task

from api.services import get_credential
from api.services.azure_clients import aks_client
from api.services.k8s.ingress import (
    CERT_MANAGER_INSTALL_URL,
    CERT_MANAGER_NAMESPACE,
    CERT_MANAGER_WEBHOOK_DEPLOYMENT,
    INGRESS_NGINX_INSTALL_URL,
    INGRESS_NGINX_NAMESPACE,
    INGRESS_NGINX_SERVICE_NAME,
    OPENAPI_CLUSTER_ISSUER_NAME,
    OPENAPI_INGRESS_NAME,
    OPENAPI_NAMESPACE,
    OPENAPI_TLS_SECRET_NAME,
    build_cluster_issuer,
    build_dns_label_patch,
    build_openapi_ingress,
    cloudapp_fqdn,
    dns_label_for_cluster,
)
from api.services.openapi.runtime import (
    clear_openapi_public_base_url,
    get_openapi_public_base_url,
    save_openapi_public_base_url,
)
from api.tasks.openapi.helpers import record_progress
from api.tasks.openapi.kubectl import ensure_admin_kubeconfig, kubectl_run

LOGGER = logging.getLogger(__name__)

_DEFAULT_OPERATOR_EMAIL_ENV = "ELB_OPERATOR_EMAIL"
# Let's Encrypt accepts any RFC 5322 valid address; this fallback is used only
# when the operator did not supply an email via env or POST body. Expiry-warning
# mail will black-hole, but the cert still issues + renews.
_FALLBACK_OPERATOR_EMAIL = "noreply@elb-dashboard.local"


def _resolve_operator_email(provided: str = "") -> str:
    provided = (provided or "").strip()
    if provided:
        return provided
    env_value = os.environ.get(_DEFAULT_OPERATOR_EMAIL_ENV, "").strip()
    if env_value:
        return env_value
    return _FALLBACK_OPERATOR_EMAIL


def _kubectl_or_raise(
    args: list[str],
    *,
    kubeconfig_path: str,
    stdin: str | None = None,
    timeout_seconds: int = 60,
    context: str = "",
) -> dict[str, Any]:
    """Run kubectl, raise RuntimeError on non-zero exit with sanitised detail.

    Used by the apply / patch / wait steps where any non-zero exit means
    the pipeline cannot continue. Steps that legitimately tolerate
    non-zero (e.g. ``get`` polling for EXTERNAL-IP) should call
    ``kubectl_run`` directly and inspect the result.
    """
    result = kubectl_run(
        args,
        kubeconfig_path=kubeconfig_path,
        stdin=stdin,
        timeout_seconds=timeout_seconds,
    )
    if result.get("exit_code", 1) != 0:
        detail = (result.get("stderr") or result.get("stdout") or "").strip()[:600]
        raise RuntimeError(f"kubectl {context or args[0]} failed: {detail}")
    return result


def _wait_for_external_ip(*, kubeconfig_path: str, timeout_seconds: int = 240) -> str:
    """Poll the ingress-nginx Service for its LoadBalancer EXTERNAL-IP."""
    deadline = time.time() + timeout_seconds
    last_err = ""
    while time.time() < deadline:
        result = kubectl_run(
            [
                "get",
                "service",
                INGRESS_NGINX_SERVICE_NAME,
                "-n",
                INGRESS_NGINX_NAMESPACE,
                "-o",
                "jsonpath={.status.loadBalancer.ingress[0].ip}",
            ],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=15,
        )
        if result.get("exit_code", 1) == 0:
            ip = (result.get("stdout") or "").strip()
            if ip:
                return ip
            last_err = "ingress-nginx Service has no EXTERNAL-IP yet"
        else:
            last_err = (result.get("stderr") or "").strip()[:200]
        time.sleep(5)
    raise RuntimeError(
        f"ingress-nginx Service did not get an EXTERNAL-IP within {timeout_seconds}s "
        f"(last: {last_err or 'n/a'})"
    )


def _wait_for_certificate_ready(
    *,
    kubeconfig_path: str,
    timeout_seconds: int = 300,
) -> None:
    """Block until the openapi Certificate resource is Ready=True."""
    result = kubectl_run(
        [
            "wait",
            "--for=condition=Ready=true",
            f"certificate/{OPENAPI_TLS_SECRET_NAME}",
            "-n",
            OPENAPI_NAMESPACE,
            f"--timeout={timeout_seconds}s",
        ],
        kubeconfig_path=kubeconfig_path,
        timeout_seconds=timeout_seconds + 30,
    )
    if result.get("exit_code", 1) != 0:
        detail = (result.get("stderr") or result.get("stdout") or "").strip()[:600]
        # Surface the Order/Challenge reason so the operator can tell
        # "DNS not propagated" apart from "rate limited" apart from
        # "challenge HTTP-01 fetch failed".
        probe = kubectl_run(
            [
                "get",
                "certificate",
                OPENAPI_TLS_SECRET_NAME,
                "-n",
                OPENAPI_NAMESPACE,
                "-o",
                "jsonpath={.status.conditions[?(@.type=='Ready')].message}",
            ],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=15,
        )
        probe_msg = (probe.get("stdout") or "").strip()
        raise RuntimeError(
            f"Certificate {OPENAPI_TLS_SECRET_NAME} did not become Ready: "
            f"{detail}"
            + (f" | last condition: {probe_msg[:400]}" if probe_msg else "")
        )


def _read_certificate_expiry(*, kubeconfig_path: str) -> str:
    """Return the cert's NotAfter timestamp, or empty string if unavailable."""
    result = kubectl_run(
        [
            "get",
            "certificate",
            OPENAPI_TLS_SECRET_NAME,
            "-n",
            OPENAPI_NAMESPACE,
            "-o",
            "jsonpath={.status.notAfter}",
        ],
        kubeconfig_path=kubeconfig_path,
        timeout_seconds=15,
    )
    if result.get("exit_code", 1) != 0:
        return ""
    return (result.get("stdout") or "").strip()


@shared_task(
    name="api.tasks.openapi.setup_openapi_public_https",
    bind=True,
    max_retries=0,
)
def setup_openapi_public_https(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    operator_email: str = "",
    caller_oid: str = "",
) -> dict[str, Any]:
    """Install / refresh the public HTTPS path in front of `elb-openapi`.

    Idempotent: re-running the task on a cluster that already has the
    pipeline applied is a no-op for steps 1-3 (kubectl apply -f the
    upstream installers re-applies the same objects), and steps 4-6
    update existing ClusterIssuer / Ingress in place. Step 7 reuses the
    existing Certificate Secret when present, so cert renewal stays on
    cert-manager's own 60-day schedule and we do not burn a Let's
    Encrypt rate-limit slot per click.
    """

    started = time.time()
    cred = get_credential()
    aks = aks_client(cred, subscription_id)
    cluster = aks.managed_clusters.get(resource_group, cluster_name)
    region = (cluster.location or "").strip().lower()
    if not region:
        return {
            "status": "failed",
            "error": "Could not resolve AKS cluster region",
        }

    email = _resolve_operator_email(operator_email)
    dns_label = dns_label_for_cluster(
        subscription_id=subscription_id,
        cluster_name=cluster_name,
    )
    fqdn = cloudapp_fqdn(dns_label=dns_label, region=region)

    record_progress(self, "ensure_kubeconfig", cluster_name=cluster_name)
    try:
        kubeconfig_path = ensure_admin_kubeconfig(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    except Exception as exc:
        LOGGER.exception("public-https: kubeconfig fetch failed")
        return {"status": "failed", "step": "ensure_kubeconfig", "error": str(exc)[:500]}

    try:
        # Step 1: install ingress-nginx (idempotent kubectl apply -f URL)
        record_progress(self, "install_ingress_nginx")
        _kubectl_or_raise(
            ["apply", "-f", INGRESS_NGINX_INSTALL_URL],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=180,
            context="apply ingress-nginx",
        )

        # Step 2: patch ingress-nginx Service with Azure DNS label annotation
        record_progress(self, "patch_dns_label", dns_label=dns_label)
        patch = build_dns_label_patch(dns_label=dns_label)
        _kubectl_or_raise(
            [
                "patch",
                "service",
                INGRESS_NGINX_SERVICE_NAME,
                "-n",
                INGRESS_NGINX_NAMESPACE,
                "--type",
                "merge",
                "--patch",
                patch,
            ],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=60,
            context="patch ingress-nginx Service",
        )

        # Step 3: wait for the ingress-nginx LB to get a public IP
        record_progress(self, "wait_external_ip")
        ingress_lb_ip = _wait_for_external_ip(
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=240,
        )

        # Step 4: install cert-manager (idempotent kubectl apply -f URL)
        record_progress(self, "install_cert_manager")
        _kubectl_or_raise(
            ["apply", "-f", CERT_MANAGER_INSTALL_URL],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=300,
            context="apply cert-manager",
        )

        # Step 5: wait for cert-manager webhook Ready (rest of cert-manager
        # is fast, but the webhook serving cert spins up via a self-signed
        # bootstrap and takes ~30-60 s on a cold cluster)
        record_progress(self, "wait_cert_manager_webhook")
        _kubectl_or_raise(
            [
                "wait",
                "--for=condition=Available=true",
                f"deployment/{CERT_MANAGER_WEBHOOK_DEPLOYMENT}",
                "-n",
                CERT_MANAGER_NAMESPACE,
                "--timeout=180s",
            ],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=200,
            context="wait cert-manager-webhook",
        )

        # Step 6: ClusterIssuer (Let's Encrypt prod, HTTP-01)
        record_progress(self, "apply_cluster_issuer", email=_mask_email(email))
        _kubectl_or_raise(
            ["apply", "-f", "-"],
            kubeconfig_path=kubeconfig_path,
            stdin=build_cluster_issuer(email=email),
            timeout_seconds=60,
            context="apply ClusterIssuer",
        )

        # Step 7: Ingress + Certificate (cert-manager auto-creates the
        # Certificate CR from the Ingress's tls block + annotation)
        record_progress(self, "apply_ingress", fqdn=fqdn)
        _kubectl_or_raise(
            ["apply", "-f", "-"],
            kubeconfig_path=kubeconfig_path,
            stdin=build_openapi_ingress(fqdn=fqdn),
            timeout_seconds=60,
            context="apply Ingress",
        )

        # Step 8: wait for the cert to become Ready (HTTP-01 challenge +
        # ACME order). First issuance is ~30-120 s; renewals are <10 s.
        record_progress(self, "wait_certificate_ready", fqdn=fqdn)
        _wait_for_certificate_ready(kubeconfig_path=kubeconfig_path, timeout_seconds=300)

        # Step 9: persist the public base URL so the dashboard flips its
        # baseUrl to https://<fqdn> on the next API Reference render
        record_progress(self, "persist_runtime_cache")
        public_base_url = f"https://{fqdn}"
        cert_expires_at = _read_certificate_expiry(kubeconfig_path=kubeconfig_path)
        save_openapi_public_base_url(
            public_base_url,
            metadata={
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
                "dns_label": dns_label,
                "region": region,
                "ingress_lb_ip": ingress_lb_ip,
                "cert_issuer": OPENAPI_CLUSTER_ISSUER_NAME,
                "cert_expires_at": cert_expires_at,
                "source": "setup_openapi_public_https",
            },
        )
    except Exception as exc:
        LOGGER.exception("public-https: pipeline failed")
        return {
            "status": "failed",
            "error": str(exc)[:800],
            "fqdn": fqdn,
            "elapsed_seconds": int(time.time() - started),
        }

    elapsed = int(time.time() - started)
    LOGGER.info(
        "public-https: ready fqdn=%s ingress_lb_ip=%s elapsed=%ss",
        fqdn,
        ingress_lb_ip,
        elapsed,
    )
    return {
        "status": "succeeded",
        "fqdn": fqdn,
        "public_base_url": public_base_url,
        "dns_label": dns_label,
        "region": region,
        "ingress_lb_ip": ingress_lb_ip,
        "cert_expires_at": cert_expires_at,
        "cluster_issuer": OPENAPI_CLUSTER_ISSUER_NAME,
        "elapsed_seconds": elapsed,
    }


@shared_task(
    name="api.tasks.openapi.disable_openapi_public_https",
    bind=True,
    max_retries=0,
)
def disable_openapi_public_https(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    caller_oid: str = "",
) -> dict[str, Any]:
    """Tear down the per-elb-openapi Ingress + Certificate + cached URL.

    Leaves ingress-nginx and cert-manager installed (other apps / a
    future re-enable benefit from the existing deployment + the cached
    ACME account key). Operators that want to fully uninstall must run
    `kubectl delete -f <installer-url>` manually.
    """

    started = time.time()
    record_progress(self, "ensure_kubeconfig", cluster_name=cluster_name)
    try:
        kubeconfig_path = ensure_admin_kubeconfig(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    except Exception as exc:
        return {"status": "failed", "step": "ensure_kubeconfig", "error": str(exc)[:500]}

    deleted: list[str] = []
    for kind, name, namespace in (
        ("ingress", OPENAPI_INGRESS_NAME, OPENAPI_NAMESPACE),
        ("certificate", OPENAPI_TLS_SECRET_NAME, OPENAPI_NAMESPACE),
    ):
        record_progress(self, f"delete_{kind}", name=name)
        result = kubectl_run(
            ["delete", kind, name, "-n", namespace, "--ignore-not-found=true"],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=60,
        )
        if result.get("exit_code", 1) == 0:
            deleted.append(f"{kind}/{name}")
        else:
            LOGGER.warning(
                "public-https: delete %s/%s failed: %s",
                kind,
                name,
                (result.get("stderr") or "").strip()[:200],
            )

    clear_openapi_public_base_url()

    return {
        "status": "succeeded",
        "deleted": deleted,
        "elapsed_seconds": int(time.time() - started),
    }


def get_openapi_public_https_status() -> dict[str, Any]:
    """Return the current public HTTPS state for the SPA's panel.

    Reads from the runtime cache only — no kubectl round trip — so the
    SPA can poll cheaply. The cache is written by
    `setup_openapi_public_https` on success and cleared by
    `disable_openapi_public_https`, so its presence is the source of
    truth for "enabled" vs "not enabled".
    """
    cached = get_openapi_public_base_url()
    if not cached:
        return {"enabled": False}
    metadata = cached.get("metadata") or {}
    return {
        "enabled": True,
        "fqdn": cached.get("base_url", "").removeprefix("https://"),
        "public_base_url": cached.get("base_url", ""),
        "dns_label": metadata.get("dns_label", ""),
        "region": metadata.get("region", ""),
        "ingress_lb_ip": metadata.get("ingress_lb_ip", ""),
        "cert_issuer": metadata.get("cert_issuer", ""),
        "cert_expires_at": metadata.get("cert_expires_at", ""),
        "updated_at": cached.get("updated_at", ""),
    }


def _mask_email(value: str) -> str:
    if not value or "@" not in value:
        return ""
    local, _, domain = value.partition("@")
    if len(local) <= 2:
        return f"{local[:1]}*@{domain}"
    return f"{local[:1]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


# Re-export for `from api.tasks.openapi import setup_openapi_public_https`.
__all__ = [
    "disable_openapi_public_https",
    "get_openapi_public_https_status",
    "setup_openapi_public_https",
]

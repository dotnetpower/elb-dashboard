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
    INGRESS_NGINX_ADMISSION_JOB_SELECTOR,
    INGRESS_NGINX_CONTROLLER_DEPLOYMENT,
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
    fetch_install_manifest_for_system_pool,
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


def _resolve_operator_email(provided: str = "") -> str:
    """Return the operator email, preferring the caller-provided value.

    The dashboard SPA auto-fills this field from `/api/me` (validated MSAL
    `upn`), and the FastAPI route rejects empty / private-TLD values before
    the task is enqueued (see `OpenApiPublicHttpsRequest`). The env
    `ELB_OPERATOR_EMAIL` is the operator-side fallback when the env is set
    deliberately. We deliberately do **not** ship a hard-coded `.local`
    fallback because Let's Encrypt rejects ACME registration on any private
    TLD with `urn:ietf:params:acme:error:invalidContact` and the whole
    public-https pipeline silently fails (regression on elb-cluster-01,
    2026-05-27).
    """
    provided = (provided or "").strip()
    if provided:
        return provided
    env_value = os.environ.get(_DEFAULT_OPERATOR_EMAIL_ENV, "").strip()
    if env_value:
        return env_value
    raise ValueError(
        "operator_email is required — caller must pass a public-TLD email "
        "(SPA auto-fills from /api/me) or set ELB_OPERATOR_EMAIL"
    )


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


_CERT_MANAGER_WEBHOOK_PROBE_RETRIES = 10
_CERT_MANAGER_WEBHOOK_PROBE_TIMEOUT_SECONDS = 60
_CERT_MANAGER_WEBHOOK_PROBE_INTERVAL_SECONDS = 15
_CERT_MANAGER_WEBHOOK_AVAILABLE_TIMEOUT_SECONDS = 300

_INGRESS_NGINX_CONTROLLER_PROBE_RETRIES = 10
_INGRESS_NGINX_CONTROLLER_PROBE_TIMEOUT_SECONDS = 60
_INGRESS_NGINX_CONTROLLER_PROBE_INTERVAL_SECONDS = 15
_INGRESS_NGINX_CONTROLLER_AVAILABLE_TIMEOUT_SECONDS = 300


def _wait_for_ingress_nginx_controller(
    *, kubeconfig_path: str
) -> dict[str, Any]:
    """Wait for the ingress-nginx controller Deployment to become Available.

    The Ingress admission webhook is served by the ingress-nginx
    controller Pod itself (Service ``ingress-nginx-controller-admission``
    selects the controller Pods). On a cold cluster the Service object
    exists \u2014 and so the LoadBalancer EXTERNAL-IP wait in Step 3 returns
    successfully \u2014 well before any controller Pod has finished pulling
    its image, registering its TLS keypair, and flipping to Ready, so a
    naive ``kubectl apply -f <ingress>`` immediately after Step 6 fails
    with:

        Error from server (InternalError): failed calling webhook
        "validate.nginx.ingress.kubernetes.io": ... no endpoints
        available for service "ingress-nginx-controller-admission"

    The retry pattern mirrors ``_wait_for_cert_manager_webhook``: short
    ``kubectl rollout status`` probes (which return ``NotFound``
    immediately when the Deployment does not yet exist server-side)
    spaced by a sleep so the advertised budget is not collapsed by
    fast-failing probes, followed by a single generous
    ``kubectl wait --for=condition=Available`` for the final readiness
    flip. Total budget \u2248 5 min, matching the cert-manager-webhook
    helper.
    """
    last_err = ""
    probe_started = time.time()
    success = False
    for attempt in range(1, _INGRESS_NGINX_CONTROLLER_PROBE_RETRIES + 1):
        result = kubectl_run(
            [
                "rollout",
                "status",
                f"deployment/{INGRESS_NGINX_CONTROLLER_DEPLOYMENT}",
                "-n",
                INGRESS_NGINX_NAMESPACE,
                f"--timeout={_INGRESS_NGINX_CONTROLLER_PROBE_TIMEOUT_SECONDS}s",
            ],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=_INGRESS_NGINX_CONTROLLER_PROBE_TIMEOUT_SECONDS + 20,
        )
        if result.get("exit_code", 1) == 0:
            success = True
            break
        last_err = (
            (result.get("stderr") or result.get("stdout") or "").strip()[:300]
        )
        LOGGER.info(
            "public-https: ingress-nginx-controller rollout probe %d/%d not ready yet: %s",
            attempt,
            _INGRESS_NGINX_CONTROLLER_PROBE_RETRIES,
            last_err,
        )
        if attempt < _INGRESS_NGINX_CONTROLLER_PROBE_RETRIES:
            time.sleep(_INGRESS_NGINX_CONTROLLER_PROBE_INTERVAL_SECONDS)
    if not success:
        elapsed = int(time.time() - probe_started)
        raise RuntimeError(
            "kubectl rollout status ingress-nginx-controller failed after "
            f"{_INGRESS_NGINX_CONTROLLER_PROBE_RETRIES} probes "
            f"({elapsed}s elapsed); last error: {last_err}"
        )

    return _kubectl_or_raise(
        [
            "wait",
            "--for=condition=Available=true",
            f"deployment/{INGRESS_NGINX_CONTROLLER_DEPLOYMENT}",
            "-n",
            INGRESS_NGINX_NAMESPACE,
            f"--timeout={_INGRESS_NGINX_CONTROLLER_AVAILABLE_TIMEOUT_SECONDS}s",
        ],
        kubeconfig_path=kubeconfig_path,
        timeout_seconds=_INGRESS_NGINX_CONTROLLER_AVAILABLE_TIMEOUT_SECONDS + 30,
        context="wait ingress-nginx-controller",
    )


_INGRESS_NGINX_ADMISSION_JOB_WAIT_TIMEOUT_SECONDS = 180
_INGRESS_NGINX_ADMISSION_JOBS = (
    "ingress-nginx-admission-create",
    "ingress-nginx-admission-patch",
)


def _wait_for_ingress_nginx_admission_jobs(*, kubeconfig_path: str) -> None:
    """Wait for the ingress-nginx admission bootstrap Jobs to Complete.

    The upstream ingress-nginx install manifest ships two pre-controller
    Jobs (``ingress-nginx-admission-create`` and
    ``ingress-nginx-admission-patch``). They generate the admission
    webhook's TLS keypair (Secret ``ingress-nginx-admission``) and patch
    its ``caBundle`` into the ``ValidatingWebhookConfiguration``. Until
    BOTH Jobs reach the ``Complete`` condition, the caBundle is empty
    and any Ingress apply fails with::

        x509: certificate signed by unknown authority

    (a *different* error from the "no endpoints available" race the
    Deployment wait above handles \u2014 same root cause family, but the
    apiserver sees a TLS verify failure instead of a missing endpoint).
    This wait is layered defense: even if the Deployment is Available
    and the EndpointSlice is published, a missing caBundle still
    breaks the apply.

    Idempotency: re-running setup on a cluster where the Jobs already
    Completed is a no-op (``kubectl wait`` returns immediately when
    the condition is already true on the Job object). The pre-delete
    in Step 1 ensures the *current* Jobs are the ones we wait on.

    The wait is best-effort: if a Job is missing (rare \u2014 only happens
    when an operator manually removed the upstream Jobs) we log and
    move on rather than fail the whole pipeline, because the
    subsequent endpoint readiness probe + apply retry will surface
    the real problem with a clearer error if one is still present.
    """
    for job_name in _INGRESS_NGINX_ADMISSION_JOBS:
        result = kubectl_run(
            [
                "wait",
                "--for=condition=Complete",
                f"job/{job_name}",
                "-n",
                INGRESS_NGINX_NAMESPACE,
                f"--timeout={_INGRESS_NGINX_ADMISSION_JOB_WAIT_TIMEOUT_SECONDS}s",
            ],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=_INGRESS_NGINX_ADMISSION_JOB_WAIT_TIMEOUT_SECONDS + 30,
        )
        if result.get("exit_code", 1) != 0:
            detail = (result.get("stderr") or result.get("stdout") or "").strip()[:300]
            # "NotFound" is benign \u2014 the upstream manifest may have
            # been customised. A timeout on an existing Job is the real
            # signal; surface it so cert-manager challenge does not
            # silently fail later.
            if "NotFound" in detail or "not found" in detail.lower():
                LOGGER.info(
                    "public-https: ingress-nginx admission Job %s not present (skipped): %s",
                    job_name,
                    detail,
                )
                continue
            raise RuntimeError(
                f"ingress-nginx admission Job {job_name} did not Complete within "
                f"{_INGRESS_NGINX_ADMISSION_JOB_WAIT_TIMEOUT_SECONDS}s: {detail}"
            )


_ADMISSION_ENDPOINTS_PROBE_RETRIES = 20
_ADMISSION_ENDPOINTS_PROBE_INTERVAL_SECONDS = 3
INGRESS_NGINX_ADMISSION_SERVICE = "ingress-nginx-controller-admission"


def _wait_for_admission_endpoints_ready(*, kubeconfig_path: str) -> None:
    """Poll the admission Service until at least one endpoint address exists.

    ``kubectl wait --for=condition=Available`` on the controller
    Deployment returns the moment a controller Pod transitions to
    ``Ready``. The Service's EndpointSlice, however, is published by
    a SEPARATE kube-controller-manager controller after that
    transition \u2014 typical lag is a few hundred ms on a warm cluster,
    but on a cold cluster (new AKS systempool, slow EndpointSlice
    sync) it can stretch to several seconds. During that window any
    Ingress ``kubectl apply`` hits the apiserver's webhook call,
    which sees the Service has no endpoints, and the apiserver
    returns::

        Internal error occurred: failed calling webhook
        "validate.nginx.ingress.kubernetes.io": ... no endpoints
        available for service "ingress-nginx-controller-admission"

    (Reproduced verbatim on elb-cluster-small, 2026-05-27.)

    The probe polls ``kubectl get endpoints`` for the admission
    Service and exits as soon as the first ``subsets[].addresses[].ip``
    appears. Budget is generous (~60 s) but on a warm cluster the
    very first probe usually succeeds in <100 ms, so the wall-clock
    cost is negligible.
    """
    last_err = ""
    for attempt in range(1, _ADMISSION_ENDPOINTS_PROBE_RETRIES + 1):
        result = kubectl_run(
            [
                "get",
                "endpoints",
                INGRESS_NGINX_ADMISSION_SERVICE,
                "-n",
                INGRESS_NGINX_NAMESPACE,
                "-o",
                "jsonpath={.subsets[*].addresses[*].ip}",
            ],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=15,
        )
        if result.get("exit_code", 1) == 0:
            ips = (result.get("stdout") or "").strip()
            if ips:
                return
            last_err = "admission Service has no endpoint addresses yet"
        else:
            last_err = (result.get("stderr") or "").strip()[:200]
        if attempt < _ADMISSION_ENDPOINTS_PROBE_RETRIES:
            time.sleep(_ADMISSION_ENDPOINTS_PROBE_INTERVAL_SECONDS)
    raise RuntimeError(
        f"ingress-nginx admission Service {INGRESS_NGINX_ADMISSION_SERVICE} "
        f"never got endpoint addresses (probed {_ADMISSION_ENDPOINTS_PROBE_RETRIES} "
        f"times \u00d7 {_ADMISSION_ENDPOINTS_PROBE_INTERVAL_SECONDS}s); last: {last_err}"
    )


# Transient webhook failure strings the apiserver surfaces when the
# admission webhook is reachable in principle (Service + endpoints
# exist) but the call itself failed for a recoverable reason \u2014
# typically (a) EndpointSlice was published between our probe and the
# apply but the kube-proxy on the apiserver-side node has not yet
# synced (~milliseconds), (b) the controller Pod was restarted by the
# Kubelet between our wait and our apply, or (c) caBundle is in the
# middle of being patched. None of these warrant failing the whole
# pipeline; a short retry covers them.
_TRANSIENT_WEBHOOK_FAILURE_SUBSTRINGS: tuple[str, ...] = (
    "no endpoints available for service",
    "connection refused",
    "context deadline exceeded",
    "i/o timeout",
    "x509: certificate signed by unknown authority",
    "tls: failed to verify certificate",
    "EOF",
)
_INGRESS_APPLY_RETRY_ATTEMPTS = 6
_INGRESS_APPLY_RETRY_INTERVAL_SECONDS = 5


def _apply_ingress_with_webhook_retry(
    *,
    kubeconfig_path: str,
    ingress_yaml: str,
) -> None:
    """Apply the elb-openapi Ingress, retrying on transient webhook errors.

    Final safety net after the Deployment + Jobs + Endpoints waits.
    ``ValidatingWebhookConfiguration.failurePolicy`` defaults to
    ``Fail``, so any transient webhook call error is a hard apply
    failure \u2014 the whole public-https Enable click bounces. Short
    retries (~30 s total) on the known transient strings absorb the
    last-millisecond races without masking genuine misconfiguration
    (wrong CRD version, syntactically invalid Ingress, RBAC denial,
    etc.) because those produce different error strings and the
    function fails immediately.
    """
    last_detail = ""
    for attempt in range(1, _INGRESS_APPLY_RETRY_ATTEMPTS + 1):
        result = kubectl_run(
            ["apply", "-f", "-"],
            kubeconfig_path=kubeconfig_path,
            stdin=ingress_yaml,
            timeout_seconds=60,
        )
        if result.get("exit_code", 1) == 0:
            if attempt > 1:
                LOGGER.info(
                    "public-https: Ingress apply succeeded on attempt %d "
                    "after transient webhook failure",
                    attempt,
                )
            return
        detail = (result.get("stderr") or result.get("stdout") or "").strip()[:600]
        last_detail = detail
        if not any(s in detail for s in _TRANSIENT_WEBHOOK_FAILURE_SUBSTRINGS):
            # Non-transient \u2014 fail fast with the original error so the
            # operator does not wait ~30 s for a definitively broken
            # apply (wrong CRD version, RBAC denial, syntax error, ...).
            raise RuntimeError(f"kubectl apply Ingress failed: {detail}")
        LOGGER.info(
            "public-https: Ingress apply transient webhook failure on "
            "attempt %d/%d, retrying in %ds: %s",
            attempt,
            _INGRESS_APPLY_RETRY_ATTEMPTS,
            _INGRESS_APPLY_RETRY_INTERVAL_SECONDS,
            detail,
        )
        if attempt < _INGRESS_APPLY_RETRY_ATTEMPTS:
            time.sleep(_INGRESS_APPLY_RETRY_INTERVAL_SECONDS)
    raise RuntimeError(
        f"kubectl apply Ingress failed after {_INGRESS_APPLY_RETRY_ATTEMPTS} "
        f"transient-webhook retries: {last_detail}"
    )


def _wait_for_cert_manager_webhook(
    *, kubeconfig_path: str
) -> dict[str, Any]:
    """Wait for the cert-manager webhook Deployment to become Available.

    Previously a single ``kubectl wait --timeout=180s`` immediately after
    ``kubectl apply -f cert-manager`` raced the cainjector→controller→webhook
    creation order on cold clusters: when the apply returned, the webhook
    Deployment object did not yet exist, so the wait condition had no target
    and burned the full 180 s timeout before failing the pipeline. Observed
    in production once on a fresh elb-cluster-01 right before the cluster
    auto-stopped, recorded in App Insights as a `RuntimeError("public-https:
    pipeline failed")`.

    The hardened sequence is:

    1. Up to ``_CERT_MANAGER_WEBHOOK_PROBE_RETRIES`` short ``kubectl rollout
       status`` probes (60 s each), each followed by a
       ``_CERT_MANAGER_WEBHOOK_PROBE_INTERVAL_SECONDS`` sleep when the probe
       fails fast. ``kubectl rollout status`` returns a ``NotFound`` error
       **immediately** when the Deployment object has not yet been created
       (the ``--timeout`` flag only applies to waiting for an existing
       rollout to complete) — without the sleep, all retries would burn
       through in ~5 seconds and the pipeline would fail before cert-manager
       had any chance to create the webhook Deployment server-side. Total
       wait window with the sleep is ~5 × (≤60 s rollout + 15 s sleep) ≈
       5 min, matching the original "~300s" budget advertised in the
       fall-through error message.
    2. A single ``kubectl wait --for=condition=Available`` with a generous
       300 s timeout for the final readiness flip (serving-cert bootstrap +
       webhook TLS warmup).

    Returns the final ``kubectl wait`` result. Raises ``RuntimeError`` only
    when the webhook is still not Available after all retries — exactly the
    same failure shape callers already expected.
    """
    last_err = ""
    probe_started = time.time()
    success = False
    for attempt in range(1, _CERT_MANAGER_WEBHOOK_PROBE_RETRIES + 1):
        result = kubectl_run(
            [
                "rollout",
                "status",
                f"deployment/{CERT_MANAGER_WEBHOOK_DEPLOYMENT}",
                "-n",
                CERT_MANAGER_NAMESPACE,
                f"--timeout={_CERT_MANAGER_WEBHOOK_PROBE_TIMEOUT_SECONDS}s",
            ],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=_CERT_MANAGER_WEBHOOK_PROBE_TIMEOUT_SECONDS + 20,
        )
        if result.get("exit_code", 1) == 0:
            success = True
            break
        last_err = (
            (result.get("stderr") or result.get("stdout") or "").strip()[:300]
        )
        LOGGER.info(
            "public-https: cert-manager-webhook rollout probe %d/%d not ready yet: %s",
            attempt,
            _CERT_MANAGER_WEBHOOK_PROBE_RETRIES,
            last_err,
        )
        # Sleep BETWEEN attempts (not after the last) so a fast NotFound
        # response — which kubectl rollout status returns immediately when
        # the Deployment has not yet been created server-side — does not
        # burn through all retries in milliseconds. Without this sleep the
        # "5 × 60 s" advertised budget collapses to ~5 s of wall time and
        # the pipeline fails before cert-manager has had any chance to
        # create the webhook Deployment.
        if attempt < _CERT_MANAGER_WEBHOOK_PROBE_RETRIES:
            time.sleep(_CERT_MANAGER_WEBHOOK_PROBE_INTERVAL_SECONDS)
    if not success:
        elapsed = int(time.time() - probe_started)
        raise RuntimeError(
            "kubectl rollout status cert-manager-webhook failed after "
            f"{_CERT_MANAGER_WEBHOOK_PROBE_RETRIES} probes "
            f"({elapsed}s elapsed); last error: {last_err}"
        )

    # Final readiness flip — once rollout status has returned 0, this
    # normally completes in <5 s, but a cold cluster + AKS node scaling
    # can stretch it; keep the timeout generous.
    return _kubectl_or_raise(
        [
            "wait",
            "--for=condition=Available=true",
            f"deployment/{CERT_MANAGER_WEBHOOK_DEPLOYMENT}",
            "-n",
            CERT_MANAGER_NAMESPACE,
            f"--timeout={_CERT_MANAGER_WEBHOOK_AVAILABLE_TIMEOUT_SECONDS}s",
        ],
        kubeconfig_path=kubeconfig_path,
        timeout_seconds=_CERT_MANAGER_WEBHOOK_AVAILABLE_TIMEOUT_SECONDS + 30,
        context="wait cert-manager-webhook",
    )


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


_CERTIFICATE_EXISTS_PROBE_RETRIES = 12
_CERTIFICATE_EXISTS_PROBE_INTERVAL_SECONDS = 5


def _wait_for_certificate_object_to_exist(
    *, kubeconfig_path: str
) -> None:
    """Poll ``kubectl get certificate`` until the object is server-side present.

    cert-manager's ingress-shim controller creates the Certificate CR
    asynchronously after the Ingress is applied — typically <2 s on a
    warm cluster, but up to ~30 s on a cold cluster where ingress-shim
    is still scheduling. Without this pre-existence probe, the immediate
    ``kubectl wait --for=condition=Ready`` call returns ``NotFound``
    instantly (older kubectl) or burns its full timeout (newer kubectl
    that waits for resource creation), neither of which is the desired
    progress signal.

    Returns silently when the Certificate exists. Returns silently also
    after the retry budget is exhausted — we leave the subsequent
    ``kubectl wait`` to surface the eventual failure with its richer
    condition-message probe, so this helper never substitutes a less
    informative error for the real one.
    """
    for attempt in range(1, _CERTIFICATE_EXISTS_PROBE_RETRIES + 1):
        result = kubectl_run(
            [
                "get",
                "certificate",
                OPENAPI_TLS_SECRET_NAME,
                "-n",
                OPENAPI_NAMESPACE,
                "-o",
                "name",
            ],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=15,
        )
        if result.get("exit_code", 1) == 0 and (result.get("stdout") or "").strip():
            return
        LOGGER.debug(
            "public-https: certificate object not yet present (probe %d/%d)",
            attempt,
            _CERTIFICATE_EXISTS_PROBE_RETRIES,
        )
        if attempt < _CERTIFICATE_EXISTS_PROBE_RETRIES:
            time.sleep(_CERTIFICATE_EXISTS_PROBE_INTERVAL_SECONDS)


def _wait_for_certificate_ready(
    *,
    kubeconfig_path: str,
    timeout_seconds: int = 300,
) -> None:
    """Block until the openapi Certificate resource is Ready=True.

    The pre-existence probe absorbs the small ingress-shim delay before
    cert-manager has even created the Certificate CR — without it, on
    older kubectl builds where ``kubectl wait --for=condition`` returns
    immediately on ``NotFound``, this helper would fail in <1 s with a
    "certificate not found" error that the operator would mistake for a
    permanent issue rather than a normal cold-start race.
    """
    _wait_for_certificate_object_to_exist(kubeconfig_path=kubeconfig_path)
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

    email = ""
    try:
        email = _resolve_operator_email(operator_email)
    except ValueError as exc:
        LOGGER.warning("public-https: missing operator_email — task aborted")
        return {
            "status": "failed",
            "step": "resolve_operator_email",
            "error": str(exc),
        }
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
        # Step 1: install ingress-nginx with systempool tolerations.
        # The upstream manifest at INGRESS_NGINX_INSTALL_URL ships pods
        # with no tolerations, so on AKS clusters whose nodes carry the
        # `CriticalAddonsOnly=true:NoSchedule` (systempool) or
        # `workload=blast:NoSchedule` (blastpool) taints, every pod
        # lands in Pending forever — observed in production on
        # elb-cluster-01 (FailedScheduling: "0/3 nodes are available:
        # 3 node(s) had untolerated taint(s)"). The fetch-and-patch
        # helper injects the systempool toleration + nodeSelector into
        # every Deployment / DaemonSet / Job from the install manifest
        # so the add-on lands on the systempool only.
        record_progress(self, "install_ingress_nginx")
        ingress_nginx_manifest = fetch_install_manifest_for_system_pool(
            INGRESS_NGINX_INSTALL_URL
        )
        # ingress-nginx ships admission-webhook bootstrap Jobs whose
        # spec is immutable. A previous toleration-less install leaves
        # `Pending` Job pods behind that `kubectl apply -f -` cannot
        # reconcile in place, so we pre-delete them by their well-known
        # label before re-applying the patched manifest. The flag
        # `--ignore-not-found=true` keeps the step idempotent on the
        # first run.
        kubectl_run(
            [
                "delete",
                "job",
                "-n",
                INGRESS_NGINX_NAMESPACE,
                "-l",
                INGRESS_NGINX_ADMISSION_JOB_SELECTOR,
                "--ignore-not-found=true",
            ],
            kubeconfig_path=kubeconfig_path,
            timeout_seconds=60,
        )
        _kubectl_or_raise(
            ["apply", "-f", "-"],
            kubeconfig_path=kubeconfig_path,
            stdin=ingress_nginx_manifest,
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

        # Step 4: install cert-manager with the same systempool patch.
        # cert-manager ships Deployments only (no install-time Jobs),
        # so no pre-delete is needed — strategic merge patch on the
        # existing Deployments is enough to roll Pending pods.
        record_progress(self, "install_cert_manager")
        cert_manager_manifest = fetch_install_manifest_for_system_pool(
            CERT_MANAGER_INSTALL_URL
        )
        _kubectl_or_raise(
            ["apply", "-f", "-"],
            kubeconfig_path=kubeconfig_path,
            stdin=cert_manager_manifest,
            timeout_seconds=300,
            context="apply cert-manager",
        )

        # Step 5: wait for cert-manager webhook Ready. The dedicated helper
        # absorbs the cold-cluster race where ``kubectl wait`` would start
        # before the webhook Deployment object existed and burn its full
        # timeout for nothing.
        record_progress(self, "wait_cert_manager_webhook")
        _wait_for_cert_manager_webhook(kubeconfig_path=kubeconfig_path)

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
        # Certificate CR from the Ingress's tls block + annotation).
        #
        # Layered defense against the admission-webhook races that
        # repeatedly broke this pipeline in production (see
        # docs/features_change/2026-05/2026-05-27-public-https-*.md):
        #
        #   a. Deployment Available  \u2014 controller Pod is Ready.
        #   b. Admission Jobs Complete \u2014 caBundle is patched in
        #      ValidatingWebhookConfiguration; without this the
        #      apiserver fails the webhook call with
        #      "x509: certificate signed by unknown authority".
        #   c. Admission Service endpoints published \u2014 closes the
        #      EndpointSlice race between Pod-Ready and Service-has-
        #      endpoints, which is the literal "no endpoints
        #      available for service ingress-nginx-controller-
        #      admission" error operators were seeing.
        #   d. Apply with transient-error retry \u2014 last-millisecond
        #      races (kube-proxy sync lag, Pod restart by Kubelet
        #      between probe and apply) are absorbed by short retries
        #      instead of bouncing the whole Enable click.
        record_progress(self, "wait_ingress_nginx_controller")
        _wait_for_ingress_nginx_controller(kubeconfig_path=kubeconfig_path)

        record_progress(self, "wait_admission_jobs_complete")
        _wait_for_ingress_nginx_admission_jobs(kubeconfig_path=kubeconfig_path)

        record_progress(self, "wait_admission_endpoints_ready")
        _wait_for_admission_endpoints_ready(kubeconfig_path=kubeconfig_path)

        record_progress(self, "apply_ingress", fqdn=fqdn)
        _apply_ingress_with_webhook_retry(
            kubeconfig_path=kubeconfig_path,
            ingress_yaml=build_openapi_ingress(fqdn=fqdn),
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

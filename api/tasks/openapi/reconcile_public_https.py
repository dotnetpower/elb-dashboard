"""Periodic reconciler for the public HTTPS endpoint cache.

Module docstring (natural):
Solves the "SPA shows Not exposed after a Container App revision restart"
class of bug. The Redis sidecar that holds the runtime endpoint cache is
ephemeral; every new Container App revision starts with an empty cache,
and the SPA's Public HTTPS panel cannot tell the difference between
"never set up" and "set up but cache forgot". The setup task itself now
also writes to a durable Storage Table singleton, but the runtime cache
may still be cold for the first few seconds after a revision flip. This
beat task closes the gap by enumerating every per-cluster durable entry
and re-publishing the latest metadata (notAfter timestamp) into the
Redis hot cache.

Responsibility: Run as a Celery beat task. For every cluster recorded
    under the per-cluster public-base-url prefix, verify the Certificate
    Ready=True via kubectl and re-publish the latest metadata (LB IP,
    cert_expires_at, updated_at) into the per-cluster Redis hot cache.
    When the cluster has been torn down (kubeconfig auth fails) or the
    Certificate is no longer present, leave the durable singleton
    intact (the operator's explicit Disable click is the only legitimate
    way to drop it) but log the discrepancy.
Edit boundaries: Reconciler wiring only. Manifest construction lives
    in `api.services.k8s.ingress`; kubectl auth lives in
    `api.tasks.openapi.kubectl`; cache primitives live in
    `api.services.openapi.runtime`.
Key entry points: `reconcile_openapi_public_https`.
Risky contracts: Must never raise — a periodic task that crashes will
    spam the worker log every minute. Always return a small status dict
    summarising what happened. Task name
    `api.tasks.openapi.reconcile_public_https` is referenced by
    `api/celery_app.py::beat_schedule` and tests; do not rename. Loops
    over every per-cluster entry; per-cluster failures are logged and
    do not stop subsequent clusters from being reconciled.
Validation: `uv run pytest -q api/tests/test_openapi_public_https_reconcile.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from api.services.k8s.ingress import OPENAPI_NAMESPACE, OPENAPI_TLS_SECRET_NAME
from api.services.openapi.runtime import (
    list_openapi_public_base_urls,
    save_openapi_public_base_url,
)
from api.tasks.openapi.kubectl import ensure_admin_kubeconfig, kubectl_run
from api.tasks.openapi.public_https import read_certificate_expiry

LOGGER = logging.getLogger(__name__)


def _cert_ready(*, kubeconfig_path: str) -> bool:
    """Return True when the openapi Certificate has Ready=True."""
    result = kubectl_run(
        [
            "get",
            "certificate",
            OPENAPI_TLS_SECRET_NAME,
            "-n",
            OPENAPI_NAMESPACE,
            "-o",
            "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
        ],
        kubeconfig_path=kubeconfig_path,
        timeout_seconds=15,
    )
    if result.get("exit_code", 1) != 0:
        return False
    return (result.get("stdout") or "").strip() == "True"


def _reconcile_one(entry: dict[str, Any]) -> dict[str, Any]:
    """Reconcile a single per-cluster entry. Never raises."""
    base_url = (entry.get("base_url") or "").strip()
    metadata = entry.get("metadata") or {}
    subscription_id = (metadata.get("subscription_id") or "").strip()
    resource_group = (metadata.get("resource_group") or "").strip()
    cluster_name = (metadata.get("cluster_name") or "").strip()
    if not (base_url and subscription_id and resource_group and cluster_name):
        return {"status": "skipped", "reason": "incomplete_metadata"}

    cluster_arm_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ContainerService/managedClusters/{cluster_name}"
    )

    try:
        kubeconfig_path = ensure_admin_kubeconfig(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    except Exception as exc:
        LOGGER.info(
            "public-https reconcile: kubeconfig fetch failed for %s/%s: %s",
            resource_group,
            cluster_name,
            type(exc).__name__,
        )
        return {
            "status": "skipped",
            "cluster_name": cluster_name,
            "reason": "kubeconfig_unavailable",
        }

    if not _cert_ready(kubeconfig_path=kubeconfig_path):
        LOGGER.info(
            "public-https reconcile: cluster %s reachable but certificate %s not Ready; "
            "skipping cache write",
            cluster_name,
            OPENAPI_TLS_SECRET_NAME,
        )
        return {
            "status": "skipped",
            "cluster_name": cluster_name,
            "reason": "cert_not_ready",
        }

    cert_expires_at = read_certificate_expiry(kubeconfig_path=kubeconfig_path)
    new_metadata = dict(metadata)
    if cert_expires_at:
        new_metadata["cert_expires_at"] = cert_expires_at
    new_metadata["source"] = "reconcile_openapi_public_https"
    save_openapi_public_base_url(
        base_url,
        cluster_arm_id=cluster_arm_id,
        metadata=new_metadata,
    )
    return {
        "status": "reconciled",
        "cluster_name": cluster_name,
        "cert_expires_at": cert_expires_at,
    }


@shared_task(
    name="api.tasks.openapi.reconcile_public_https",
    bind=True,
    max_retries=0,
    ignore_result=True,
)
def reconcile_openapi_public_https(self: Any) -> dict[str, Any]:
    """Refresh every per-cluster public-https hot cache entry."""
    del self
    try:
        entries = list_openapi_public_base_urls()
    except Exception as exc:
        LOGGER.debug("public-https reconcile: list failed: %s", exc)
        return {"status": "skipped", "reason": "list_failed"}
    if not entries:
        return {"status": "skipped", "reason": "no_durable_state"}

    results: list[dict[str, Any]] = []
    for entry in entries:
        try:
            results.append(_reconcile_one(entry))
        except Exception as exc:  # pragma: no cover - defensive guard
            LOGGER.exception(
                "public-https reconcile: per-entry crash for %s",
                type(exc).__name__,
            )
            results.append({"status": "error", "error": type(exc).__name__})
    return {
        "status": "reconciled",
        "clusters_total": len(entries),
        "per_cluster": results,
    }


__all__ = ["reconcile_openapi_public_https"]

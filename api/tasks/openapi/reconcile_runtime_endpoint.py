"""Periodic re-stamp of the IP-based OpenAPI runtime endpoint durable cache.

Solves a deadlock in the Service Bus queue-drain readiness gate. The IP-based
runtime endpoint (``openapi:runtime:base-url``) is mirrored into the durable
``dashboardsingletons`` Storage Table, but a freshness TTL
(``OPENAPI_RUNTIME_ENDPOINT_MAX_AGE_SECONDS``, default 1 h) makes a cold read
return ``""`` once the durable row ages past the window. Nothing re-stamps that
row on a quiet deployment, so after a Container App revision restart (which wipes
the ephemeral Redis) plus an idle hour, ``external_blast._base_url`` resolves
nothing → ``external_blast.ready`` raises ``openapi_not_configured`` →
``_openapi_ready_for_drain`` returns False → the drain defers FOREVER, even
though the cluster is up. The only path that would refresh the endpoint is the
drain itself, which the gate blocks: a chicken-and-egg deadlock that previously
required pinning ``ELB_OPENAPI_BASE_URL`` by hand on the worker/api containers.

This beat task closes the gap by re-resolving the live ``elb-openapi`` Service IP
for the configured cluster and re-stamping the durable row (refreshing
``updated_at``) on every tick the cluster is reachable, so the durable copy
stays inside the freshness window and post-restart cold reads keep working with
no manual pin. When the cluster is Stopped the live IP does not resolve and the
row is left to age out correctly (the freshness gate must still reject a
long-Stopped cluster's unreachable IP).

Responsibility: Run as a Celery beat task. No-op unless ``SERVICEBUS_ENABLED``
    (the deadlock-prone consumer). Resolve the cluster context from the saved
    Service Bus config first, then the durable endpoint's own metadata; re-stamp
    the IP-based runtime endpoint when the live Service IP resolves.
Edit boundaries: Reconciler wiring only. Endpoint cache primitives live in
    ``api.services.openapi.runtime``; the live Service-IP lookup lives in
    ``api.services.k8s.monitoring``; the SB config lives in
    ``api.services.service_bus_pref``.
Key entry points: ``reconcile_openapi_runtime_endpoint``.
Risky contracts: Must never raise — a periodic task that crashes spams the
    worker log every tick. Always return a small status dict. Task name
    ``api.tasks.openapi.reconcile_runtime_endpoint`` is referenced by
    ``api/celery_app.py::beat_schedule`` and tests; do not rename. Only
    re-stamps on a real IP so a Stopped cluster's stale endpoint is never
    refreshed back to fresh.
Validation: ``uv run pytest -q api/tests/test_openapi_runtime_endpoint_reconcile.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

LOGGER = logging.getLogger(__name__)

_SERVICE_NAME = "elb-openapi"


def _resolve_cluster_context() -> tuple[str, str, str]:
    """Best-effort ``(subscription_id, resource_group, cluster_name)`` for the
    OpenAPI cluster.

    Prefers the saved Service Bus config's BLAST routing fields (the operator's
    explicit target); falls back to the durable runtime endpoint's own metadata
    (seeded by the last live resolution from the ``/api/blast/jobs`` listing or a
    prior submit) so the reconcile still works when the SB config row was saved
    without routing context. Returns empty strings when neither source carries a
    complete triple.
    """
    try:
        from api.services.service_bus_pref import get_service_bus_config

        cfg = get_service_bus_config()
        if cfg.subscription_id and cfg.resource_group and cfg.cluster_name:
            return (cfg.subscription_id, cfg.resource_group, cfg.cluster_name)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("runtime endpoint reconcile: SB config read failed: %s", type(exc).__name__)

    try:
        from api.services.openapi.runtime import get_openapi_runtime_metadata

        meta = get_openapi_runtime_metadata()
        sub = str(meta.get("subscription_id") or "")
        rg = str(meta.get("resource_group") or "")
        cluster = str(meta.get("cluster_name") or "")
        if sub and rg and cluster:
            return (sub, rg, cluster)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug(
            "runtime endpoint reconcile: durable metadata read failed: %s", type(exc).__name__
        )

    return ("", "", "")


@shared_task(
    name="api.tasks.openapi.reconcile_runtime_endpoint",
    bind=True,
    max_retries=0,
    ignore_result=True,
)
def reconcile_openapi_runtime_endpoint(self: Any) -> dict[str, Any]:
    """Re-stamp the IP-based OpenAPI runtime endpoint durable cache.

    Side effects (idempotent): one ``k8s_get_service_ip`` read and, on success,
    one ``save_openapi_base_url`` write (Redis hot cache + durable Table). Both
    are best-effort and the task never raises.
    """
    del self
    try:
        from api.services.service_bus_pref import service_bus_enabled

        if not service_bus_enabled():
            return {"status": "skipped", "reason": "servicebus_disabled"}
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("runtime endpoint reconcile: enabled check failed: %s", type(exc).__name__)
        return {"status": "skipped", "reason": "enabled_check_failed"}

    subscription_id, resource_group, cluster_name = _resolve_cluster_context()
    if not (subscription_id and resource_group and cluster_name):
        return {"status": "skipped", "reason": "no_cluster_context"}

    try:
        from api.services import get_credential
        from api.services.k8s.monitoring import k8s_get_service_ip

        ip = k8s_get_service_ip(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            _SERVICE_NAME,
        )
    except Exception as exc:
        LOGGER.debug("runtime endpoint reconcile: service IP lookup raised: %s", type(exc).__name__)
        return {"status": "skipped", "reason": "service_ip_error"}

    if not ip:
        # Cluster Stopped / Service not yet provisioned: leave the durable row to
        # age out so the freshness gate keeps rejecting an unreachable endpoint.
        return {"status": "skipped", "reason": "service_ip_unresolved"}

    try:
        from api.services.openapi.runtime import save_openapi_base_url

        save_openapi_base_url(
            f"http://{ip}",
            metadata={
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
                "service_name": _SERVICE_NAME,
                "source": "reconcile_openapi_runtime_endpoint",
            },
        )
    except Exception as exc:
        LOGGER.debug("runtime endpoint reconcile: durable write failed: %s", type(exc).__name__)
        return {"status": "skipped", "reason": "durable_write_failed"}

    return {"status": "reconciled", "cluster_name": cluster_name}


__all__ = ["reconcile_openapi_runtime_endpoint"]

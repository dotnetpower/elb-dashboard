"""Read-only Private Link Service (PLS) annotation state for ``elb-openapi``.

Responsibility: Surface "does the live Kubernetes Service carry the
``service.beta.kubernetes.io/azure-pls-create=true`` annotation set?"
to the dashboard SPA so operators see PLS transition state without
re-reading the Service manifest by hand. The Service / LoadBalancer state
itself stays the source of truth — this module never mutates it.
Edit boundaries: Service-layer only. No FastAPI / Celery here. Routes call
``get_pls_status``; the deploy task in ``api.tasks.openapi.deploy`` mutates
the Service via ``_read_service_annotations`` + ``_delete_openapi_service``
on its own path.
Key entry points: ``PlsStatus`` (return type), ``get_pls_status``.
Risky contracts: Reads the live Service via the in-cluster K8s API session
opened by ``api.services.k8s.monitoring._get_k8s_session``. The session is
closed even on error. Returns a degraded payload with
``available=False, reason=<short_code>`` on any failure rather than raising
so a routes handler can render the card without crashing.
Validation: ``uv run pytest -q api/tests/test_openapi_pls_status.py``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

# Imported at module scope so tests can monkeypatch
# ``api.services.openapi.pls_status.pls_config_from_env`` directly
# instead of having to reach into ``api.tasks.openapi.constants``.
from api.tasks.openapi.constants import pls_config_from_env

LOGGER = logging.getLogger(__name__)

_PLS_CREATE_ANNOTATION = "service.beta.kubernetes.io/azure-pls-create"


@dataclass(frozen=True)
class PlsStatus:
    """Read-only PLS state surfaced to the SPA.

    ``available`` is False whenever the live state could not be probed (RBAC
    failure, K8s API unreachable, deploy never ran). The SPA renders the same
    cell as "unknown" instead of treating absence as "no PLS configured".
    """

    available: bool
    pls_enabled_env: bool
    pls_name: str
    service_exists: bool | None
    service_has_pls_annotation: bool | None
    transition_pending: bool
    confirm_recreate_required: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_pls_status(
    cred: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str = "default",
    service_name: str = "elb-openapi",
) -> PlsStatus:
    """Return the live PLS annotation state for the ``elb-openapi`` Service.

    ``transition_pending`` is True when:
      * the operator has enabled PLS via the ``OPENAPI_PLS_ENABLED=1`` env
        knob (so the next deploy WILL inject the PLS annotation set), and
      * the Service exists but is missing ``azure-pls-create``.

    In that state ``confirm_recreate_required`` is also True — the deploy
    task refuses to re-apply without ``OPENAPI_PLS_CONFIRM_RECREATE=1``
    because the AKS LB controller silently ignores in-place PLS annotation
    updates. The SPA needs to surface this so operators don't keep clicking
    "Deploy" forever waiting for the PLS to appear.
    """
    try:
        cfg = pls_config_from_env()
    except ValueError as exc:
        LOGGER.warning("openapi pls status: invalid env config: %s", exc)
        # An invalid env config means the operator typed something the
        # parser refused; we have NO confirmed signal that PLS is
        # actually enabled. Reporting ``pls_enabled_env=True`` here
        # would mislead the SPA into showing "PLS is on" with an empty
        # name. Report ``False`` and surface ``reason="pls_env_invalid"``
        # as the dominant state instead.
        return PlsStatus(
            available=True,
            pls_enabled_env=False,
            pls_name="",
            service_exists=None,
            service_has_pls_annotation=None,
            transition_pending=False,
            confirm_recreate_required=False,
            reason="pls_env_invalid",
        )

    try:
        from api.services.k8s.monitoring import _get_k8s_session
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("openapi pls status: k8s session import failed: %s", exc)
        return PlsStatus(
            available=False,
            pls_enabled_env=cfg.enabled,
            pls_name=cfg.name,
            service_exists=None,
            service_has_pls_annotation=None,
            transition_pending=False,
            confirm_recreate_required=False,
            reason="k8s_client_unavailable",
        )

    session = None
    try:
        session, server = _get_k8s_session(
            cred, subscription_id, resource_group, cluster_name
        )
        response = session.get(
            f"{server}/api/v1/namespaces/{namespace}/services/{service_name}",
            timeout=10,
        )
        if response.status_code == 404:
            return PlsStatus(
                available=True,
                pls_enabled_env=cfg.enabled,
                pls_name=cfg.name,
                service_exists=False,
                service_has_pls_annotation=False,
                # Cannot transition something that doesn't exist; the next
                # deploy will create the Service with the annotation set
                # (if PLS is enabled in env).
                transition_pending=False,
                confirm_recreate_required=False,
            )
        if response.status_code != 200:
            LOGGER.warning(
                "openapi pls status: unexpected status=%s", response.status_code
            )
            return PlsStatus(
                available=False,
                pls_enabled_env=cfg.enabled,
                pls_name=cfg.name,
                service_exists=None,
                service_has_pls_annotation=None,
                transition_pending=False,
                confirm_recreate_required=False,
                reason="k8s_unexpected_status",
            )
        body = response.json() or {}
        metadata = body.get("metadata") or {}
        annotations = metadata.get("annotations") or {}
        has_pls = (
            str(annotations.get(_PLS_CREATE_ANNOTATION, "")).strip().lower() == "true"
        )
        transition_pending = cfg.enabled and not has_pls
        return PlsStatus(
            available=True,
            pls_enabled_env=cfg.enabled,
            pls_name=cfg.name,
            service_exists=True,
            service_has_pls_annotation=has_pls,
            transition_pending=transition_pending,
            confirm_recreate_required=transition_pending,
        )
    except Exception as exc:
        LOGGER.warning(
            "openapi pls status: probe failed: %s", type(exc).__name__
        )
        return PlsStatus(
            available=False,
            pls_enabled_env=cfg.enabled,
            pls_name=cfg.name,
            service_exists=None,
            service_has_pls_annotation=None,
            transition_pending=False,
            confirm_recreate_required=False,
            reason="k8s_probe_failed",
        )
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as close_exc:  # pragma: no cover - defensive
                LOGGER.debug("pls_status session close ignored: %s", close_exc)


__all__ = ("PlsStatus", "get_pls_status")

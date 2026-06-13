"""Classify the ``elb-openapi`` pod startup state for the API Reference page.

Responsibility: Tell whether a not-yet-serving ``elb-openapi`` Deployment is
merely *starting* (image cold-pull / readiness warm-up on a fresh node) versus
genuinely *failed* (crash-loop / image-pull error), so the spec route can avoid
showing a misleading "VNet peering broken" recovery affordance while the pod is
just booting.
Edit boundaries: Keep pod/Deployment inspection + classification here. Routes
only translate this into an HTTP payload; do not move HTTP shaping in here and
do not shell out / use Azure Run Command.
Key entry points: `classify_openapi_pod_state` (pure), `get_openapi_pod_startup_state` (I/O).
Risky contracts: The returned ``state`` strings (``ready`` / ``starting`` /
``failed`` / ``absent`` / ``unknown``) are consumed by the spec route to pick a
``degraded_reason``; keep them stable. Always degrade to ``unknown`` on any
Kubernetes read error so the caller can fall back to its existing behaviour.
Validation: `uv run pytest -q api/tests/test_openapi_pod_phase.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.monitoring import (
    _get_k8s_session,
    k8s_get_deployment_ready_replicas,
)

LOGGER = logging.getLogger(__name__)

OPENAPI_DEPLOYMENT_NAME = "elb-openapi"
OPENAPI_LABEL_SELECTOR = "app=elb-openapi"
K8S_NAMESPACE = "default"

# Container ``waiting.reason`` values that mean the pod is still coming up. These
# resolve on their own (image pull finishing, sandbox/network setup, readiness
# probe warming) and must NOT be surfaced as an error.
_STARTING_REASONS = frozenset(
    {
        "ContainerCreating",
        "PodInitializing",
        "Pending",
    }
)

# Container ``waiting.reason`` values that mean the pod will not become Ready
# without operator action. These are real failures (but still NOT a VNet
# peering problem, so the peering-repair affordance is wrong for them too).
_FAILED_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "ErrImageNeverPull",
        "InvalidImageName",
        "ImageInspectError",
        "RegistryUnavailable",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
    }
)


def _pod_signal(pod: dict[str, Any]) -> tuple[str, str]:
    """Return ``(category, reason)`` for one pod.

    ``category`` is one of ``ready`` / ``failed`` / ``starting`` / ``unknown``.
    A waiting ``reason`` wins over the coarse phase because it carries the
    actionable detail (``CrashLoopBackOff`` vs ``ContainerCreating``).
    """

    status = pod.get("status") or {}
    phase = str(status.get("phase") or "").strip()
    container_statuses = status.get("containerStatuses") or []

    # Any failing container reason short-circuits to "failed".
    for cs in container_statuses:
        waiting = (cs.get("state") or {}).get("waiting") or {}
        reason = str(waiting.get("reason") or "").strip()
        if reason in _FAILED_REASONS:
            return ("failed", reason)

    # All containers Ready -> ready (defensive; the caller normally short
    # circuits on Deployment.readyReplicas before inspecting pods).
    if container_statuses and all(bool(cs.get("ready")) for cs in container_statuses):
        return ("ready", "")

    # A starting container reason (image pull / sandbox / init).
    for cs in container_statuses:
        waiting = (cs.get("state") or {}).get("waiting") or {}
        reason = str(waiting.get("reason") or "").strip()
        if reason in _STARTING_REASONS:
            return ("starting", reason)

    # No container status yet, or running-but-not-ready (readiness probe still
    # warming) — both are benign startup states.
    if phase in ("Pending", "Running", "") or not container_statuses:
        return ("starting", phase or "Pending")

    return ("unknown", phase)


def classify_openapi_pod_state(
    pods: list[dict[str, Any]],
    *,
    ready_replicas: int,
    desired_replicas: int,
) -> tuple[str, str, str]:
    """Return ``(state, reason, message)`` for the ``elb-openapi`` rollout.

    Pure function (no I/O) so it is cheap to unit-test. ``state`` is one of
    ``ready`` / ``starting`` / ``failed`` / ``absent`` / ``unknown``.
    """

    if ready_replicas >= 1:
        return ("ready", "", "elb-openapi has a Ready replica.")

    if not pods:
        if desired_replicas <= 0:
            return (
                "absent",
                "",
                "No elb-openapi pod is scheduled.",
            )
        # Deployment desires a replica but no pod object is visible yet —
        # the ReplicaSet is still creating it. Treat as starting.
        return (
            "starting",
            "Pending",
            "The elb-openapi pod is being scheduled.",
        )

    categories: list[tuple[str, str]] = [_pod_signal(pod) for pod in pods]

    # Priority: a Ready pod (rare in this path) > a hard failure > starting.
    for category, _reason in categories:
        if category == "ready":
            return ("ready", "", "elb-openapi has a Ready replica.")
    for category, reason in categories:
        if category == "failed":
            return (
                "failed",
                reason,
                f"The elb-openapi pod is not ready ({reason}). Check the pod logs.",
            )
    for category, reason in categories:
        if category == "starting":
            human = (
                "The elb-openapi pod is starting"
                + (f" ({reason})" if reason and reason != "Pending" else "")
                + ". This usually finishes within ~2 minutes on a fresh node "
                "while the container image is pulled."
            )
            return ("starting", reason, human)

    return ("unknown", "", "elb-openapi pod state could not be determined.")


def _list_openapi_pods(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
) -> list[dict[str, Any]]:
    """Return raw pod objects for the ``elb-openapi`` Deployment.

    Scoped by the ``app=elb-openapi`` label selector so the call stays cheap on
    a busy cluster. Returns ``[]`` on any read error — the caller treats an
    empty list as "could not inspect" and degrades to its existing behaviour.
    """

    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name
    )
    try:
        url = (
            f"{server}/api/v1/namespaces/{namespace}/pods"
            f"?labelSelector={OPENAPI_LABEL_SELECTOR}"
        )
        response = session.get(url, timeout=10)
        if response.status_code != 200:
            return []
        items = response.json().get("items", [])
        return [item for item in items if isinstance(item, dict)]
    except Exception as exc:
        LOGGER.warning("openapi pod-phase: pod list failed: %s", exc)
        return []
    finally:
        session.close()


def get_openapi_pod_startup_state(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str = K8S_NAMESPACE,
) -> dict[str, Any]:
    """Probe the live ``elb-openapi`` rollout and classify its startup state.

    Returns a dict with ``state`` / ``reason`` / ``message`` /
    ``ready_replicas`` / ``desired_replicas``. Never raises — a Kubernetes read
    failure yields ``state == "unknown"`` so the caller can keep its existing
    (peering-repair) fallback for genuinely unreachable endpoints.
    """

    try:
        ready, desired = k8s_get_deployment_ready_replicas(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            OPENAPI_DEPLOYMENT_NAME,
            namespace,
        )
    except Exception as exc:
        LOGGER.warning("openapi pod-phase: ready-replica probe failed: %s", exc)
        return {
            "state": "unknown",
            "reason": "",
            "message": "elb-openapi pod state could not be determined.",
            "ready_replicas": 0,
            "desired_replicas": 0,
        }

    pods: list[dict[str, Any]] = []
    if ready < 1:
        pods = _list_openapi_pods(
            credential, subscription_id, resource_group, cluster_name, namespace
        )

    state, reason, message = classify_openapi_pod_state(
        pods, ready_replicas=ready, desired_replicas=desired
    )
    return {
        "state": state,
        "reason": reason,
        "message": message,
        "ready_replicas": ready,
        "desired_replicas": desired,
    }

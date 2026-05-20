"""Kubernetes pod log and event observability helpers.

Responsibility: Kubernetes pod log and event observability helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `k8s_pod_logs`, `k8s_list_events`, `_capped`
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py`.
"""

from __future__ import annotations

import re
from typing import Any, cast

from azure.core.credentials import TokenCredential

_SAFE_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def k8s_pod_logs(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    pod_name: str,
    tail_lines: int = 200,
) -> str:
    """Return pod logs via the Kubernetes API."""

    if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(pod_name):
        raise ValueError("Invalid namespace or pod name")

    from api.services.k8s_monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(
            f"{server}/api/v1/namespaces/{namespace}/pods/{pod_name}/log",
            params={"tailLines": tail_lines},
            timeout=15,
        )
        response.raise_for_status()
        return cast(str, response.text)
    finally:
        session.close()


def k8s_list_events(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Return recent k8s events sorted newest-first."""

    if namespace is not None and not _SAFE_K8S_NAME_RE.match(namespace):
        raise ValueError("Invalid namespace")
    if limit <= 0 or limit > 1000:
        raise ValueError("limit must be in (0, 1000]")

    from api.services.k8s_monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        if namespace:
            url = f"{server}/api/v1/namespaces/{namespace}/events"
        else:
            url = f"{server}/api/v1/events"
        params = {"limit": min(500, max(limit * 4, 100))}
        response = session.get(url, params=params, timeout=10)
        response.raise_for_status()
        items = response.json().get("items", []) or []
    finally:
        session.close()

    out: list[dict[str, Any]] = []
    for event in items:
        if not isinstance(event, dict):
            continue
        meta = event.get("metadata", {}) if isinstance(event.get("metadata"), dict) else {}
        involved = (
            event.get("involvedObject", {})
            if isinstance(event.get("involvedObject"), dict)
            else {}
        )
        source = event.get("source", {}) if isinstance(event.get("source"), dict) else {}
        last_ts = (
            event.get("lastTimestamp")
            or event.get("eventTime")
            or meta.get("creationTimestamp")
            or ""
        )
        try:
            count_val = max(1, min(int(float(event.get("count") or 1)), 1_000_000))
        except (TypeError, ValueError):
            count_val = 1
        event_type = str(event.get("type") or "Normal")
        if event_type not in ("Normal", "Warning"):
            event_type = "Normal"
        out.append(
            {
                "namespace": _capped(meta.get("namespace") or involved.get("namespace"), 63),
                "name": _capped(meta.get("name"), 253),
                "type": event_type,
                "reason": _capped(event.get("reason"), 64),
                "message": _capped(event.get("message"), 1024),
                "count": count_val,
                "last_timestamp": _capped(last_ts, 32),
                "involved_kind": _capped(involved.get("kind"), 64),
                "involved_name": _capped(involved.get("name"), 253),
                "source_component": _capped(source.get("component"), 64),
                "source_host": _capped(source.get("host"), 253),
            }
        )

    out.sort(key=lambda event: event.get("last_timestamp") or "", reverse=True)
    return out[:limit]


def _capped(value: Any, limit: int) -> str:
    text = str(value or "")
    return text[:limit]


__all__ = ["k8s_list_events", "k8s_pod_logs"]

"""Kubernetes pod log and event observability helpers.

Responsibility: Kubernetes pod log and event observability helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `k8s_pod_logs`, `k8s_pod_describe`, `k8s_list_events`, `_capped`
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py api/tests/test_k8s_pod_describe.py`.
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

    from api.services.k8s.monitoring import _get_k8s_session

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

    from api.services.k8s.monitoring import _get_k8s_session

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


def k8s_pod_describe(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    pod_name: str,
) -> str:
    """Return a `kubectl describe pod`-style text block for a single pod.

    Fetches the pod manifest plus its recent events (filtered server-side by
    `involvedObject.name`) and formats them as a compact human-readable
    block. The output is sanitized and capped to avoid runaway sizes — labels
    and annotations are line-limited so a noisy controller cannot blow up the
    response.
    """

    if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(pod_name):
        raise ValueError("Invalid namespace or pod name")

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        pod_resp = session.get(
            f"{server}/api/v1/namespaces/{namespace}/pods/{pod_name}",
            timeout=10,
        )
        pod_resp.raise_for_status()
        pod = pod_resp.json() if isinstance(pod_resp.json(), dict) else {}

        events: list[dict[str, Any]] = []
        try:
            ev_resp = session.get(
                f"{server}/api/v1/namespaces/{namespace}/events",
                params={"fieldSelector": f"involvedObject.name={pod_name}", "limit": 50},
                timeout=10,
            )
            ev_resp.raise_for_status()
            items = ev_resp.json().get("items", []) or []
            if isinstance(items, list):
                events = [e for e in items if isinstance(e, dict)]
        except Exception:
            events = []
    finally:
        session.close()

    return _format_pod_describe(pod, events)


def _format_pod_describe(pod: dict[str, Any], events: list[dict[str, Any]]) -> str:
    meta = pod.get("metadata", {}) if isinstance(pod.get("metadata"), dict) else {}
    spec = pod.get("spec", {}) if isinstance(pod.get("spec"), dict) else {}
    status = pod.get("status", {}) if isinstance(pod.get("status"), dict) else {}

    lines: list[str] = []

    def add(label: str, value: Any) -> None:
        # Width 18 leaves at least 2 spaces between the longest label
        # ("Service Account:" = 16 chars) and the value.
        lines.append(f"{label:<18}{value}")

    add("Name:", _capped(meta.get("name"), 253))
    add("Namespace:", _capped(meta.get("namespace"), 63))
    add("Node:", _capped(spec.get("nodeName") or "<none>", 253))
    add("Status:", _capped(status.get("phase") or "Unknown", 32))
    add("IP:", _capped(status.get("podIP") or "<none>", 64))
    add("Host IP:", _capped(status.get("hostIP") or "<none>", 64))
    add("Service Account:", _capped(spec.get("serviceAccountName") or "default", 253))
    add("Created:", _capped(meta.get("creationTimestamp"), 32))
    add("Start Time:", _capped(status.get("startTime") or "<unknown>", 32))
    add("Restart Policy:", _capped(spec.get("restartPolicy") or "Always", 32))

    labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
    lines.append("Labels:")
    for k, v in list(labels.items())[:25]:
        lines.append(f"  {_capped(k, 128)}={_capped(v, 256)}")
    if not labels:
        lines.append("  <none>")

    annotations = meta.get("annotations") if isinstance(meta.get("annotations"), dict) else {}
    lines.append("Annotations:")
    for k, v in list(annotations.items())[:25]:
        # Annotations can be large; cap aggressively for readability.
        lines.append(f"  {_capped(k, 128)}={_capped(v, 256)}")
    if not annotations:
        lines.append("  <none>")

    owners = meta.get("ownerReferences") if isinstance(meta.get("ownerReferences"), list) else []
    if owners:
        lines.append("Controlled By:")
        for owner in owners[:5]:
            if not isinstance(owner, dict):
                continue
            lines.append(
                f"  {_capped(owner.get('kind'), 64)}/{_capped(owner.get('name'), 253)}"
            )

    container_statuses_raw = status.get("containerStatuses")
    container_statuses: dict[str, dict[str, Any]] = {}
    if isinstance(container_statuses_raw, list):
        for cs in container_statuses_raw:
            if isinstance(cs, dict) and cs.get("name"):
                container_statuses[str(cs["name"])] = cs

    spec_containers = spec.get("containers") if isinstance(spec.get("containers"), list) else []
    lines.append("Containers:")
    if not spec_containers:
        lines.append("  <none>")
    for c in spec_containers:
        if not isinstance(c, dict):
            continue
        cname = _capped(c.get("name"), 64)
        cs = container_statuses.get(cname, {})
        lines.append(f"  {cname}:")
        lines.append(f"    Image:          {_capped(c.get('image'), 512)}")
        if cs.get("imageID"):
            lines.append(f"    Image ID:       {_capped(cs.get('imageID'), 512)}")
        ports = c.get("ports") if isinstance(c.get("ports"), list) else []
        if ports:
            lines.append(
                "    Ports:          "
                + ", ".join(
                    f"{p.get('containerPort')}/{p.get('protocol', 'TCP')}"
                    for p in ports
                    if isinstance(p, dict) and p.get("containerPort") is not None
                )
            )
        lines.append(f"    Ready:          {bool(cs.get('ready'))}")
        lines.append(f"    Restart Count:  {int(cs.get('restartCount') or 0)}")
        for state_key in ("state", "lastState"):
            st = cs.get(state_key) if isinstance(cs.get(state_key), dict) else {}
            if not st:
                continue
            for kind in ("running", "waiting", "terminated"):
                detail = st.get(kind) if isinstance(st.get(kind), dict) else None
                if detail is None:
                    continue
                title = "State:         " if state_key == "state" else "Last State:    "
                if kind == "running":
                    lines.append(
                        f"    {title} Running (started {_capped(detail.get('startedAt'), 32)})"
                    )
                elif kind == "waiting":
                    lines.append(
                        f"    {title} Waiting ({_capped(detail.get('reason') or 'Unknown', 64)})"
                    )
                    if detail.get("message"):
                        lines.append(f"      Message: {_capped(detail.get('message'), 512)}")
                else:  # terminated
                    lines.append(
                        f"    {title} Terminated (exit {detail.get('exitCode')}, "
                        f"{_capped(detail.get('reason') or 'Unknown', 64)})"
                    )
                    if detail.get("message"):
                        lines.append(f"      Message: {_capped(detail.get('message'), 512)}")
        resources = c.get("resources") if isinstance(c.get("resources"), dict) else {}
        if resources:
            req = resources.get("requests") if isinstance(resources.get("requests"), dict) else {}
            lim = resources.get("limits") if isinstance(resources.get("limits"), dict) else {}
            if req:
                lines.append(
                    "    Requests:       "
                    + ", ".join(f"{k}={_capped(v, 32)}" for k, v in list(req.items())[:5])
                )
            if lim:
                lines.append(
                    "    Limits:         "
                    + ", ".join(f"{k}={_capped(v, 32)}" for k, v in list(lim.items())[:5])
                )

    conditions = status.get("conditions") if isinstance(status.get("conditions"), list) else []
    if conditions:
        lines.append("Conditions:")
        lines.append(f"  {'Type':<22}Status")
        for cond in conditions[:10]:
            if not isinstance(cond, dict):
                continue
            lines.append(
                f"  {_capped(cond.get('type'), 22):<22}{_capped(cond.get('status'), 16)}"
            )

    lines.append("Events:")
    if not events:
        lines.append("  <none>")
    else:
        # Sort newest-first by lastTimestamp; tolerate missing fields.
        def _ev_ts(e: dict[str, Any]) -> str:
            return str(e.get("lastTimestamp") or e.get("eventTime") or "")

        events_sorted = sorted(events, key=_ev_ts, reverse=True)
        lines.append(f"  {'Type':<10}{'Reason':<22}{'Age':<10}{'Count':<8}Message")
        for ev in events_sorted[:25]:
            ev_type = _capped(ev.get("type"), 10)
            reason = _capped(ev.get("reason"), 22)
            age = _capped(_format_event_age(_ev_ts(ev)), 10)
            try:
                count = max(1, min(int(float(ev.get("count") or 1)), 1_000_000))
            except (TypeError, ValueError):
                count = 1
            msg = _capped(ev.get("message"), 256)
            lines.append(f"  {ev_type:<10}{reason:<22}{age:<10}{count:<8}{msg}")

    return "\n".join(lines)


def _format_event_age(iso: str) -> str:
    """Convert an ISO 8601 timestamp into a compact `kubectl get`-style age.

    Mirrors the SPA's `formatAge` so the Describe dialog and the Active
    Pods AGE column read the same way. Returns the original string if it
    cannot be parsed, so callers still see something useful.
    """

    if not iso:
        return "<unknown>"
    from datetime import UTC, datetime

    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - ts
    sec = max(0, int(delta.total_seconds()))
    if sec < 60:
        return f"{sec}s"
    minutes = sec // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        rem_min = minutes % 60
        return f"{hours}h{rem_min}m" if rem_min else f"{hours}h"
    days = hours // 24
    rem_hr = hours % 24
    return f"{days}d{rem_hr}h" if rem_hr else f"{days}d"


__all__ = ["k8s_list_events", "k8s_pod_describe", "k8s_pod_logs"]

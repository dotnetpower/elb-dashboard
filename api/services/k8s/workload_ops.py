"""Kubernetes Deployment/Job log, describe, and delete operations.

Responsibility: Provide the same Logs / Describe / Delete trio for Deployments
and Jobs that `observability.py` provides for pods, reusing its name/namespace
guards and event formatting helpers.
Edit boundaries: Keep reusable domain logic here; routes and tasks should call
this layer instead of duplicating SDK code. Pod-level helpers stay in
`observability.py`.
Key entry points: `k8s_deployment_logs`, `k8s_deployment_describe`,
`k8s_deployment_delete`, `k8s_job_logs`, `k8s_job_describe`, `k8s_job_delete`.
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure
Run Command. The delete helpers MUST refuse `SYSTEM_NAMESPACES` server-side —
frontend gating is not enough (OWASP A01).
Validation: `uv run pytest -q api/tests/test_k8s_workload_ops.py`.
"""

from __future__ import annotations

from typing import Any, cast

from azure.core.credentials import TokenCredential

from api.services.k8s.observability import (
    _SAFE_K8S_NAME_RE,
    SYSTEM_NAMESPACES,
    _capped,
    _format_event_age,
)


def _select_pod_for_logs(
    session: Any, server: str, namespace: str, label_selector: str
) -> str | None:
    """Return the name of the most relevant pod matching ``label_selector``.

    Prefers a ``Running`` pod, then falls back to the newest pod of any phase
    (mirroring how ``kubectl logs deploy/x`` / ``kubectl logs job/x`` pick a
    representative pod). Returns ``None`` when no pod matches.
    """

    response = session.get(
        f"{server}/api/v1/namespaces/{namespace}/pods",
        params={"labelSelector": label_selector},
        timeout=10,
    )
    response.raise_for_status()
    items = response.json().get("items", []) or []
    pods = [p for p in items if isinstance(p, dict)]
    if not pods:
        return None

    def _created(pod: dict[str, Any]) -> str:
        meta = pod.get("metadata", {}) if isinstance(pod.get("metadata"), dict) else {}
        return str(meta.get("creationTimestamp") or "")

    running = [
        p
        for p in pods
        if isinstance(p.get("status"), dict) and p["status"].get("phase") == "Running"
    ]
    pool = running or pods
    pool.sort(key=_created, reverse=True)
    name = pool[0].get("metadata", {}).get("name")
    return cast("str | None", name)


def _fetch_pod_log_via_session(
    session: Any, server: str, namespace: str, pod_name: str, tail_lines: int
) -> str:
    response = session.get(
        f"{server}/api/v1/namespaces/{namespace}/pods/{pod_name}/log",
        params={"tailLines": tail_lines},
        timeout=15,
    )
    response.raise_for_status()
    return cast(str, response.text)


def k8s_deployment_logs(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    deployment_name: str,
    tail_lines: int = 200,
) -> str:
    """Return logs from a representative pod of a Deployment.

    Resolves the Deployment's ``spec.selector.matchLabels`` to a label
    selector, picks a representative pod, and tails its logs. The output is
    prefixed with the chosen pod name because a Deployment can own many pods.
    """

    if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(deployment_name):
        raise ValueError("Invalid namespace or deployment name")

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        dep_resp = session.get(
            f"{server}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}",
            timeout=10,
        )
        dep_resp.raise_for_status()
        dep = dep_resp.json() if isinstance(dep_resp.json(), dict) else {}
        spec = dep.get("spec", {}) if isinstance(dep.get("spec"), dict) else {}
        selector = spec.get("selector", {}) if isinstance(spec.get("selector"), dict) else {}
        match_labels = (
            selector.get("matchLabels", {})
            if isinstance(selector.get("matchLabels"), dict)
            else {}
        )
        if not match_labels:
            return "(deployment has no pod selector)"
        label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
        pod_name = _select_pod_for_logs(session, server, namespace, label_selector)
        if not pod_name:
            return "(no pods found for this deployment)"
        body = _fetch_pod_log_via_session(session, server, namespace, pod_name, tail_lines)
        return f"# logs from pod {pod_name}\n{body}"
    finally:
        session.close()


def k8s_job_logs(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    job_name: str,
    tail_lines: int = 200,
) -> str:
    """Return logs from a representative pod of a Job.

    Job pods carry the ``job-name=<name>`` label by default; the newest /
    Running pod's logs are returned, prefixed with the chosen pod name.
    """

    if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(job_name):
        raise ValueError("Invalid namespace or job name")

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        pod_name = _select_pod_for_logs(session, server, namespace, f"job-name={job_name}")
        if not pod_name:
            return "(no pods found for this job)"
        body = _fetch_pod_log_via_session(session, server, namespace, pod_name, tail_lines)
        return f"# logs from pod {pod_name}\n{body}"
    finally:
        session.close()


def k8s_deployment_describe(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    deployment_name: str,
) -> str:
    """Return a `kubectl describe deployment`-style text block."""

    if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(deployment_name):
        raise ValueError("Invalid namespace or deployment name")
    obj, events = _fetch_workload_and_events(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        namespace,
        deployment_name,
        api_path=f"apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}",
    )
    return _format_workload_describe("Deployment", obj, events)


def k8s_job_describe(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    job_name: str,
) -> str:
    """Return a `kubectl describe job`-style text block."""

    if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(job_name):
        raise ValueError("Invalid namespace or job name")
    obj, events = _fetch_workload_and_events(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        namespace,
        job_name,
        api_path=f"apis/batch/v1/namespaces/{namespace}/jobs/{job_name}",
    )
    return _format_workload_describe("Job", obj, events)


def _fetch_workload_and_events(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    name: str,
    *,
    api_path: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        obj_resp = session.get(f"{server}/{api_path}", timeout=10)
        obj_resp.raise_for_status()
        obj = obj_resp.json() if isinstance(obj_resp.json(), dict) else {}

        events: list[dict[str, Any]] = []
        try:
            ev_resp = session.get(
                f"{server}/api/v1/namespaces/{namespace}/events",
                params={"fieldSelector": f"involvedObject.name={name}", "limit": 50},
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
    return obj, events


def _format_workload_describe(
    kind: str, obj: dict[str, Any], events: list[dict[str, Any]]
) -> str:
    meta = obj.get("metadata", {}) if isinstance(obj.get("metadata"), dict) else {}
    spec = obj.get("spec", {}) if isinstance(obj.get("spec"), dict) else {}
    status = obj.get("status", {}) if isinstance(obj.get("status"), dict) else {}

    lines: list[str] = []

    def add(label: str, value: Any) -> None:
        lines.append(f"{label:<20}{value}")

    add("Name:", _capped(meta.get("name"), 253))
    add("Namespace:", _capped(meta.get("namespace"), 63))
    add("Created:", _capped(meta.get("creationTimestamp"), 32))

    labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
    lines.append("Labels:")
    for k, v in list(labels.items())[:25]:
        lines.append(f"  {_capped(k, 128)}={_capped(v, 256)}")
    if not labels:
        lines.append("  <none>")

    selector = spec.get("selector", {}) if isinstance(spec.get("selector"), dict) else {}
    match_labels = (
        selector.get("matchLabels", {})
        if isinstance(selector.get("matchLabels"), dict)
        else {}
    )
    if match_labels:
        lines.append("Selector:")
        for k, v in list(match_labels.items())[:25]:
            lines.append(f"  {_capped(k, 128)}={_capped(v, 256)}")

    if kind == "Deployment":
        add(
            "Replicas:",
            f"{int(spec.get('replicas') or 0)} desired | "
            f"{int(status.get('updatedReplicas') or 0)} updated | "
            f"{int(status.get('readyReplicas') or 0)} ready | "
            f"{int(status.get('availableReplicas') or 0)} available | "
            f"{int(status.get('unavailableReplicas') or 0)} unavailable",
        )
        strategy = spec.get("strategy", {}) if isinstance(spec.get("strategy"), dict) else {}
        if strategy.get("type"):
            add("Strategy:", _capped(strategy.get("type"), 32))
    elif kind == "Job":
        add("Parallelism:", int(spec.get("parallelism") or 1))
        add("Completions:", int(spec.get("completions") or 1))
        add("Start Time:", _capped(status.get("startTime") or "<none>", 32))
        add("Completion Time:", _capped(status.get("completionTime") or "<none>", 32))
        add("Active:", int(status.get("active") or 0))
        add("Succeeded:", int(status.get("succeeded") or 0))
        add("Failed:", int(status.get("failed") or 0))

    conditions = status.get("conditions") if isinstance(status.get("conditions"), list) else []
    if conditions:
        lines.append("Conditions:")
        lines.append(f"  {'Type':<22}{'Status':<10}Reason")
        for cond in conditions[:10]:
            if not isinstance(cond, dict):
                continue
            lines.append(
                f"  {_capped(cond.get('type'), 22):<22}"
                f"{_capped(cond.get('status'), 10):<10}"
                f"{_capped(cond.get('reason'), 64)}"
            )

    lines.append("Events:")
    if not events:
        lines.append("  <none>")
    else:

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


def _workload_delete(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    name: str,
    *,
    kind: str,
    api_path: str,
    propagation_policy: str,
) -> dict[str, Any]:
    if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(name):
        raise ValueError("Invalid namespace or resource name")
    if namespace in SYSTEM_NAMESPACES:
        raise PermissionError(f"namespace {namespace!r} is system-managed; refusing to delete")

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.delete(
            f"{server}/{api_path}",
            params={"propagationPolicy": propagation_policy},
            timeout=15,
        )
    finally:
        session.close()

    if response.status_code in (200, 202):
        return {
            "status": "deleted",
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "status_code": response.status_code,
        }
    if response.status_code == 404:
        return {
            "status": "not_found",
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "status_code": 404,
        }
    return {
        "status": "error",
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "status_code": response.status_code,
        "detail": (getattr(response, "text", "") or "")[:512],
    }


def k8s_deployment_delete(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    deployment_name: str,
) -> dict[str, Any]:
    """Delete a Deployment (Foreground propagation so its pods are removed).

    Refuses any namespace in `SYSTEM_NAMESPACES`. 404 is treated as success
    so a double-click does not surface a scary error.
    """

    return _workload_delete(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        namespace,
        deployment_name,
        kind="Deployment",
        api_path=f"apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}",
        propagation_policy="Foreground",
    )


def k8s_job_delete(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    job_name: str,
) -> dict[str, Any]:
    """Delete a Job (Background propagation so its pods are GC'd).

    Refuses any namespace in `SYSTEM_NAMESPACES`. 404 is treated as success.
    """

    return _workload_delete(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        namespace,
        job_name,
        kind="Job",
        api_path=f"apis/batch/v1/namespaces/{namespace}/jobs/{job_name}",
        propagation_policy="Background",
    )


__all__ = [
    "k8s_deployment_delete",
    "k8s_deployment_describe",
    "k8s_deployment_logs",
    "k8s_job_delete",
    "k8s_job_describe",
    "k8s_job_logs",
]

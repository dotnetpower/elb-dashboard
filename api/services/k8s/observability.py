"""Kubernetes pod log and event observability helpers.

Responsibility: Kubernetes pod log and event observability helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `k8s_pod_logs`, `k8s_pod_describe`, `k8s_pod_delete`, `k8s_list_events`,
`fetch_pod_all_container_logs`, `compute_pod_display_status`, `_capped`, `SYSTEM_NAMESPACES`
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
`k8s_pod_delete` MUST refuse system namespaces server-side — frontend gating is not enough.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py api/tests/test_k8s_pod_describe.py
api/tests/test_k8s_pod_delete.py`.
"""

from __future__ import annotations

import re
from typing import Any, cast

from azure.core.credentials import TokenCredential

_SAFE_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# Cluster-managed namespaces that must never be touched from the dashboard.
# Server-side enforcement — the SPA also hides the Delete button for these,
# but treating frontend gating as authoritative would be an OWASP A01 fail.
SYSTEM_NAMESPACES: frozenset[str] = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "gatekeeper-system",
        "azure-arc",
        "calico-system",
        "tigera-operator",
    }
)


def _list_pod_container_names(pod_obj: dict[str, Any]) -> list[str]:
    """Return every container name (init + regular) declared on a pod object.

    Init containers are listed first because they run before the main
    containers, so showing their logs first mirrors the execution order.
    """

    spec = pod_obj.get("spec", {}) if isinstance(pod_obj.get("spec"), dict) else {}
    names: list[str] = []
    for key in ("initContainers", "containers"):
        for container in spec.get(key, []) or []:
            if isinstance(container, dict) and container.get("name"):
                names.append(str(container["name"]))
    return names


def _index_container_statuses(pod_obj: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map container name -> its status entry (init + regular merged)."""

    status = pod_obj.get("status", {}) if isinstance(pod_obj.get("status"), dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for key in ("initContainerStatuses", "containerStatuses"):
        for cs in status.get(key, []) or []:
            if isinstance(cs, dict) and cs.get("name"):
                out[str(cs["name"])] = cs
    return out


def _container_state_summary(cs: dict[str, Any]) -> tuple[str, str]:
    """Return ``(short_state, detail_message)`` for a container status entry.

    ``short_state`` is a compact label for the block header (e.g.
    ``Running``, ``Terminated exit 1 (Error)``, ``Waiting (CrashLoopBackOff)``).
    ``detail_message`` is the container's ``state.*.message`` (kubelet's reason
    text, often the only place the real crash cause is recorded) or "".
    """

    state = cs.get("state") if isinstance(cs.get("state"), dict) else {}
    running = state.get("running") if isinstance(state.get("running"), dict) else None
    waiting = state.get("waiting") if isinstance(state.get("waiting"), dict) else None
    terminated = state.get("terminated") if isinstance(state.get("terminated"), dict) else None
    if running is not None:
        return "Running", ""
    if waiting is not None:
        reason = str(waiting.get("reason") or "Waiting")
        return f"Waiting ({reason})", str(waiting.get("message") or "")
    if terminated is not None:
        reason = str(terminated.get("reason") or "")
        exit_code = terminated.get("exitCode")
        label = f"Terminated exit {exit_code}"
        if reason:
            label += f" ({reason})"
        return label, str(terminated.get("message") or "")
    return "Unknown", ""


def fetch_pod_all_container_logs(
    session: Any,
    server: str,
    namespace: str,
    pod_name: str,
    tail_lines: int,
) -> str:
    """Return the tail of logs for *every* container of a single pod.

    The Kubernetes pod log endpoint serves one container at a time and 400s
    for a multi-container pod when no ``container`` is given, so a container-
    less GET would hide every BLAST init/sidecar container's output. This
    reads the pod spec + status and for each container (init first):

    * fetches its current log,
    * when the log GET fails because the container is waiting
      (``CrashLoopBackOff`` / ``ImagePullBackOff`` / ``PodInitializing``),
      surfaces the kubelet waiting reason + message instead of a bare
      ``(log unavailable)`` — the error is otherwise invisible,
    * when the container has restarted (``restartCount > 0``) ALSO fetches the
      ``previous=true`` instance log, because a crashed container's *current*
      log is the fresh restart and the real failure output lives in the
      previous instance, and
    * prefixes each block with a ``--- container: <name> [<state>] ---``
      header so the operator can tell which container produced what and why it
      is unhealthy.

    A single, cleanly-running, single-container pod with output returns its
    body unchanged (no header) so the calm common case stays byte-for-byte
    compatible. The pod-spec read degrades to a container-less GET if it fails.
    """

    try:
        pod_resp = session.get(
            f"{server}/api/v1/namespaces/{namespace}/pods/{pod_name}",
            timeout=10,
        )
        pod_resp.raise_for_status()
        pod_obj = pod_resp.json() if isinstance(pod_resp.json(), dict) else {}
    except Exception:
        pod_obj = {}

    containers = _list_pod_container_names(pod_obj)
    statuses = _index_container_statuses(pod_obj)

    def _one(container: str | None, *, previous: bool = False) -> str:
        params: dict[str, Any] = {"tailLines": tail_lines}
        if container:
            params["container"] = container
        if previous:
            params["previous"] = "true"
        response = session.get(
            f"{server}/api/v1/namespaces/{namespace}/pods/{pod_name}/log",
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        return cast(str, response.text)

    # Fast path: a single, cleanly-running container with output keeps the
    # legacy raw-body shape so a healthy pod's log view is unchanged.
    if len(containers) <= 1:
        only = containers[0] if containers else None
        cs = statuses.get(only, {}) if only else {}
        restarts = int(cs.get("restartCount") or 0)
        short_state, _detail = _container_state_summary(cs) if cs else ("Unknown", "")
        clean = restarts == 0 and (not cs or short_state == "Running")
        try:
            body = _one(only)
        except Exception as exc:
            body = ""
            fetch_error = exc
        else:
            fetch_error = None
        if clean and fetch_error is None and body.strip():
            return body
        return _render_container_block(
            session,
            server,
            namespace,
            pod_name,
            tail_lines,
            only or "(default)",
            cs,
            body,
            fetch_error,
            _one,
        )

    blocks: list[str] = []
    for name in containers:
        cs = statuses.get(name, {})
        try:
            body = _one(name)
        except Exception as exc:
            body = ""
            fetch_error: Exception | None = exc
        else:
            fetch_error = None
        blocks.append(
            _render_container_block(
                session,
                server,
                namespace,
                pod_name,
                tail_lines,
                name,
                cs,
                body,
                fetch_error,
                _one,
            )
        )
    return "\n".join(blocks)


def _render_container_block(
    session: Any,
    server: str,
    namespace: str,
    pod_name: str,
    tail_lines: int,
    name: str,
    cs: dict[str, Any],
    body: str,
    fetch_error: Exception | None,
    fetch_log: Any,
) -> str:
    """Compose one container's log block, surfacing crash/previous output.

    ``fetch_log(container, previous=...)`` is the bound GET helper from the
    caller so the previous-instance read reuses the same session/params.
    """

    short_state, detail = _container_state_summary(cs) if cs else ("", "")
    restarts = int(cs.get("restartCount") or 0)
    header = f"--- container: {name}"
    if short_state and short_state != "Running":
        header += f" [{short_state}]"
    elif restarts:
        header += f" [restarts={restarts}]"
    header += " ---"

    lines: list[str] = [header]
    trimmed = body.rstrip()
    if trimmed:
        lines.append(trimmed)
    elif fetch_error is not None:
        # The log GET failed. When the container is waiting (CrashLoopBackOff,
        # ImagePullBackOff, PodInitializing) the kubelet reason/message is the
        # only diagnostic available — surface it instead of a bare error.
        if detail:
            lines.append(f"(no log; {short_state}: {detail})")
        elif short_state:
            lines.append(f"(no log; container state: {short_state})")
        else:
            lines.append(f"(log unavailable: {type(fetch_error).__name__})")
    else:
        # Empty body, no error: still show the state so an operator is not
        # left staring at a blank box for a crashed/terminated container.
        if detail:
            lines.append(f"(no output; {short_state}: {detail})")
        elif short_state and short_state != "Running":
            lines.append(f"(no output; container state: {short_state})")
        else:
            lines.append("(no output)")

    # A restarted container's current log is the fresh restart; the failure
    # output is in the previous instance. Fetch it so crashes are visible.
    if restarts:
        try:
            prev = fetch_log(name, previous=True).rstrip()
        except Exception:
            prev = ""
        if prev:
            lines.append(f"--- container: {name} (previous instance, restarts={restarts}) ---")
            lines.append(prev)
    return "\n".join(lines)


def k8s_pod_logs(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    pod_name: str,
    tail_lines: int = 200,
) -> str:
    """Return pod logs via the Kubernetes API for every container of the pod."""

    if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(pod_name):
        raise ValueError("Invalid namespace or pod name")

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        return fetch_pod_all_container_logs(session, server, namespace, pod_name, tail_lines)
    finally:
        session.close()


def k8s_pod_delete(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
    pod_name: str,
    *,
    grace_period_seconds: int = 30,
) -> dict[str, Any]:
    """Delete a single pod via the Kubernetes API.

    Refuses any namespace in `SYSTEM_NAMESPACES` — frontend hides the action
    for those, but a hand-crafted DELETE must also be rejected here.

    Returns a small status dict the route forwards to the SPA. 404 from the
    cluster is treated as success (pod already gone) so a double-click does
    not surface a scary error.
    """

    if not _SAFE_K8S_NAME_RE.match(namespace) or not _SAFE_K8S_NAME_RE.match(pod_name):
        raise ValueError("Invalid namespace or pod name")
    if namespace in SYSTEM_NAMESPACES:
        raise PermissionError(f"namespace {namespace!r} is system-managed; refusing to delete")
    if grace_period_seconds < 0 or grace_period_seconds > 600:
        raise ValueError("grace_period_seconds must be in [0, 600]")

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.delete(
            f"{server}/api/v1/namespaces/{namespace}/pods/{pod_name}",
            params={
                "gracePeriodSeconds": grace_period_seconds,
                "propagationPolicy": "Background",
            },
            timeout=15,
        )
    finally:
        session.close()

    if response.status_code in (200, 202):
        return {
            "status": "deleted",
            "namespace": namespace,
            "pod": pod_name,
            "status_code": response.status_code,
        }
    if response.status_code == 404:
        return {
            "status": "not_found",
            "namespace": namespace,
            "pod": pod_name,
            "status_code": 404,
        }
    return {
        "status": "error",
        "namespace": namespace,
        "pod": pod_name,
        "status_code": response.status_code,
        "detail": (response.text or "")[:512],
    }


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


def compute_pod_display_status(pod: dict[str, Any]) -> str:
    """Return the kubectl-style STATUS for a pod (container reason, not phase).

    ``status.phase`` alone hides the real problem: a CrashLoopBackOff pod still
    reports phase ``Running``, an ImagePullBackOff pod reports ``Pending``.
    This mirrors ``kubectl get pods`` / the Azure portal Pods column by
    surfacing the failing container's waiting/terminated reason (including init
    containers, prefixed ``Init:``) so an operator sees ``CrashLoopBackOff`` /
    ``Error`` / ``Init:Error`` / ``ImagePullBackOff`` instead of a misleading
    healthy-looking phase. Falls back to the phase when nothing is wrong.
    """

    status = pod.get("status", {}) if isinstance(pod.get("status"), dict) else {}
    meta = pod.get("metadata", {}) if isinstance(pod.get("metadata"), dict) else {}
    reason = str(status.get("reason") or status.get("phase") or "Unknown")

    def _state(cs: dict[str, Any], key: str) -> dict[str, Any]:
        state = cs.get("state") if isinstance(cs.get("state"), dict) else {}
        node = state.get(key) if isinstance(state.get(key), dict) else {}
        return node if isinstance(node, dict) else {}

    # Init containers run first; a failing one blocks the pod and is the real
    # status until it completes.
    init_statuses = status.get("initContainerStatuses")
    initializing = False
    if isinstance(init_statuses, list):
        for cs in init_statuses:
            if not isinstance(cs, dict):
                continue
            terminated = _state(cs, "terminated")
            waiting = _state(cs, "waiting")
            if terminated:
                if int(terminated.get("exitCode") or 0) == 0:
                    continue  # init container finished OK
                t_reason = str(terminated.get("reason") or "")
                if t_reason:
                    reason = f"Init:{t_reason}"
                elif terminated.get("signal"):
                    reason = f"Init:Signal:{terminated.get('signal')}"
                else:
                    reason = f"Init:ExitCode:{terminated.get('exitCode')}"
                initializing = True
                break
            w_reason = str(waiting.get("reason") or "")
            if w_reason and w_reason != "PodInitializing":
                reason = f"Init:{w_reason}"
                initializing = True
                break

    if not initializing:
        container_statuses = status.get("containerStatuses")
        if isinstance(container_statuses, list):
            # kubectl walks containers in reverse so the first listed container
            # wins ties; emulate that ordering.
            for cs in reversed(container_statuses):
                if not isinstance(cs, dict):
                    continue
                waiting = _state(cs, "waiting")
                terminated = _state(cs, "terminated")
                w_reason = str(waiting.get("reason") or "")
                t_reason = str(terminated.get("reason") or "")
                if w_reason:
                    reason = w_reason
                elif t_reason:
                    reason = t_reason
                elif terminated:
                    if terminated.get("signal"):
                        reason = f"Signal:{terminated.get('signal')}"
                    else:
                        reason = f"ExitCode:{terminated.get('exitCode')}"

    if meta.get("deletionTimestamp"):
        reason = "Terminating"
    return reason


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


def _append_container_describe(
    lines: list[str], c: dict[str, Any], cs: dict[str, Any]
) -> None:
    """Append one container's describe block (image, ready, restarts, state).

    Used for both init and regular containers so a failed init container's
    terminated reason/exitCode/message is rendered identically to a main
    container's. ``cs`` is the matching container status entry (possibly {}).
    """

    cname = _capped(c.get("name"), 64)
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

    init_statuses_raw = status.get("initContainerStatuses")
    init_statuses: dict[str, dict[str, Any]] = {}
    if isinstance(init_statuses_raw, list):
        for cs in init_statuses_raw:
            if isinstance(cs, dict) and cs.get("name"):
                init_statuses[str(cs["name"])] = cs

    # Init containers run before the main containers and are a common BLAST
    # failure point (DB download / staging). A failed init container's
    # terminated reason/exitCode/message must be visible here, otherwise the
    # describe view shows a healthy-looking pod with no clue why it is stuck.
    spec_init = spec.get("initContainers") if isinstance(spec.get("initContainers"), list) else []
    if spec_init:
        lines.append("Init Containers:")
        for c in spec_init:
            if not isinstance(c, dict):
                continue
            _append_container_describe(lines, c, init_statuses.get(_capped(c.get("name"), 64), {}))

    spec_containers = spec.get("containers") if isinstance(spec.get("containers"), list) else []
    lines.append("Containers:")
    if not spec_containers:
        lines.append("  <none>")
    for c in spec_containers:
        if not isinstance(c, dict):
            continue
        _append_container_describe(lines, c, container_statuses.get(_capped(c.get("name"), 64), {}))

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


__all__ = [
    "SYSTEM_NAMESPACES",
    "k8s_list_events",
    "k8s_pod_delete",
    "k8s_pod_describe",
    "k8s_pod_logs",
]

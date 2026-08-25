"""Kubernetes pod log discovery and follow helpers for BLAST jobs.

Responsibility: Kubernetes pod log discovery and follow helpers for BLAST jobs
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `K8sLogTarget`, `elastic_blast_suffix`, `resolve_elastic_blast_job_id`,
`elastic_blast_selector_from_error`, `discover_k8s_log_targets`,
`stream_k8s_log_lines`, `fetch_k8s_pod_log_tail`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.monitoring import _get_k8s_session
from api.services.sanitise import sanitise

_LINE_MAX_CHARS = 4_000
_SAFE_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_ELASTIC_BLAST_JOB_ID_RE = re.compile(r"^job-[0-9a-f]{32}$", re.IGNORECASE)
_ELASTIC_BLAST_SHORT_SELECTOR_RE = re.compile(r"^job-[0-9a-f]{8}$", re.IGNORECASE)
_INIT_SSD_FAILURE_RE = re.compile(r"\binit-ssd-([0-9a-f]{8})-[0-9]{1,3}\b", re.IGNORECASE)


@dataclass(frozen=True)
class K8sLogTarget:
    namespace: str
    pod_name: str
    container_name: str
    phase: str
    source: str = "k8s"

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.pod_name}/{self.container_name}"


def elastic_blast_suffix(value: str) -> str:
    """Return the short suffix used by ElasticBLAST job/pod names."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    compact = raw.rsplit("/", 1)[-1]
    compact = compact.removeprefix("job-")
    return compact[-8:] if len(compact) >= 8 else compact


def elastic_blast_selector_from_error(error_text: str) -> str:
    """Return a short ``job-<8hex>`` selector from an init-ssd failure name.

    Submit-time shard initialization can fail before the sibling persists the
    full ElasticBLAST runtime id. Its bounded error still names the failed Job
    as ``init-ssd-<runtime-suffix>-<ordinal>``; the suffix is enough to match
    the pod owner name and the full ``elb-job-id`` label suffix before TTL GC.
    """

    match = _INIT_SSD_FAILURE_RE.search(str(error_text or ""))
    return f"job-{match.group(1).lower()}" if match else ""


def resolve_elastic_blast_job_id(
    payload: dict[str, Any] | None,
    *,
    persisted_job_id: str = "",
    error_text: str = "",
) -> str:
    """Return the ``job-<hash>`` id ElasticBLAST stamped on its k8s objects.

    The dashboard stores this in a few places depending on which code path
    last touched the row: top-level ``payload.elastic_blast_job_id`` /
    ``payload.k8s_job_id`` (set by the submit task), and the nested
    ``payload._progress.steps.running.k8s.job_id`` / ``payload.external.k8s.job_id``
    (set by background k8s status refresh). Live log discovery only worked
    when the top-level field was filled, so jobs whose row was last touched
    by the refresh path looked like they had no k8s pods. This helper walks
    all known sites and returns the first ``job-…`` value it finds.
    """

    candidates: list[Any] = [persisted_job_id]
    if not isinstance(payload, dict):
        payload = {}
    candidates.extend(
        [
            payload.get("elastic_blast_job_id"),
            payload.get("k8s_job_id"),
        ]
    )
    progress = payload.get("_progress")
    if isinstance(progress, dict):
        steps = progress.get("steps")
        if isinstance(steps, dict):
            for step_name in ("running", "exporting_results", "warming_up", "staging_db"):
                step = steps.get(step_name)
                if isinstance(step, dict):
                    k8s = step.get("k8s")
                    if isinstance(k8s, dict):
                        candidates.append(k8s.get("job_id"))
    external = payload.get("external")
    if isinstance(external, dict):
        # The sibling /v1/jobs row (stored under ``payload.external``) now
        # exposes the elastic-blast job id directly as ``elb_job_id`` once it
        # has discovered it from the submit output. This is the ONLY way the
        # dashboard can map an OpenAPI/Service Bus job to its in-cluster BLAST
        # pods (it never ran ``elastic-blast submit`` itself), so without it
        # live pod-log streaming for external jobs is impossible.
        candidates.append(external.get("elb_job_id"))
        k8s = external.get("k8s")
        if isinstance(k8s, dict):
            candidates.append(k8s.get("job_id"))
    for value in candidates:
        text = str(value or "").strip()
        if _ELASTIC_BLAST_JOB_ID_RE.fullmatch(text):
            return text.lower()
    return elastic_blast_selector_from_error(error_text)


def _pod_env_values(pod: dict[str, Any], name: str) -> set[str]:
    values: set[str] = set()
    spec = pod.get("spec", {}) if isinstance(pod.get("spec"), dict) else {}
    for group_key in ("initContainers", "containers"):
        for container in spec.get(group_key, []) or []:
            for env in container.get("env", []) or []:
                if env.get("name") != name:
                    continue
                value = str(env.get("value") or "")
                if value:
                    values.add(value)
    return values


def _owner_names(pod: dict[str, Any]) -> set[str]:
    return {
        str(owner.get("name") or "")
        for owner in pod.get("metadata", {}).get("ownerReferences", []) or []
        if owner.get("name")
    }


def _target_phase(pod_name: str, container_name: str) -> str:
    if pod_name.startswith("init-ssd-") or container_name in {"get-blastdb", "vmtouch"}:
        return "staging_db"
    if pod_name.startswith("elb-finalizer-") or "finalizer" in pod_name:
        return "exporting_results"
    if pod_name.startswith("warm-") or "warmup" in pod_name:
        return "warming_up"
    if pod_name.startswith("blastn-batch-") or "batch" in pod_name:
        return "running"
    return "running"


def _name_has_suffix_token(name: str, suffix: str) -> bool:
    return bool(re.search(rf"(?:^|-){re.escape(suffix)}(?:-|$)", name, re.IGNORECASE))


def _is_init_ssd_name_for_suffix(name: str, suffix: str) -> bool:
    return bool(
        re.fullmatch(
            rf"init-ssd-{re.escape(suffix)}-[0-9]{{1,3}}(?:-[a-z0-9]+)?",
            name,
            re.IGNORECASE,
        )
    )


def _pod_matches_job(pod: dict[str, Any], job_id: str, elastic_job_id: str) -> bool:
    metadata = pod.get("metadata", {}) or {}
    labels = metadata.get("labels", {}) or {}
    pod_name = str(metadata.get("name") or "")
    owner_names = _owner_names(pod)
    ids = {value for value in {job_id, elastic_job_id} if value}
    valid_elastic_selector = (
        elastic_job_id
        if _ELASTIC_BLAST_JOB_ID_RE.fullmatch(elastic_job_id)
        or _ELASTIC_BLAST_SHORT_SELECTOR_RE.fullmatch(elastic_job_id)
        else ""
    )
    elastic_suffix = elastic_blast_suffix(valid_elastic_selector)
    is_short_selector = bool(_ELASTIC_BLAST_SHORT_SELECTOR_RE.fullmatch(valid_elastic_selector))

    if is_short_selector:
        return any(
            _is_init_ssd_name_for_suffix(name, elastic_suffix)
            for name in {pod_name, *owner_names}
        )

    labelled_id = str(labels.get("elb-job-id") or "")
    if labelled_id:
        return labelled_id in ids
    env_ids = _pod_env_values(pod, "BLAST_ELB_JOB_ID")
    if env_ids:
        return bool(env_ids & ids)
    return False


def discover_k8s_log_targets(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str,
    job_id: str,
    elastic_job_id: str = "",
) -> list[K8sLogTarget]:
    """Discover pod/container log targets for one BLAST run."""

    if not _SAFE_K8S_NAME_RE.match(namespace):
        raise ValueError("invalid namespace")
    if not (subscription_id and resource_group and cluster_name and job_id):
        return []

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(f"{server}/api/v1/namespaces/{namespace}/pods", timeout=10)
        response.raise_for_status()
        targets: list[K8sLogTarget] = []
        for pod in response.json().get("items", []) or []:
            if not isinstance(pod, dict) or not _pod_matches_job(pod, job_id, elastic_job_id):
                continue
            metadata = pod.get("metadata", {}) or {}
            pod_name = str(metadata.get("name") or "")
            if not pod_name or not _SAFE_K8S_NAME_RE.match(pod_name):
                continue
            spec = pod.get("spec", {}) if isinstance(pod.get("spec"), dict) else {}
            # Iterate init containers too: ElasticBLAST's batch search pod runs
            # `import-query-batches` as an init container, and when it fails the
            # main `blast` container never starts (empty log). Excluding init
            # containers here is exactly why a failed search showed no error in
            # the live/persisted log stream.
            seen_containers: set[str] = set()
            for group_key in ("initContainers", "containers"):
                for container in spec.get(group_key, []) or []:
                    container_name = str(container.get("name") or "")
                    if not container_name or not _SAFE_K8S_NAME_RE.match(container_name):
                        continue
                    if container_name in seen_containers:
                        continue
                    seen_containers.add(container_name)
                    targets.append(
                        K8sLogTarget(
                            namespace=namespace,
                            pod_name=pod_name,
                            container_name=container_name,
                            phase=_target_phase(pod_name, container_name),
                        )
                    )
        return sorted(targets, key=lambda target: target.key)
    finally:
        session.close()


def stream_k8s_log_lines(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    target: K8sLogTarget,
    *,
    tail_lines: int,
    stop_event: threading.Event,
) -> Iterator[str]:
    """Yield log lines for a single pod/container using Kubernetes follow."""

    if not _SAFE_K8S_NAME_RE.match(target.namespace):
        raise ValueError("invalid namespace")
    if not _SAFE_K8S_NAME_RE.match(target.pod_name):
        raise ValueError("invalid pod name")
    if not _SAFE_K8S_NAME_RE.match(target.container_name):
        raise ValueError("invalid container name")
    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(
            f"{server}/api/v1/namespaces/{target.namespace}/pods/{target.pod_name}/log",
            params={
                "container": target.container_name,
                "follow": "true",
                "tailLines": max(1, min(int(tail_lines or 100), 2_000)),
                "timestamps": "true",
            },
            stream=True,
            timeout=(10, 65),
        )
        # Release the underlying socket even when the generator is closed
        # mid-stream (browser disconnect, ``stop_event``, follower task
        # cancellation). Without this the connection is held until GC, which
        # under streaming load leaves the K8s API session pool exhausted.
        # Some test doubles return a plain object so guard the close() call.
        try:
            response.raise_for_status()
            for raw_line in response.iter_lines(decode_unicode=True):
                if stop_event.is_set():
                    break
                line = str(raw_line or "").strip("\r")
                if line:
                    yield sanitise(line)[:_LINE_MAX_CHARS]
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: S110 - best-effort socket release
                    pass
    finally:
        session.close()


__all__ = [
    "K8sLogTarget",
    "discover_k8s_log_targets",
    "elastic_blast_suffix",
    "fetch_k8s_pod_log_tail",
    "resolve_elastic_blast_job_id",
    "stream_k8s_log_lines",
]


def fetch_k8s_pod_log_tail(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    target: K8sLogTarget,
    *,
    tail_lines: int = 200,
) -> list[str]:
    """Fetch the tail of a (possibly completed) pod/container log.

    Unlike `stream_k8s_log_lines`, this uses ``follow=false`` and returns once
    Kubernetes flushes the requested tail, so it is safe to call against
    terminated pods during artifact finalization. Lines are sanitised and
    truncated identically to the streaming path so downstream persistence
    sees the same shape.
    """

    if not _SAFE_K8S_NAME_RE.match(target.namespace):
        raise ValueError("invalid namespace")
    if not _SAFE_K8S_NAME_RE.match(target.pod_name):
        raise ValueError("invalid pod name")
    if not _SAFE_K8S_NAME_RE.match(target.container_name):
        raise ValueError("invalid container name")
    bounded = max(1, min(int(tail_lines or 200), 2_000))
    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(
            f"{server}/api/v1/namespaces/{target.namespace}/pods/{target.pod_name}/log",
            params={
                "container": target.container_name,
                "follow": "false",
                "tailLines": bounded,
                "timestamps": "true",
            },
            timeout=(10, 30),
        )
        response.raise_for_status()
        body = getattr(response, "text", "") or ""
        cleaned: list[str] = []
        for raw_line in body.splitlines():
            line = str(raw_line or "").strip("\r")
            if not line:
                continue
            cleaned.append(sanitise(line)[:_LINE_MAX_CHARS])
        return cleaned
    finally:
        session.close()

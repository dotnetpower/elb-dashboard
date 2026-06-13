"""Kubernetes manifest builders for node-local BLAST DB warmup jobs.

Responsibility: Kubernetes manifest builders for node-local BLAST DB warmup jobs
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `WarmupJobPlan`, `build_warmup_scripts_configmap`, `build_warmup_job_plan`,
`database_status_from_warmup_jobs`, `attach_pod_progress_to_database_status`,
`infer_warmup_pod_phase`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from api.services.db.sharding import DEFAULT_CONTAINER, MAX_SHARDS, partition_prefix_for
from api.services.warmup.scripts import (
    BLAST_VMTOUCH_AKS_SCRIPT as _BLAST_VMTOUCH_AKS_SCRIPT,
)
from api.services.warmup.scripts import (
    INIT_DB_SHARD_AKS_SCRIPT as _INIT_DB_SHARD_AKS_SCRIPT,
)
from api.services.warmup.scripts import (
    warmup_shell_command as _warmup_shell_command,
)

DEFAULT_WARMUP_APP_LABEL = "elb-db-warmup"
DEFAULT_NAMESPACE = "default"
DEFAULT_SCRIPTS_CONFIGMAP = "elb-warmup-scripts"
DEFAULT_NODE_DB_PATH = "/workspace/blast"
DEFAULT_CONTAINER_DB_PATH = "/blast/blastdb"

# azcopy tuning for the node-local warmup download (blob -> node disk over the
# private endpoint). The download script intentionally does NOT pin
# ``AZCOPY_CONCURRENCY_VALUE``: azcopy's own default is ``16 * vCPU`` (capped at
# 300) and it dynamically tunes against CPU usage, which a fixed value defeats.
# A live throwaway-pod benchmark on cluster-02 (Standard_E16s_v5, core_nt,
# 256 MiB blocks) measured the old hard-coded ``concurrency=16`` at 158 MB/s vs
# azcopy's CPU-based auto (256 connections) at 281 MB/s — a 1.78x speedup just
# from letting azcopy choose. We therefore inject the azcopy env vars ONLY when
# an operator override is supplied (``None`` means "let azcopy auto-tune"); the
# buffer constraint that actually matters is just ``max block size <= 0.75 *
# AZCOPY_BUFFER_GB`` (256 MiB needs ~0.34 GiB), so the script's small default
# buffer was never the bottleneck.

WARMUP_PHASE_LABELS: dict[str, str] = {
    "waiting": "Waiting for container",
    "starting": "Starting warmup",
    "copying_files": "Copying files to node disk",
    "verifying_db": "Verifying local BLAST DB",
    "touching_memory": "Touching files into RAM",
    "completed": "Warm on node",
    "failed": "Failed",
    "unknown": "Running",
}

_SAFE_DB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_NODE_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_SAFE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
_SHARD_SUFFIX_RE = re.compile(r"^(?P<db>.+)_shard_(?P<shard>\d{2,})$")
SOURCE_VERSION_ANNOTATION = "elb.dashboard/source-version"


@dataclass(frozen=True, slots=True)
class WarmupJobPlan:
    """A concrete set of one-shard-per-node warmup jobs."""

    db_name: str
    mol_type: str
    storage_account: str
    num_shards: int
    nodes: tuple[str, ...]
    image: str
    namespace: str
    jobs: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_name": self.db_name,
            "mol_type": self.mol_type,
            "storage_account": self.storage_account,
            "num_shards": self.num_shards,
            "nodes": list(self.nodes),
            "image": self.image,
            "namespace": self.namespace,
            "jobs": list(self.jobs),
        }


def build_warmup_scripts_configmap(
    *,
    namespace: str = DEFAULT_NAMESPACE,
    name: str = DEFAULT_SCRIPTS_CONFIGMAP,
) -> dict[str, Any]:
    """Build the ConfigMap mounted by node-local warmup Jobs."""

    if not _SAFE_LABEL_RE.match(namespace):
        raise ValueError(f"invalid namespace: {namespace!r}")
    if not _SAFE_LABEL_RE.match(name):
        raise ValueError(f"invalid configmap name: {name!r}")
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": DEFAULT_WARMUP_APP_LABEL},
        },
        "data": {
            "init-db-shard-aks.sh": _INIT_DB_SHARD_AKS_SCRIPT,
            "blast-vmtouch-aks.sh": _BLAST_VMTOUCH_AKS_SCRIPT,
        },
    }


def build_warmup_job_plan(
    *,
    db_name: str,
    mol_type: str,
    storage_account: str,
    num_shards: int,
    nodes: list[str],
    image: str,
    namespace: str = DEFAULT_NAMESPACE,
    container: str = DEFAULT_CONTAINER,
    scripts_configmap: str = DEFAULT_SCRIPTS_CONFIGMAP,
    node_db_path: str = DEFAULT_NODE_DB_PATH,
    app_label: str = DEFAULT_WARMUP_APP_LABEL,
    azcopy_concurrency: int | None = None,
    azcopy_buffer_gb: int | None = None,
    source_version: str = "",
) -> WarmupJobPlan:
    """Build one Kubernetes Job per shard, pinned one-to-one onto nodes.

    The generated jobs mount a node-local hostPath at ``/blast/blastdb`` so the
    shard ``.nal`` files produced by ``api.services.db.sharding`` point at the
    same paths BLAST sees inside the container.

    Single-shard DBs are the *full* database and are broadcast to every Ready
    node (one Job per node, all staging shard-00 content) because an unsharded
    search batch can land on any ``workload=blast`` node.
    """

    _validate_common(
        db_name=db_name,
        mol_type=mol_type,
        storage_account=storage_account,
        num_shards=num_shards,
        nodes=nodes,
        image=image,
        namespace=namespace,
        scripts_configmap=scripts_configmap,
        node_db_path=node_db_path,
        app_label=app_label,
        azcopy_concurrency=azcopy_concurrency,
        azcopy_buffer_gb=azcopy_buffer_gb,
    )
    if len(nodes) < num_shards:
        raise ValueError(
            f"need at least {num_shards} nodes for one-shard-per-node warmup, got {len(nodes)}"
        )

    prefix = partition_prefix_for(storage_account, db_name, num_shards, container=container)
    # A single-shard DB is the *full* database — the search batch can land on any
    # ``workload=blast`` node, so the full DB must be staged on every Ready node,
    # not just node 0. Broadcast one Job per node (all staging shard-00 content)
    # so an unsharded search never fails with "database not found" on an
    # un-warmed node. Multi-shard DBs keep one-shard-per-node placement.
    broadcast_full_db = num_shards == 1 and len(nodes) > 1
    job_count = len(nodes) if broadcast_full_db else num_shards
    jobs = tuple(
        _build_job(
            db_name=db_name,
            mol_type=mol_type,
            shard_idx=idx,
            node_name=nodes[idx],
            image=image,
            namespace=namespace,
            scripts_configmap=scripts_configmap,
            node_db_path=node_db_path,
            app_label=app_label,
            partition_prefix=prefix,
            azcopy_concurrency=azcopy_concurrency,
            azcopy_buffer_gb=azcopy_buffer_gb,
            source_version=source_version,
            db_content_shard_idx=0 if broadcast_full_db else idx,
        )
        for idx in range(job_count)
    )
    return WarmupJobPlan(
        db_name=db_name,
        mol_type=mol_type,
        storage_account=storage_account,
        num_shards=num_shards,
        nodes=tuple(nodes[:job_count]),
        image=image,
        namespace=namespace,
        jobs=jobs,
    )


def database_status_from_warmup_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate node-local warmup Jobs into the dashboard status shape."""

    by_db: dict[str, dict[str, Any]] = {}
    for job in jobs:
        metadata = job.get("metadata", {}) or {}
        labels = metadata.get("labels", {}) or {}
        raw_db_name = str(labels.get("db") or "")
        label_shard = str(labels.get("shard") or "")
        db_name, derived_shard = _logical_db_name_and_shard(raw_db_name, label_shard)
        if not db_name:
            continue
        status = job.get("status", {}) or {}
        info = by_db.setdefault(
            db_name,
            {
                "name": db_name,
                "mol_type": "",
                "nodes_ready": 0,
                "nodes_failed": 0,
                "nodes_active": 0,
                "total_jobs": 0,
                "shards": [],
                "shard_nodes": {},
                "shard_host_paths": {},
                "progress_pct": 0,
                # `warmup` marks the entry as coming from an explicit
                # dashboard warmup Job (label `app={DEFAULT_WARMUP_APP_LABEL}`),
                # which the New Search run-profile picker requires before
                # auto-selecting the "Warmed database" profile.
                "sources": ["warmup"],
            },
        )
        succeeded = int(status.get("succeeded") or 0)
        failed = int(status.get("failed") or 0)
        active = int(status.get("active") or 0)
        info["total_jobs"] += 1
        info["nodes_ready"] += 1 if succeeded > 0 else 0
        info["nodes_failed"] += 1 if failed > 0 and succeeded == 0 else 0
        info["nodes_active"] += 1 if active > 0 else 0
        shard = label_shard or derived_shard
        if shard:
            info["shards"].append(shard)
            node_name = _job_node_name(job)
            if node_name:
                info["shard_nodes"][shard] = node_name
            host_path = _job_db_host_path(job)
            if host_path:
                info["shard_host_paths"][shard] = host_path

        start_time = _parse_k8s_time(status.get("startTime"))
        completion_time = _parse_k8s_time(status.get("completionTime"))
        if start_time is not None:
            previous = info.get("started_at")
            if previous is None or start_time < previous:
                info["started_at"] = start_time
        if completion_time is not None:
            previous = info.get("completed_at")
            if previous is None or completion_time > previous:
                info["completed_at"] = completion_time
        source_version = _job_source_version(job)
        if source_version:
            info.setdefault("source_versions", set()).add(source_version)

    for info in by_db.values():
        total = info["total_jobs"]
        done = info["nodes_ready"] + info["nodes_failed"]
        info["progress_pct"] = round((done / total) * 100, 1) if total else 0
        if total > 0 and info["nodes_ready"] == total:
            info["status"] = "Ready"
        elif info["nodes_failed"] > 0:
            info["status"] = "Failed"
        elif info["nodes_active"] > 0:
            info["status"] = "Loading"
        else:
            info["status"] = "Unknown"
        info["shards"] = sorted(set(info["shards"]))
        source_versions = info.pop("source_versions", set())
        if source_versions:
            sorted_versions = sorted(source_versions)
            info["source_versions"] = sorted_versions
            if len(sorted_versions) == 1:
                info["source_version"] = sorted_versions[0]
            else:
                info["status"] = "Stale"
                info["active_phase"] = "failed"
                info["active_phase_label"] = "Mixed DB generations"
                info["active_message"] = "Warmup jobs belong to multiple DB source versions."
        _attach_timing_estimate(info)
    return list(by_db.values())


def _logical_db_name_and_shard(raw_db_name: str, label_shard: str = "") -> tuple[str, str]:
    match = _SHARD_SUFFIX_RE.match(raw_db_name)
    if not match:
        return raw_db_name, label_shard
    return match.group("db"), label_shard or match.group("shard")


def _job_source_version(job: dict[str, Any]) -> str:
    metadata = job.get("metadata", {}) or {}
    annotations = metadata.get("annotations", {}) or {}
    value = annotations.get(SOURCE_VERSION_ANNOTATION)
    if isinstance(value, str) and value:
        return value
    template_metadata = job.get("spec", {}).get("template", {}).get("metadata", {}) or {}
    template_annotations = template_metadata.get("annotations", {}) or {}
    value = template_annotations.get(SOURCE_VERSION_ANNOTATION)
    return value if isinstance(value, str) else ""


def _job_db_host_path(job: dict[str, Any]) -> str:
    pod_spec = job.get("spec", {}).get("template", {}).get("spec", {})
    for volume in pod_spec.get("volumes", []) or []:
        if volume.get("name") != "db":
            continue
        host_path = (volume.get("hostPath") or {}).get("path")
        if isinstance(host_path, str):
            return host_path
    return ""


def _job_node_name(job: dict[str, Any]) -> str:
    node_name = job.get("spec", {}).get("template", {}).get("spec", {}).get("nodeName")
    return node_name if isinstance(node_name, str) else ""


def attach_pod_progress_to_database_status(
    databases: list[dict[str, Any]],
    pods: list[dict[str, Any]],
    logs_by_pod: dict[str, str],
) -> None:
    """Attach active per-pod warmup phase details to DB status rows.

    Kubernetes Job status only flips when a shard completes. During the long
    middle of a warmup all jobs are simply ``active``, which makes the
    dashboard look stuck at 0%. Pod phase + recent log markers reveal whether
    each shard is still copying from Storage to node-local disk, validating the
    local BLAST DB, or actively touching files into RAM with vmtouch.
    """

    by_db = {str(item.get("name") or ""): item for item in databases}
    latest_by_db_shard: dict[tuple[str, str], tuple[str, str, dict[str, Any]]] = {}
    for pod in pods:
        metadata = pod.get("metadata", {}) or {}
        if metadata.get("deletionTimestamp"):
            continue
        labels = metadata.get("labels", {}) or {}
        db_name = str(labels.get("db") or "")
        if not db_name:
            continue
        pod_name = str(metadata.get("name") or "")
        shard = str(labels.get("shard") or "")
        created_at = str(metadata.get("creationTimestamp") or "")
        detail = infer_warmup_pod_phase(pod, logs_by_pod.get(pod_name, ""))
        key = (db_name, shard)
        previous = latest_by_db_shard.get(key)
        if previous is None or (created_at, pod_name) > (previous[0], previous[1]):
            latest_by_db_shard[key] = (created_at, pod_name, detail)

    pod_details_by_db: dict[str, list[dict[str, Any]]] = {}
    for (db_name, _shard), (_created_at, _pod_name, detail) in latest_by_db_shard.items():
        pod_details_by_db.setdefault(db_name, []).append(detail)

    for db_name, pod_details in pod_details_by_db.items():
        info = by_db.get(db_name)
        if info is None:
            continue
        phase_counts: dict[str, int] = {}
        for detail in pod_details:
            phase = str(detail.get("phase") or "unknown")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        active_phase = _dominant_active_phase(phase_counts)
        active_detail = next(
            (
                detail
                for detail in pod_details
                if str(detail.get("phase") or "unknown") == active_phase
            ),
            pod_details[0],
        )
        info["active_phase"] = active_phase
        info["active_phase_label"] = WARMUP_PHASE_LABELS.get(
            active_phase, WARMUP_PHASE_LABELS["unknown"]
        )
        info["active_message"] = active_detail.get("message") or ""
        info["active_last_log"] = active_detail.get("last_log") or ""
        info["phase_counts"] = phase_counts
        total = max(int(info.get("total_jobs") or 0), sum(phase_counts.values()))
        if total > 0:
            completed = int(phase_counts.get("completed") or 0)
            failed = int(phase_counts.get("failed") or 0)
            active = max(0, total - completed - failed)
            info["nodes_ready"] = completed
            info["nodes_failed"] = failed
            info["nodes_active"] = active
            log_progress = _aggregate_pod_progress_pct(pod_details, total=total)
            info["progress_pct"] = (
                log_progress
                if log_progress is not None
                else round(((completed + failed) / total) * 100, 1)
            )
            if completed == total:
                info["status"] = "Ready"
            elif failed > 0:
                info["status"] = "Failed"
            elif active > 0:
                info["status"] = "Loading"
            _attach_timing_estimate(info)
        info["pod_statuses"] = sorted(
            pod_details,
            key=lambda item: (str(item.get("shard") or ""), str(item.get("pod") or "")),
        )[:20]


def infer_warmup_pod_phase(pod: dict[str, Any], log_text: str) -> dict[str, Any]:
    """Infer the current warmup phase for a single shard pod."""

    metadata = pod.get("metadata", {}) or {}
    status = pod.get("status", {}) or {}
    spec = pod.get("spec", {}) or {}
    labels = metadata.get("labels", {}) or {}
    pod_name = str(metadata.get("name") or "")
    shard = str(labels.get("shard") or "")
    container_status = _warmup_container_status(status)
    waiting = (container_status.get("state") or {}).get("waiting") or {}
    terminated = (container_status.get("state") or {}).get("terminated") or {}
    running = (container_status.get("state") or {}).get("running") or {}
    pod_phase = str(status.get("phase") or "Unknown")
    log_tail = _last_meaningful_log_line(log_text)

    phase = "unknown"
    message = log_tail or pod_phase
    if terminated:
        exit_code = int(terminated.get("exitCode") or 0)
        phase = "completed" if exit_code == 0 else "failed"
        message = str(terminated.get("reason") or message)
    elif waiting:
        phase = "waiting"
        reason = str(waiting.get("reason") or "Waiting")
        message = reason
    elif pod_phase == "Pending":
        phase = "waiting"
        message = "Pod is pending scheduling or image startup"
    elif log_text:
        phase = _phase_from_warmup_log(log_text)
        message = _warmup_log_message(log_text) or message
    elif running:
        phase = "starting"
        message = "Container is running; waiting for warmup logs"

    return {
        "pod": pod_name,
        "shard": shard,
        "node": spec.get("nodeName") or "",
        "phase": phase,
        "phase_label": WARMUP_PHASE_LABELS.get(phase, WARMUP_PHASE_LABELS["unknown"]),
        "message": message[:240],
        "last_log": log_tail[:240] if log_tail else "",
        "started_at": running.get("startedAt")
        or status.get("startTime")
        or metadata.get("creationTimestamp"),
    }


def _warmup_container_status(status: dict[str, Any]) -> dict[str, Any]:
    for item in status.get("containerStatuses", []) or []:
        if item.get("name") == "warmup":
            return cast(dict[str, Any], item)
    containers = status.get("containerStatuses", []) or []
    return cast(dict[str, Any], containers[0]) if containers else {}


def _phase_from_warmup_log(log_text: str) -> str:
    text = log_text.lower()
    if "done shard=" in text or "runtime cache-blastdbs-to-ram" in text:
        return "completed"
    if (
        "failed after" in text
        or "manifest download failed" in text
        or "partial downloads remain" in text
    ):
        return "failed"
    if "retrying in" in text and "azcopy" in text:
        return "copying_files"
    if "error" in text and "azcopy" in text:
        return "failed"
    if "vmtouch memory limit" in text or "cache-blastdbs-to-ram" in text:
        return "touching_memory"
    if "blastdbcmd" in text or "database:" in text or "db files downloaded" in text:
        return "verifying_db"
    if (
        "downloading with pattern" in text
        or "downloading manifest" in text
        or "shard download:" in text
        or "azcopy" in text
        or "download_skip" in text
        or _azcopy_progress_pct(text) is not None
    ):
        return "copying_files"
    if "start shard=" in text:
        return "starting"
    return "unknown"


def _warmup_log_message(log_text: str) -> str:
    text = log_text.lower()
    if "authorizationfailure" in text and not any(
        marker in text
        for marker in (
            "final job status: completed",
            "downloading with pattern",
            "blastdbcmd",
            "vmtouch memory limit",
            "done shard=",
        )
    ):
        return "Storage authorization or firewall denied manifest download"
    if "manifest download failed" in text:
        return "Manifest download failed"
    if "downloading with pattern" in text and "log file is located at:" in text:
        return "Downloading shard files with azcopy"
    if _azcopy_progress_pct(text) is not None:
        return _last_meaningful_log_line(log_text)
    if "retrying in" in text and "azcopy" in text:
        return _last_meaningful_log_line(log_text)
    return ""


# Phases a pod only reaches AFTER its per-shard file copy has finished: the
# terminal states plus the post-download local steps (verifying the BLAST DB,
# touching files into RAM). Their logs no longer carry an azcopy "%", so
# treating them like an in-flight copy would fall back to 0 and make the DB
# progress bar saw-tooth down from ~100% the instant copying completes. They
# represent "copy done" and must therefore count as 100.
_POST_COPY_PROGRESS_PHASES = frozenset(
    {"completed", "failed", "verifying_db", "touching_memory"}
)


def _aggregate_pod_progress_pct(
    pod_details: list[dict[str, Any]],
    *,
    total: int,
) -> float | None:
    if total <= 0:
        return None
    values: list[float] = []
    for detail in pod_details:
        phase = str(detail.get("phase") or "")
        if phase in _POST_COPY_PROGRESS_PHASES:
            values.append(100.0)
            continue
        progress = _azcopy_progress_pct(
            str(detail.get("message") or ""),
            str(detail.get("last_log") or ""),
        )
        values.append(progress if progress is not None else 0.0)
    if not values:
        return None
    if len(values) < total:
        values.extend([0.0] * (total - len(values)))
    return round(sum(values[:total]) / total, 1)


def _azcopy_progress_pct(*texts: str) -> float | None:
    for text in texts:
        matches = re.findall(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%", text or "")
        for raw in reversed(matches):
            try:
                value = float(raw)
            except ValueError:
                continue
            if 0 <= value <= 100:
                return value
    return None


def _last_meaningful_log_line(log_text: str) -> str:
    for line in reversed(log_text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _dominant_active_phase(phase_counts: dict[str, int]) -> str:
    if not phase_counts:
        return "unknown"
    for phase in (
        "failed",
        "touching_memory",
        "verifying_db",
        "copying_files",
        "starting",
        "waiting",
        "unknown",
        "completed",
    ):
        if phase_counts.get(phase, 0) > 0:
            return phase
    return "unknown"


def _parse_k8s_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _attach_timing_estimate(info: dict[str, Any]) -> None:
    started_at = info.pop("started_at", None)
    completed_at = info.pop("completed_at", None)
    if not isinstance(started_at, datetime):
        return

    now = datetime.now(UTC)
    active = int(info.get("nodes_active") or 0)
    end = completed_at if active <= 0 and isinstance(completed_at, datetime) else now
    elapsed = max(0, int((end - started_at).total_seconds()))
    info["started_at"] = started_at.isoformat().replace("+00:00", "Z")
    info["elapsed_seconds"] = elapsed

    total = int(info.get("total_jobs") or 0)
    completed = int(info.get("nodes_ready") or 0) + int(info.get("nodes_failed") or 0)
    if total <= 0 or completed <= 0:
        return
    remaining = max(0, total - completed)
    if remaining == 0:
        info["estimated_remaining_seconds"] = 0
        return
    if active <= 0:
        return
    seconds_per_completed_job = elapsed / completed
    info["estimated_remaining_seconds"] = int(seconds_per_completed_job * remaining)


def _build_job(
    *,
    db_name: str,
    mol_type: str,
    shard_idx: int,
    node_name: str,
    image: str,
    namespace: str,
    scripts_configmap: str,
    node_db_path: str,
    app_label: str,
    partition_prefix: str,
    azcopy_concurrency: int | None,
    azcopy_buffer_gb: int | None,
    source_version: str,
    db_content_shard_idx: int | None = None,
) -> dict[str, Any]:
    # ``shard_idx`` is the *tracking* ordinal used for the Job name, labels, and
    # one-job-per-node pinning. ``db_content_shard_idx`` selects the DB *content*
    # the node stages. For a normal sharded warmup the two are identical (node N
    # holds shard N). For a single-shard "full DB" broadcast they differ: every
    # node stages the same shard-00 content (the full DB) but carries a distinct
    # tracking ordinal so the Job names stay unique and the status aggregation
    # counts each node separately.
    content_idx = shard_idx if db_content_shard_idx is None else db_content_shard_idx
    shard = f"{shard_idx:02d}"
    content_shard = f"{content_idx:02d}"
    shard_db = f"{db_name}_shard_{content_shard}"
    job_name = f"warm-{_job_name_fragment(db_name)}-{shard}"
    host_path = node_db_path.rstrip("/")
    command = _warmup_shell_command()
    annotations = {SOURCE_VERSION_ANNOTATION: source_version} if source_version else {}
    # azcopy concurrency / buffer are injected ONLY when an operator override is
    # supplied. When omitted (the default) the env vars stay unset so azcopy
    # uses its own CPU-based auto-tuning (16 * vCPU, capped at 300), which a
    # benchmark showed is ~1.78x faster than the old hard-coded 16.
    env = [
        {"name": "ELB_SHARD_IDX", "value": content_shard},
        {"name": "ELB_PARTITION_PREFIX", "value": partition_prefix},
        {"name": "ELB_DB", "value": shard_db},
        {"name": "ELB_DB_SOURCE_VERSION", "value": source_version},
        {"name": "ELB_DB_MOL_TYPE", "value": mol_type},
    ]
    if azcopy_concurrency is not None:
        env.append({"name": "AZCOPY_CONCURRENCY_VALUE", "value": str(azcopy_concurrency)})
    if azcopy_buffer_gb is not None:
        env.append({"name": "AZCOPY_BUFFER_GB", "value": str(azcopy_buffer_gb)})
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {
                "app": app_label,
                "db": _label_value(db_name),
                "shard": shard,
            },
            "annotations": annotations,
        },
        "spec": {
            "backoffLimit": 1,
            "template": {
                "metadata": {
                    "labels": {
                        "app": app_label,
                        "db": _label_value(db_name),
                        "shard": shard,
                    },
                    "annotations": annotations,
                },
                "spec": {
                    "restartPolicy": "Never",
                    "nodeName": node_name,
                    "tolerations": [
                        {
                            "key": "workload",
                            "operator": "Equal",
                            "value": "blast",
                            "effect": "NoSchedule",
                        }
                    ],
                    "containers": [
                        {
                            "name": "warmup",
                            "image": image,
                            "command": ["bash", "-lc"],
                            "args": [command],
                            "env": env,
                            "volumeMounts": [
                                {"name": "db", "mountPath": DEFAULT_CONTAINER_DB_PATH},
                                {"name": "scripts", "mountPath": "/scripts"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "db",
                            "hostPath": {
                                "path": host_path,
                                "type": "DirectoryOrCreate",
                            },
                        },
                        {
                            "name": "scripts",
                            "configMap": {
                                "name": scripts_configmap,
                                "defaultMode": 0o755,
                            },
                        },
                    ],
                },
            },
        },
    }


def _validate_common(
    *,
    db_name: str,
    mol_type: str,
    storage_account: str,
    num_shards: int,
    nodes: list[str],
    image: str,
    namespace: str,
    scripts_configmap: str,
    node_db_path: str,
    app_label: str,
    azcopy_concurrency: int | None,
    azcopy_buffer_gb: int | None,
) -> None:
    if not _SAFE_DB_RE.match(db_name):
        raise ValueError(f"invalid db_name: {db_name!r}")
    if mol_type not in {"nucl", "prot"}:
        raise ValueError("mol_type must be 'nucl' or 'prot'")
    if not re.match(r"^[a-z0-9]{3,24}$", storage_account):
        raise ValueError(f"invalid storage_account: {storage_account!r}")
    if num_shards < 1 or num_shards > MAX_SHARDS:
        raise ValueError(f"num_shards must be in [1, {MAX_SHARDS}]")
    if not nodes:
        raise ValueError("nodes must not be empty")
    bad_nodes = [node for node in nodes if not _SAFE_NODE_RE.match(node)]
    if bad_nodes:
        raise ValueError(f"invalid node name: {bad_nodes[0]!r}")
    if not _SAFE_IMAGE_RE.match(image):
        raise ValueError(f"invalid image: {image!r}")
    labels = (
        ("namespace", namespace),
        ("scripts_configmap", scripts_configmap),
        ("app_label", app_label),
    )
    for label_name, label in labels:
        if not _SAFE_LABEL_RE.match(label):
            raise ValueError(f"invalid {label_name}: {label!r}")
    if not node_db_path.startswith("/") or ".." in node_db_path.split("/"):
        raise ValueError("node_db_path must be an absolute path without '..'")
    if azcopy_concurrency is not None and (azcopy_concurrency < 1 or azcopy_concurrency > 512):
        raise ValueError("azcopy_concurrency must be in [1, 512]")
    if azcopy_buffer_gb is not None and (azcopy_buffer_gb < 1 or azcopy_buffer_gb > 64):
        raise ValueError("azcopy_buffer_gb must be in [1, 64]")


def _job_name_fragment(db_name: str) -> str:
    fragment = re.sub(r"[^a-z0-9-]+", "-", db_name.lower()).strip("-")
    if not fragment:
        return "db"
    return fragment[:48].strip("-") or "db"


def _label_value(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-_.")
    if not label:
        return "db"
    return label[:63].rstrip("-_.") or "db"

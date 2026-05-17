"""Kubernetes manifest builders for node-local BLAST DB warmup jobs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from api.services.db_sharding import DEFAULT_CONTAINER, MAX_SHARDS, partition_prefix_for

DEFAULT_WARMUP_APP_LABEL = "elb-db-warmup"
DEFAULT_NAMESPACE = "default"
DEFAULT_SCRIPTS_CONFIGMAP = "elb-scripts"
DEFAULT_NODE_DB_PATH = "/workspace/blast"
DEFAULT_CONTAINER_DB_PATH = "/blast/blastdb"
DEFAULT_AZCOPY_CONCURRENCY = 16
DEFAULT_AZCOPY_BUFFER_GB = 2

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
    azcopy_concurrency: int = DEFAULT_AZCOPY_CONCURRENCY,
    azcopy_buffer_gb: int = DEFAULT_AZCOPY_BUFFER_GB,
) -> WarmupJobPlan:
    """Build one Kubernetes Job per shard, pinned one-to-one onto nodes.

    The generated jobs mount a node-local hostPath at ``/blast/blastdb`` so the
    shard ``.nal`` files produced by ``api.services.db_sharding`` point at the
    same paths BLAST sees inside the container.
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
        )
        for idx in range(num_shards)
    )
    return WarmupJobPlan(
        db_name=db_name,
        mol_type=mol_type,
        storage_account=storage_account,
        num_shards=num_shards,
        nodes=tuple(nodes[:num_shards]),
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
        db_name = labels.get("db") or ""
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
            },
        )
        succeeded = int(status.get("succeeded") or 0)
        failed = int(status.get("failed") or 0)
        active = int(status.get("active") or 0)
        info["total_jobs"] += 1
        info["nodes_ready"] += 1 if succeeded > 0 else 0
        info["nodes_failed"] += 1 if failed > 0 and succeeded == 0 else 0
        info["nodes_active"] += 1 if active > 0 else 0
        shard = labels.get("shard")
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
        _attach_timing_estimate(info)
    return list(by_db.values())


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
            info["progress_pct"] = round(((completed + failed) / total) * 100, 1)
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
            return item
    containers = status.get("containerStatuses", []) or []
    return containers[0] if containers else {}


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
    if "retrying in" in text and "azcopy" in text:
        return _last_meaningful_log_line(log_text)
    return ""


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
    azcopy_concurrency: int,
    azcopy_buffer_gb: int,
) -> dict[str, Any]:
    shard = f"{shard_idx:02d}"
    shard_db = f"{db_name}_shard_{shard}"
    job_name = f"warm-{_job_name_fragment(db_name)}-{shard}"
    host_path = node_db_path.rstrip("/")
    command = _warmup_shell_command()
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
                            "env": [
                                {"name": "ELB_SHARD_IDX", "value": shard},
                                {"name": "ELB_PARTITION_PREFIX", "value": partition_prefix},
                                {"name": "ELB_DB", "value": shard_db},
                                {"name": "ELB_DB_MOL_TYPE", "value": mol_type},
                                {
                                    "name": "AZCOPY_CONCURRENCY_VALUE",
                                    "value": str(azcopy_concurrency),
                                },
                                {"name": "AZCOPY_BUFFER_GB", "value": str(azcopy_buffer_gb)},
                            ],
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


def _warmup_shell_command() -> str:
    return """
set -euo pipefail
cd /blast/blastdb
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
log "START shard=${ELB_SHARD_IDX} db=${ELB_DB} node=$(hostname)"
if find . -maxdepth 1 -name '.azDownload-*' | grep -q .; then
  log "CLEANUP partial downloads"
    find . -maxdepth 1 -name '.azDownload-*' -exec rm -rf {} +
fi
if [ -s .download-complete ] && [ ! -s taxonomy4blast.sqlite3 ]; then
    log "CACHE_INCOMPLETE missing taxonomy4blast.sqlite3"
    rm -f .download-complete
fi
if [ ! -s .download-complete ]; then
  /scripts/init-db-shard-aks.sh
  partials=$(find . -maxdepth 1 -name '.azDownload-*' | wc -l)
  if [ "$partials" != "0" ]; then
    log "ERROR partial downloads remain: $partials"
    exit 1
  fi
  nsq_count=$(find . -maxdepth 1 -name '*.nsq' | wc -l)
  if [ "$nsq_count" = "0" ]; then
    log "ERROR no nucleotide volume files downloaded"
    exit 1
  fi
  touch .download-complete
else
  log "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}"
fi
blastdbcmd -db "$ELB_DB" -info | tee warmup-db-info.txt
/scripts/blast-vmtouch-aks.sh | tee warmup-vmtouch.log
log "DONE shard=${ELB_SHARD_IDX} size=$(du -sh . | cut -f1)"
""".strip()


_INIT_DB_SHARD_AKS_SCRIPT = r"""
#!/bin/bash
set -euo pipefail

echo "BASH version ${BASH_VERSION}"
echo "Shard download: idx=${ELB_SHARD_IDX} prefix=${ELB_PARTITION_PREFIX} db=${ELB_DB}"

start=$(date +%s)
log_runtime() {
    local ts
    ts=$(date +'%F %T')
    printf '%s RUNTIME %s %f seconds\n' "$ts" "$1" "$2"
}

azcopy login --identity || { echo "ERROR: azcopy login failed"; exit 1; }
export AZCOPY_CONCURRENCY_VALUE=${AZCOPY_CONCURRENCY_VALUE:-16}
export AZCOPY_BUFFER_GB=${AZCOPY_BUFFER_GB:-2}

retry_azcopy() {
    local max_attempts=3 attempt=1 wait_sec=5
    while [ "$attempt" -le "$max_attempts" ]; do
        if azcopy "$@"; then return 0; fi
        echo "azcopy attempt ${attempt}/${max_attempts} failed, retrying in ${wait_sec}s..."
        sleep "$wait_sec"
        wait_sec=$((wait_sec * 2))
        attempt=$((attempt + 1))
    done
    echo "ERROR: azcopy failed after ${max_attempts} attempts"
    return 1
}

SHARD_URL="${ELB_PARTITION_PREFIX}${ELB_SHARD_IDX}/"
MANIFEST_URL="${SHARD_URL}${ELB_DB}.manifest"
NAL_URL="${SHARD_URL}${ELB_DB}.nal"
echo "Downloading manifest: ${MANIFEST_URL}"
retry_azcopy cp "${MANIFEST_URL}" /tmp/manifest.txt --log-level=ERROR || {
    echo "ERROR: manifest download failed"
    exit 1
}
retry_azcopy cp "${NAL_URL}" "./${ELB_DB}.nal" --log-level=ERROR || true
VOLUMES=$(cat /tmp/manifest.txt)
echo "Volumes: ${VOLUMES}"

DB_BASE_URL=$(echo "${ELB_PARTITION_PREFIX}" | sed 's|/[^/]*/[^/]*$|/|')
ORIG_DB=$(echo "${ELB_DB}" | sed 's/_shard_[0-9]*$//')
DB_URL="${DB_BASE_URL}${ORIG_DB}/"
echo "DB base URL: ${DB_URL}"

PATTERN=""
for VOL in $VOLUMES; do
    [ -n "$PATTERN" ] && PATTERN="${PATTERN};"
    PATTERN="${PATTERN}${VOL}.*"
done
PATTERN="${PATTERN};taxdb.btd;taxdb.bti;taxonomy4blast.sqlite3;${ORIG_DB}.ndb;${ORIG_DB}.ntf;${ORIG_DB}.nto"
echo "Downloading with pattern: ${PATTERN}"

retry_azcopy cp "${DB_URL}*" . \
    --include-pattern "${PATTERN}" \
    --block-size-mb=256 \
    --log-level=WARNING

find . -maxdepth 1 -name '.azDownload-*' -exec rm -rf {} +

end=$(date +%s)
log_runtime "download-shard-${ELB_SHARD_IDX}" $((end - start))

nsq_count=$(find . -maxdepth 1 -name '*.nsq' | wc -l)
echo "DB files downloaded: ${nsq_count} .nsq files"
echo "Total size: $(du -sh . 2>/dev/null | cut -f1)"
if [ "$nsq_count" = "0" ]; then
    echo "ERROR: no nucleotide volume files downloaded"
    exit 1
fi

VOLPATHS=""
for VOL in $VOLUMES; do
    [ -n "$VOLPATHS" ] && VOLPATHS="$VOLPATHS "
    VOLPATHS="${VOLPATHS}$(pwd)/${VOL}"
done
echo "VOLPATHS=${VOLPATHS}" > /tmp/shard_volpaths.txt
echo "Volume paths: ${VOLPATHS}"
pkill -f azcopy 2>/dev/null || true
rm -rf /root/.azcopy 2>/dev/null || true
""".strip()


_BLAST_VMTOUCH_AKS_SCRIPT = r"""
#!/bin/bash
set -euo pipefail

echo "BASH version ${BASH_VERSION}"
start=$(date +%s)
log_runtime() {
    local ts
    ts=$(date +'%F %T')
    printf '%s RUNTIME %s %f seconds\n' "$ts" "$1" "$2"
}

AVAIL_MEM=$(awk '/MemAvailable/ {print int($2/1024/1024*0.8)"G"}' /proc/meminfo)
echo "vmtouch memory limit: ${AVAIL_MEM}"
blastdb_path -dbtype "$ELB_DB_MOL_TYPE" -db "$ELB_DB" -getvolumespath \
    | tr ' ' '\n' \
    | parallel vmtouch -tqm "$AVAIL_MEM"

mkdir -p results
exit_code=$?
end=$(date +%s)
log_runtime "cache-blastdbs-to-ram" $((end - start))
exit $exit_code
""".strip()


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
    azcopy_concurrency: int,
    azcopy_buffer_gb: int,
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
    if azcopy_concurrency < 1 or azcopy_concurrency > 512:
        raise ValueError("azcopy_concurrency must be in [1, 512]")
    if azcopy_buffer_gb < 1 or azcopy_buffer_gb > 64:
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

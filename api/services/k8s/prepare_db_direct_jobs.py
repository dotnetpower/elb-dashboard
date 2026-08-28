"""Kubernetes builders for NCBI Direct BLAST database generation staging.

Responsibility: Build the ConfigMap and Indexed Job that securely downloads,
    verifies, extracts, and uploads one pinned NCBI HTTPS archive per index.
Edit boundaries: Pure manifest builders only; NCBI discovery, Kubernetes API
    submission, Azure metadata promotion, and reconciliation stay elsewhere.
Key entry points: `build_direct_scripts_configmap`, `build_direct_job_manifest`.
Risky contracts: Archive inputs are pre-validated immutable manifests; pods
    accept regular root-level DB files only, enforce extraction-size bounds,
    write into a generation-scoped prefix, and publish a completion marker last.
Validation: `uv run pytest -q api/tests/test_prepare_db_direct_manifest.py`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from api.services.k8s.prepare_db_jobs import (
    _SAFE_DB_RE,
    _SAFE_IMAGE_RE,
    _SAFE_K8S_NAME_RE,
    _SAFE_LABEL_RE,
    _SAFE_STORAGE_ACCOUNT_RE,
    DEFAULT_AZCOPY_CONCURRENCY,
    DEFAULT_AZCOPY_IMAGE,
    DEFAULT_BACKOFF_LIMIT,
    DEFAULT_NAMESPACE,
    DEFAULT_TTL_SECONDS_AFTER_FINISHED,
)
from api.services.ncbi_direct import NcbiDirectArchive

_DIRECT_APP_LABEL = "elb-prepare-db-direct"
_SAFE_PREFIX_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/generations/[a-z0-9][a-z0-9-]{7,63}$"
)


def direct_prepare_job_name(db_name: str, generation_id: str) -> str:
    """Return a deterministic Kubernetes-safe Direct Job/ConfigMap name."""
    if not _SAFE_DB_RE.fullmatch(db_name):
        raise ValueError("invalid DB name")
    db_fragment = re.sub(r"[^a-z0-9-]+", "-", db_name.lower()).strip("-")
    db_fragment = db_fragment[:24].strip("-") or "db"
    generation_fragment = re.sub(r"[^a-z0-9]+", "", generation_id.lower())[-12:]
    if not generation_fragment:
        raise ValueError("invalid generation id")
    return f"prepare-{db_fragment}-{generation_fragment}"


DIRECT_PREPARE_SCRIPT = r"""#!/bin/bash
set -euo pipefail

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
INDEX=$(printf '%02d' "${JOB_COMPLETION_INDEX:?JOB_COMPLETION_INDEX required}")
SPEC="/scripts/archive-${INDEX}.json"
DB_NAME="${ELB_DB_NAME:?ELB_DB_NAME required}"
STORAGE_ACCOUNT="${ELB_STORAGE_ACCOUNT:?ELB_STORAGE_ACCOUNT required}"
DEST_PREFIX="${ELB_DEST_PREFIX:?ELB_DEST_PREFIX required}"
TRANSFER_SHA="${ELB_TRANSFER_MANIFEST_SHA256:?ELB_TRANSFER_MANIFEST_SHA256 required}"
SCRATCH="/scratch/${INDEX}"
ARCHIVE="${SCRATCH}/archive.tar.gz"
WORK="${SCRATCH}/out"
mkdir -p "$WORK"
rm -f "$ARCHIVE"

readarray -t VALUES < <(python3 - "$SPEC" <<'PY'
import json
import sys
spec = json.load(open(sys.argv[1], encoding="utf-8"))
for key in ("url", "md5", "size", "member_prefix"):
    print(spec[key])
PY
)
URL="${VALUES[0]}"
EXPECTED_MD5="${VALUES[1]}"
EXPECTED_SIZE="${VALUES[2]}"
MEMBER_PREFIX="${VALUES[3]}"
log "DIRECT_DOWNLOAD index=${INDEX} size=${EXPECTED_SIZE}"
curl --fail --show-error --location --proto '=https' --tlsv1.2 \
  --retry 5 --retry-all-errors --retry-delay 5 --connect-timeout 30 \
  --max-time "${ELB_ARCHIVE_TIMEOUT_SECONDS:-7200}" \
    "$URL" --output "$ARCHIVE"

python3 - "$ARCHIVE" "$WORK" "$MEMBER_PREFIX" "$EXPECTED_MD5" "$EXPECTED_SIZE" <<'PY'
import hashlib
import json
import os
import re
import sys
import tarfile
from pathlib import Path

archive = Path(sys.argv[1])
out = Path(sys.argv[2])
member_prefix = sys.argv[3]
expected_md5 = sys.argv[4]
expected_size = int(sys.argv[5])
actual_size = archive.stat().st_size
if actual_size != expected_size:
    raise SystemExit(f"archive size mismatch expected={expected_size} actual={actual_size}")
h = hashlib.md5(usedforsecurity=False)
with archive.open("rb") as handle:
    for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
        h.update(chunk)
if h.hexdigest() != expected_md5:
    raise SystemExit("archive MD5 mismatch")
allowed = re.compile(rf"^{re.escape(member_prefix)}(?:\.\d+)?\.[A-Za-z0-9]+$")
max_expanded = max(expected_size * 5, 1024 * 1024)
expanded = 0
written = []
with tarfile.open(archive, "r:gz") as bundle:
    members = bundle.getmembers()
    if not members:
        raise SystemExit("archive contained no members")
    for member in members:
        name = member.name
        if not member.isfile() or name != os.path.basename(name) or not allowed.fullmatch(name):
            raise SystemExit(f"unsafe archive member: {name!r}")
        expanded += member.size
        if expanded > max_expanded:
            raise SystemExit("archive expansion exceeded the bounded ratio")
    for member in members:
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"could not read archive member: {member.name}")
        target = out / member.name
        with source, target.open("wb") as destination:
            while chunk := source.read(8 * 1024 * 1024):
                destination.write(chunk)
        written.append({"name": member.name, "size": member.size})
(out / ".files.json").write_text(json.dumps(written, sort_keys=True), encoding="utf-8")
PY
rm -f "$ARCHIVE"

if ! azcopy login --identity >/tmp/azcopy-login.log 2>&1; then
  log "ERROR azcopy login --identity failed"
  exit 3
fi
export AZCOPY_CONCURRENCY_VALUE="${AZCOPY_CONCURRENCY_VALUE:-8}"
DEST="https://${STORAGE_ACCOUNT}.${ELB_BLOB_SUFFIX:-blob.core.windows.net}/blast-db/${DEST_PREFIX}/"
for file in "$WORK"/*; do
  [ -f "$file" ] || continue
  azcopy copy "$file" "$DEST" --overwrite=true --block-size-mb=64 --log-level=ERROR
  rm -f "$file"
done
MARKER="${SCRATCH}/marker.json"
python3 - "$WORK/.files.json" "$MARKER" "$TRANSFER_SHA" "$INDEX" <<'PY'
import json
import sys
files = json.load(open(sys.argv[1], encoding="utf-8"))
json.dump(
    {"index": int(sys.argv[4]), "transfer_manifest_sha256": sys.argv[3], "files": files},
    open(sys.argv[2], "w", encoding="utf-8"),
    sort_keys=True,
)
PY
azcopy copy "$MARKER" "${DEST}.manifests/${INDEX}.json" --overwrite=true --log-level=ERROR
log "DIRECT_DONE index=${INDEX}"
"""


def build_direct_scripts_configmap(
    *,
    archives: Sequence[NcbiDirectArchive],
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
) -> dict[str, Any]:
    """Build one immutable archive spec per Indexed Job completion."""
    if not archives:
        raise ValueError("archives must not be empty")
    if not _SAFE_K8S_NAME_RE.fullmatch(name) or not _SAFE_LABEL_RE.fullmatch(namespace):
        raise ValueError("invalid ConfigMap name or namespace")
    data = {"prepare-direct.sh": DIRECT_PREPARE_SCRIPT}
    encoded_size = len(DIRECT_PREPARE_SCRIPT.encode("utf-8"))
    for index, archive in enumerate(archives):
        if not _SAFE_DB_RE.fullmatch(archive.member_prefix):
            raise ValueError("archive member_prefix must be a safe DB name")
        archive_spec = json.dumps(
            {
                "url": archive.url,
                "md5": archive.md5,
                "size": archive.size,
                "member_prefix": archive.member_prefix,
            },
            sort_keys=True,
        )
        encoded_size += len(archive_spec.encode("utf-8"))
        if encoded_size > 900 * 1024:
            raise ValueError("Direct ConfigMap would exceed the 900 KiB safety cap")
        data[f"archive-{index:02d}.json"] = archive_spec
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": _DIRECT_APP_LABEL}},
        "data": data,
    }


def build_direct_job_manifest(
    *,
    job_name: str,
    db_name: str,
    storage_account: str,
    generation_id: str,
    destination_prefix: str,
    transfer_manifest_sha256: str,
    archive_count: int,
    scripts_configmap: str,
    image: str = DEFAULT_AZCOPY_IMAGE,
    namespace: str = DEFAULT_NAMESPACE,
    parallelism: int = 4,
    azcopy_concurrency: int = DEFAULT_AZCOPY_CONCURRENCY,
    backoff_limit: int = DEFAULT_BACKOFF_LIMIT,
    ttl_seconds_after_finished: int = DEFAULT_TTL_SECONDS_AFTER_FINISHED,
    active_deadline_seconds: int = 8 * 60 * 60,
    max_archive_size: int = 8 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    """Build a bounded Indexed Job for generation-scoped Direct staging."""
    if not _SAFE_DB_RE.fullmatch(db_name) or not _SAFE_STORAGE_ACCOUNT_RE.fullmatch(
        storage_account
    ):
        raise ValueError("invalid DB name or storage account")
    if not _SAFE_K8S_NAME_RE.fullmatch(job_name) or not _SAFE_K8S_NAME_RE.fullmatch(
        scripts_configmap
    ):
        raise ValueError("invalid Job or ConfigMap name")
    if not _SAFE_LABEL_RE.fullmatch(namespace) or not _SAFE_IMAGE_RE.fullmatch(image):
        raise ValueError("invalid namespace or image")
    if not _SAFE_PREFIX_RE.fullmatch(destination_prefix):
        raise ValueError("invalid generation destination prefix")
    if not re.fullmatch(r"[0-9a-f]{64}", transfer_manifest_sha256):
        raise ValueError("invalid transfer manifest SHA-256")
    if archive_count < 1 or parallelism < 1 or parallelism > min(8, archive_count):
        raise ValueError("invalid archive count or parallelism")
    if max_archive_size <= 0:
        raise ValueError("max_archive_size must be positive")
    scratch_bytes = max_archive_size * 6
    scratch_gib = max(2, (scratch_bytes + (1024**3 - 1)) // 1024**3)
    labels = {"app": _DIRECT_APP_LABEL, "db": db_name, "generation": generation_id[:63]}
    env = [
        {"name": "ELB_DB_NAME", "value": db_name},
        {"name": "ELB_STORAGE_ACCOUNT", "value": storage_account},
        {"name": "ELB_DEST_PREFIX", "value": destination_prefix},
        {"name": "ELB_TRANSFER_MANIFEST_SHA256", "value": transfer_manifest_sha256},
        {"name": "AZCOPY_CONCURRENCY_VALUE", "value": str(azcopy_concurrency)},
        {
            "name": "JOB_COMPLETION_INDEX",
            "valueFrom": {
                "fieldRef": {
                    "fieldPath": "metadata.annotations['batch.kubernetes.io/job-completion-index']"
                }
            },
        },
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": namespace, "labels": labels},
        "spec": {
            "completionMode": "Indexed",
            "completions": archive_count,
            "parallelism": min(parallelism, archive_count),
            "backoffLimitPerIndex": backoff_limit,
            "maxFailedIndexes": archive_count,
            "ttlSecondsAfterFinished": ttl_seconds_after_finished,
            "activeDeadlineSeconds": active_deadline_seconds,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": {"kubernetes.azure.com/mode": "user"},
                    "tolerations": [
                        {
                            "key": "workload",
                            "operator": "Equal",
                            "value": "blast",
                            "effect": "NoSchedule",
                        }
                    ],
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "kubernetes.io/hostname",
                            "whenUnsatisfiable": "ScheduleAnyway",
                            "labelSelector": {"matchLabels": labels},
                        }
                    ],
                    "containers": [
                        {
                            "name": "prepare-db-direct",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["bash", "-lc"],
                            "args": ["/scripts/prepare-direct.sh"],
                            "env": env,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": False,
                                "runAsNonRoot": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {
                                    "cpu": "500m",
                                    "memory": "512Mi",
                                    "ephemeral-storage": f"{scratch_gib}Gi",
                                },
                                "limits": {
                                    "memory": "2Gi",
                                    "ephemeral-storage": f"{scratch_gib}Gi",
                                },
                            },
                            "volumeMounts": [
                                {"name": "scripts", "mountPath": "/scripts"},
                                {"name": "scratch", "mountPath": "/scratch"},
                                {"name": "azcopy-cache", "mountPath": "/root/.azcopy"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "scripts",
                            "configMap": {"name": scripts_configmap, "defaultMode": 0o555},
                        },
                        {"name": "scratch", "emptyDir": {"sizeLimit": f"{scratch_gib}Gi"}},
                        {
                            "name": "azcopy-cache",
                            "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"},
                        },
                    ],
                },
            },
        },
    }


__all__ = [
    "DIRECT_PREPARE_SCRIPT",
    "build_direct_job_manifest",
    "build_direct_scripts_configmap",
    "direct_prepare_job_name",
]

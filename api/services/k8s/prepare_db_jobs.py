"""Kubernetes Job builders + lifecycle helpers for the prepare-db AKS-fanout mode.

Responsibility: Pure-domain helpers that plan shards, build the per-shard
    ConfigMap + Indexed Job manifest, and submit / poll / delete the Job
    through the existing direct Kubernetes API session. Issue #7 Phase 1
    `mode=aks` path; the legacy server-side `start_copy_from_url` route
    in [api/routes/storage/prepare_db.py](../../routes/storage/prepare_db.py)
    is untouched.
Edit boundaries: Pure builders + thin K8s HTTP wrappers only. Storage
    metadata writes, lock acquisition, NCBI listing, and audit live in the
    Celery task (`api.tasks.storage.prepare_db_via_aks`) — do not import
    those here.
Key entry points: `plan_prepare_db_shards`, `prepare_db_job_name`,
    `build_prepare_db_scripts_configmap`, `build_prepare_db_job_manifest`,
    `submit_prepare_db_job`, `get_prepare_db_job`, `delete_prepare_db_job`.
Risky contracts: The per-pod script lives in `PREPARE_DB_AKS_SCRIPT` and
    references `/scripts/prepare-db.sh` + `/scripts/shard-NN.txt`; keep the
    paths in lock-step with `build_prepare_db_scripts_configmap`. The Job's
    `completionMode: Indexed` requires Kubernetes >= 1.24 (all currently
    supported AKS versions). `azcopy login --identity` resolves the
    kubelet-attached managed identity, which must already carry
    `Storage Blob Data Contributor` on the workload Storage account (the
    existing warmup RBAC grant covers this). Each pod runs an
    `azcopy copy --from-to=S3Blob` over its shard's files (selected via an
    `--include-pattern` built from the shard basenames) inside a bounded
    in-pod resume loop: the first attempt heals with `--overwrite=true`, then
    up to `ELB_AZCOPY_MAX_ATTEMPTS-1` retries use `--overwrite=ifSourceNewer`
    so a single transient per-file failure re-fetches only the missing files
    instead of triggering a full-shard re-download (the historical
    non-convergence). azcopy reads the public NCBI bucket anonymously and
    writes through the kubelet MI to the Storage private endpoint, so the
    account stays `publicNetworkAccess: Disabled`. azcopy is PINNED to 10.28.0 because the
    `aka.ms` redirect now serves 10.32.4, which panics on every S3Blob copy
    (override via `ELB_AZCOPY_URL`); the download depends on egress to
    `github.com` from the pod NIC. The trailing `/*` source + include-pattern
    is what yields the FLAT `blast-db/<db>/<file>` layout elastic-blast
    requires — azcopy's list-of-files mode nests `<snapshot>/` and must
    not be used.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_planner.py
    api/tests/test_prepare_db_aks_manifest.py`.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.credentials import _get_k8s_session

LOGGER = logging.getLogger(__name__)

DEFAULT_APP_LABEL = "elb-prepare-db"
DEFAULT_NAMESPACE = "default"
DEFAULT_SCRIPTS_CONFIGMAP_PREFIX = "elb-prepare-db"
# Pin the base image — charter §3 requires Azure CLI >= 2.81 and forbids
# `:latest`-style tags for production workloads. The image does not ship
# azcopy; the entrypoint script downloads it from aka.ms.
DEFAULT_AZCOPY_IMAGE = "mcr.microsoft.com/azure-cli:2.81.0"
DEFAULT_AZCOPY_CONCURRENCY = 16
# Per-INDEX retry budget (wired to the Job's `backoffLimitPerIndex`, NOT the
# global `backoffLimit`). Each shard independently retries up to this many
# times; a transient failure in one shard never counts against the others.
# 2 → each shard gets 3 attempts, which absorbs the occasional OOM / SNAT
# reset / NCBI 5xx / node preemption over a multi-hour download.
DEFAULT_BACKOFF_LIMIT = 2
DEFAULT_TTL_SECONDS_AFTER_FINISHED = 3600
# 4 hours: `nt` (~4.8k files) and `core_nt` genuinely take well over an hour
# to stream from NCBI at 10-shard parallelism — the dashboard itself badges
# these as "May take hours". The previous 45 min ceiling fired
# `activeDeadlineSeconds` mid-download (K8s marks the Job
# `Failed/DeadlineExceeded`), abandoning every still-in-flight shard. The
# downstream per-blob reconcile then counted those never-committed files as
# `failed: missing`, surfacing as a misleading "partial · N failed" even
# though nothing actually errored — the Job was simply killed too early.
# Normal completion still exits the instant all shards succeed, so a larger
# ceiling never slows a small DB; it only stops cutting off the big ones.
# Override per-job via `PREPARE_DB_AKS_JOB_TIMEOUT_SECONDS`.
DEFAULT_ACTIVE_DEADLINE_SECONDS = 4 * 60 * 60
DEFAULT_FILES_PER_POD = 50
DEFAULT_MAX_PARALLELISM = 10
# When a Job with the deterministic ``(db, source_version)`` name already
# exists but carries a ``deletionTimestamp`` (i.e. a just-issued cancel is
# still tearing it down in the background), ``_create_job_if_absent`` waits
# up to this long for the terminating Job to disappear before creating a
# fresh one. Without this, a cancel-then-resubmit within the same NCBI
# snapshot day collides with the dying Job, is mis-reported as a healthy
# "existing" run, and never spawns new pods.
DEFAULT_TERMINATING_WAIT_SECONDS = 60.0
DEFAULT_TERMINATING_POLL_SECONDS = 2.0
SOURCE_VERSION_ANNOTATION = "elb.dashboard/source-version"

_SAFE_DB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_STORAGE_ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
_SAFE_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_SAFE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")


# Pod-side shell script. Each pod is one completion of an Indexed Job; its
# shard index comes from `JOB_COMPLETION_INDEX` (kubelet downward-API env).
# The script reads its assigned NCBI keys from `/scripts/shard-NN.txt`, then
# runs a SINGLE `azcopy copy --from-to=S3Blob` over exactly those files
# (selected via an `--include-pattern` built from the shard's basenames).
# azcopy fetches the public NCBI S3 objects anonymously and streams them
# into the workload Storage account through the kubelet managed identity.
# This replaced the old per-file serial `curl | azcopy --from-to=PipeBlob`
# loop, which threw away azcopy's native multi-file/multi-connection
# parallelism (one file at a time, no per-file resume) and could never
# converge for `nt`/`core_nt` within the Job deadline. A single azcopy call
# was measured live at ~172 MB/s on one 1Gi pod (5x3GB nt volumes, peak RSS
# 143 MiB) — see docs/features_change for the proof.
#
# azcopy version is PINNED to 10.28.0: the current `aka.ms` redirect serves
# 10.32.4, which panics (nil deref in getSourceServiceClient) on every
# `--from-to=S3Blob` copy. 10.28.0 handles S3Blob correctly. Override the
# download URL with `ELB_AZCOPY_URL` only if a newer build is verified to
# fix the S3Blob crash.
#
# The base image (`mcr.microsoft.com/azure-cli`) does NOT ship `azcopy`,
# GNU `tar`, or other GNU text tools (it is Azure Linux / Mariner), so the
# script downloads azcopy once per pod and extracts the single binary using
# Python's stdlib `tarfile`. It also avoids GNU text tools and `head` in
# pipefail pipelines (`head` SIGPIPEs the upstream and fails the shard);
# `python3` is always present because the azure-cli image is a python app.
PREPARE_DB_AKS_SCRIPT = r"""#!/bin/bash
set -euo pipefail

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

SHARD_INDEX=$(printf '%02d' "${JOB_COMPLETION_INDEX:?JOB_COMPLETION_INDEX required}")
DB_NAME="${ELB_DB_NAME:?ELB_DB_NAME required}"
STORAGE_ACCOUNT="${ELB_STORAGE_ACCOUNT:?ELB_STORAGE_ACCOUNT required}"
BLOB_SUFFIX="${ELB_BLOB_SUFFIX:-blob.core.windows.net}"
NCBI_BASE="${ELB_NCBI_BASE:-https://ncbi-blast-databases.s3.amazonaws.com}"
FILE_LIST="/scripts/shard-${SHARD_INDEX}.txt"

if [ ! -r "$FILE_LIST" ]; then
    log "ERROR shard file list $FILE_LIST not found"
    exit 2
fi

TOTAL=$(grep -cve '^[[:space:]]*$' "$FILE_LIST" || true)
log "START shard=${SHARD_INDEX} db=${DB_NAME} files=${TOTAL}"

# Bootstrap azcopy. The azure-cli image does not bundle it (and does not
# ship GNU `tar`), so download a PINNED linux build and pull the single
# `azcopy` binary out using Python's stdlib tarfile module. The version is
# pinned to 10.28.0 because aka.ms now serves 10.32.4, which crashes on
# `--from-to=S3Blob`. Override with ELB_AZCOPY_URL only after verifying a
# newer build fixes the S3Blob crash.
AZCOPY_URL="${ELB_AZCOPY_URL:-https://github.com/Azure/azure-storage-azcopy/releases/download/v10.28.0/azcopy_linux_amd64_10.28.0.tar.gz}"
if ! command -v azcopy >/dev/null 2>&1; then
    log "Installing pinned azcopy from ${AZCOPY_URL}..."
    curl -fsSL --retry 5 --retry-delay 5 --max-time 180 \
        "$AZCOPY_URL" -o /tmp/azcopy.tgz
    python3 - <<'PY'
import os
import shutil
import tarfile

with tarfile.open("/tmp/azcopy.tgz", "r:gz") as t:
    for member in t.getmembers():
        if member.name.endswith("/azcopy") and member.isfile():
            t.extract(member, "/tmp")
            shutil.move(f"/tmp/{member.name}", "/usr/local/bin/azcopy")
            os.chmod("/usr/local/bin/azcopy", 0o755)
            break
    else:
        raise SystemExit("azcopy binary not found inside tarball")
PY
    rm -f /tmp/azcopy.tgz
    rm -rf /tmp/azcopy_linux_*
    log "azcopy installed: $(azcopy --version 2>/dev/null | head -1)"
fi

if ! azcopy login --identity >/tmp/azcopy-login.log 2>&1; then
    log "ERROR azcopy login --identity failed"
    sed 's/[A-Za-z0-9_-]\{20,\}/<redacted>/g' /tmp/azcopy-login.log | head -n 20
    exit 3
fi
export AZCOPY_CONCURRENCY_VALUE="${AZCOPY_CONCURRENCY_VALUE:-16}"

# Intentionally NOT exporting AZCOPY_BUFFER_GB: the container memory limit
# is 1Gi (see build_prepare_db_job_manifest) and azcopy auto-tunes the
# in-memory block buffer to ~25% of the cgroup limit. With the 64 MiB block
# size below, a live 1Gi pod peaked at 143 MiB RSS while streaming five
# parallel 3 GB nt volumes, so there is ample headroom. A larger explicit
# value risks OOMKilled mid-shard.

DEST_BASE="https://${STORAGE_ACCOUNT}.${BLOB_SUFFIX}/blast-db/${DB_NAME}"
SOURCE_VERSION="${ELB_SOURCE_VERSION:?ELB_SOURCE_VERSION required}"
BLOCK_MB="${ELB_AZCOPY_BLOCK_MB:-64}"

# Build the azcopy --include-pattern from this shard's basenames. The shard
# file holds full NCBI keys ("<snapshot>/nt.000.nsq"); azcopy matches the
# include-pattern against the object NAME, so strip each key to its basename
# and join with ';'. python3 is used because the azure-cli image ships no GNU
# text tools, and `head` inside a `set -o pipefail` pipeline would SIGPIPE the
# upstream and fail the shard.
PATTERN=$(python3 -c '
import sys
names = [ln.strip().split("/")[-1] for ln in open(sys.argv[1]) if ln.strip()]
sys.stdout.write(";".join(names))
' "$FILE_LIST")
if [ -z "$PATTERN" ]; then
    log "ERROR shard ${SHARD_INDEX} has no files to copy"
    exit 2
fi

# azcopy S3Blob copy of EXACTLY this shard's files, wrapped in a bounded
# in-pod resume loop. azcopy reads the public NCBI bucket anonymously (no
# AWS creds needed) and writes through the kubelet managed identity to the
# workload Storage account's private endpoint, so the account stays
# publicNetworkAccess=Disabled. The trailing "/*" on the source yields the
# FLAT "blast-db/<db>/<file>" layout elastic-blast requires (azcopy's
# list-of-files mode would instead nest "<snapshot>/" under the destination
# and break that layout). azcopy commits each blob atomically via its block
# list, so a killed transfer never leaves a partial committed blob, and it
# prints its own "% Done/Failed/Total" progress to stdout for live
# `kubectl logs`.
#
# WHY THE LOOP: a single shard streams ~200 GB / ~486 NCBI files over ~20
# min, so one transient per-file hiccup (S3 503 / SNAT reset / read timeout)
# is near-certain at this scale, and azcopy reports the whole run as exit 1
# (CompletedWithErrors) even when 485/486 committed fine. Exiting on that
# first non-zero would make the Job's per-index backoff relaunch a FRESH pod
# that re-downloads the WHOLE shard from scratch with --overwrite=true --
# which then hits another single-file blip near the end and never converges
# (the historical "partial · N failed" loop that kept nt/core_nt from ever
# finishing). Instead we re-run azcopy IN-PLACE: the first attempt uses
# `--overwrite=true` to heal any truncated/legacy blob, and every retry uses
# `--overwrite=ifSourceNewer` so the already-committed blobs (dest LMT newer
# than the source snapshot) are SKIPPED and only the handful of failed files
# are re-fetched -- converging in seconds, not another full download. Only
# after ELB_AZCOPY_MAX_ATTEMPTS in-pod attempts still fail does the pod exit
# non-zero, letting `backoffLimitPerIndex` relaunch the shard and ultimately
# surface an honest failure for a genuinely unreachable file.
#
# azcopy version is pinned to 10.28.0 above: aka.ms now serves 10.32.4,
# which panics on every --from-to=S3Blob copy.
SRC="${NCBI_BASE}/${SOURCE_VERSION}/*"
MAX_ATTEMPTS="${ELB_AZCOPY_MAX_ATTEMPTS:-5}"
log "COPY shard=${SHARD_INDEX} files=${TOTAL} src=${SOURCE_VERSION} attempts=${MAX_ATTEMPTS}"
attempt=1
overwrite="true"
rc=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    log "COPY attempt=${attempt}/${MAX_ATTEMPTS} shard=${SHARD_INDEX} overwrite=${overwrite}"
    set +e
    azcopy copy "$SRC" "${DEST_BASE}/" \
        --from-to=S3Blob \
        --include-pattern "$PATTERN" \
        --block-size-mb="$BLOCK_MB" \
        --overwrite="$overwrite" \
        --log-level=WARNING
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
        log "DONE shard=${SHARD_INDEX} azcopy_exit=0 attempt=${attempt}"
        exit 0
    fi
    log "RETRY shard=${SHARD_INDEX} azcopy_exit=${rc} attempt=${attempt}"
    # Re-fetch only the files that did not commit; skip the completed blobs.
    overwrite="ifSourceNewer"
    attempt=$((attempt + 1))
    sleep 5
done
log "DONE shard=${SHARD_INDEX} azcopy_exit=${rc} attempts_exhausted=${MAX_ATTEMPTS}"
exit "$rc"
"""


def plan_prepare_db_shards(
    files: list[str],
    *,
    sizes: dict[str, int] | None = None,
    max_pods: int = DEFAULT_MAX_PARALLELISM,
    files_per_pod: int = DEFAULT_FILES_PER_POD,
) -> list[list[str]]:
    """Split a file list into balanced shards using longest-processing-time-first (LPT).

    Why LPT: NCBI volume files for `core_nt`/`nt` range from 1 GB metadata to
    >10 GB ``.nsq``. A round-robin split puts wildly different per-pod totals
    and the slowest pod becomes the Job's wall time. LPT (sort by size desc,
    place each next file into the currently-lightest shard) achieves a
    bounded 4/3-OPT makespan and matches what BLAST sharding upstream uses.

    The shard count is ``min(max_pods, ceil(len(files) / files_per_pod))``,
    clamped to ``[1, len(files)]`` so a 3-file DB never spawns 10 pods.

    Args:
        files: NCBI S3 keys, e.g. ``["<snapshot>/core_nt.000.nhr", ...]``.
        sizes: Optional ``{key: bytes}`` map. Unknown-size files are placed
            with a constant weight so distribution stays balanced by count.
        max_pods: Hard upper bound on shard count.
        files_per_pod: Used to compute shards from total file count.

    Returns:
        ``list[list[str]]`` — one inner list per shard, preserving the order
        in which LPT assigned files. Shard count == ``len(returned)``.
    """
    if files_per_pod < 1:
        raise ValueError("files_per_pod must be >= 1")
    if max_pods < 1:
        raise ValueError("max_pods must be >= 1")
    if not files:
        return []
    sizes = sizes or {}
    total = len(files)
    # ceil division
    file_based_shards = (total + files_per_pod - 1) // files_per_pod
    target_shards = max(1, min(max_pods, file_based_shards, total))

    # Sort largest-first; tie-break on the key itself so the output is
    # deterministic for tests and for the Job's per-shard ConfigMap keys.
    def _weight(key: str) -> tuple[int, str]:
        return (-int(sizes.get(key, 0)), key)

    sorted_files = sorted(files, key=_weight)

    shards: list[list[str]] = [[] for _ in range(target_shards)]
    sums = [0] * target_shards
    for key in sorted_files:
        # +1 fallback when size is unknown so unknown-size files still
        # round-robin instead of all piling onto shard 0.
        weight = int(sizes.get(key, 0)) or 1
        # Pick the lightest shard. ``list.index(min(...))`` is O(n) per file
        # which is fine for n <= max_pods (default 10) and file counts in
        # the low thousands. A heap would be measurably faster only past
        # ~10k files per Job, which the cluster never sees.
        idx = sums.index(min(sums))
        shards[idx].append(key)
        sums[idx] += weight
    return shards


def prepare_db_job_name(db_name: str, source_version: str) -> str:
    """Deterministic Job name for `(db, source_version)`.

    Used as both the Job name and the ConfigMap name. Re-submitting the
    same `(db, source_version)` collides with the in-flight Job and the
    K8s API returns 409, which the Celery task surfaces as the existing
    in-progress message (no duplicate dispatch).

    Format: ``prepare-db-<safe-db>-<short-version>``. Stays <= 52 chars to
    leave headroom for the K8s 63-char metadata.name limit (the Indexed
    Job controller suffixes ``-<index>`` to pod names).
    """
    db_fragment = re.sub(r"[^a-z0-9-]+", "-", db_name.lower()).strip("-") or "db"
    db_fragment = db_fragment[:24].strip("-") or "db"
    # source_version is typically NCBI's snapshot dir like
    # "2026-05-21-01-05-02". Compress to just digits so the name stays
    # short and predictable.
    version_fragment = re.sub(r"[^0-9]+", "", source_version)
    version_fragment = version_fragment[-12:] or "x"
    return f"prepare-db-{db_fragment}-{version_fragment}"


def build_prepare_db_scripts_configmap(
    *,
    shards: list[list[str]],
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
    app_label: str = DEFAULT_APP_LABEL,
) -> dict[str, Any]:
    """Build the ConfigMap mounted by every prepare-db pod.

    Keys:
        - ``prepare-db.sh``: the entrypoint script (`PREPARE_DB_AKS_SCRIPT`).
        - ``shard-NN.txt`` per shard: newline-separated NCBI keys this shard
          should fetch. The pod picks its file based on `JOB_COMPLETION_INDEX`.

    Storage size budget: a ConfigMap maxes out at 1 MiB. ``core_nt`` ships
    ~800 files; each key averages ~70 bytes (e.g.
    ``2026-05-21-01-05-02/core_nt.012.nhr``). 800 * 70 = ~56 KiB, plus the
    ~2 KiB script. Even 10x worst case (8000 files) stays under 600 KiB,
    so we don't need to split into multiple ConfigMaps in Phase 1.
    """
    if not _SAFE_LABEL_RE.match(namespace):
        raise ValueError(f"invalid namespace: {namespace!r}")
    if not _SAFE_K8S_NAME_RE.match(name):
        raise ValueError(f"invalid configmap name: {name!r}")
    if not shards:
        raise ValueError("shards must not be empty")
    data: dict[str, str] = {"prepare-db.sh": PREPARE_DB_AKS_SCRIPT}
    for i, files in enumerate(shards):
        # Each shard list is newline-joined. Empty trailing newline so
        # `read -r` in the shell sees the last line.
        data[f"shard-{i:02d}.txt"] = ("\n".join(files) + "\n") if files else ""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": app_label},
        },
        "data": data,
    }


def build_prepare_db_job_manifest(
    *,
    job_name: str,
    db_name: str,
    storage_account: str,
    source_version: str,
    shard_count: int,
    scripts_configmap: str,
    image: str = DEFAULT_AZCOPY_IMAGE,
    namespace: str = DEFAULT_NAMESPACE,
    app_label: str = DEFAULT_APP_LABEL,
    azcopy_concurrency: int = DEFAULT_AZCOPY_CONCURRENCY,
    backoff_limit: int = DEFAULT_BACKOFF_LIMIT,
    ttl_seconds_after_finished: int = DEFAULT_TTL_SECONDS_AFTER_FINISHED,
    active_deadline_seconds: int = DEFAULT_ACTIVE_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Build the Indexed Job manifest that runs N parallel `prepare-db` pods.

    ``completionMode: Indexed`` makes K8s expose ``JOB_COMPLETION_INDEX`` to
    each pod and treat ``completions == parallelism == shard_count`` as the
    success condition. ``ttlSecondsAfterFinished`` ensures the K8s TTL
    controller reaps the Job + pods even if the Celery worker dies before
    its explicit delete call lands.

    ``backoff_limit`` is wired to ``backoffLimitPerIndex`` (per-shard retry
    budget), NOT the global ``backoffLimit``. Combined with
    ``maxFailedIndexes == shard_count`` this isolates pod failures to their
    own shard so a few transient blips can no longer fail the whole Job and
    kill every in-flight pod — see the inline note on ``job_spec``.
    """
    if not _SAFE_DB_RE.match(db_name):
        raise ValueError(f"invalid db_name: {db_name!r}")
    if not _SAFE_STORAGE_ACCOUNT_RE.match(storage_account):
        raise ValueError(f"invalid storage_account: {storage_account!r}")
    if not _SAFE_K8S_NAME_RE.match(job_name):
        raise ValueError(f"invalid job_name: {job_name!r}")
    if not _SAFE_K8S_NAME_RE.match(scripts_configmap):
        raise ValueError(f"invalid scripts_configmap: {scripts_configmap!r}")
    if not _SAFE_LABEL_RE.match(namespace):
        raise ValueError(f"invalid namespace: {namespace!r}")
    if not _SAFE_LABEL_RE.match(app_label):
        raise ValueError(f"invalid app_label: {app_label!r}")
    if not _SAFE_IMAGE_RE.match(image):
        raise ValueError(f"invalid image: {image!r}")
    if shard_count < 1:
        raise ValueError("shard_count must be >= 1")
    if azcopy_concurrency < 1 or azcopy_concurrency > 512:
        raise ValueError("azcopy_concurrency must be in [1, 512]")
    if backoff_limit < 0:
        raise ValueError("backoff_limit must be >= 0")
    if ttl_seconds_after_finished < 60:
        raise ValueError("ttl_seconds_after_finished must be >= 60")
    if active_deadline_seconds < 60:
        raise ValueError("active_deadline_seconds must be >= 60")

    db_label = _label_value(db_name)
    source_version_label = _label_value(source_version) if source_version else ""

    pod_metadata_labels: dict[str, str] = {
        "app": app_label,
        "db": db_label,
    }
    if source_version_label:
        pod_metadata_labels["source-version"] = source_version_label
    job_labels = dict(pod_metadata_labels)

    annotations: dict[str, str] = {}
    if source_version:
        annotations[SOURCE_VERSION_ANNOTATION] = source_version

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        # Conservative tolerations: only run on the workload pool's
        # `workload=blast` taint if it exists; otherwise the pod
        # schedules on the default (untainted) user pool. We do NOT
        # add a broad tolerations array that would let prepare-db
        # pods land on the system pool.
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
                "name": "prepare-db",
                "image": image,
                # Pinned tag (see DEFAULT_AZCOPY_IMAGE). `IfNotPresent`
                # is only safe because the tag is immutable; revert to
                # `Always` if we ever go back to `:latest`.
                "imagePullPolicy": "IfNotPresent",
                "command": ["bash", "-lc"],
                "args": ["/scripts/prepare-db.sh"],
                "env": [
                    {"name": "ELB_DB_NAME", "value": db_name},
                    {"name": "ELB_STORAGE_ACCOUNT", "value": storage_account},
                    {"name": "ELB_SOURCE_VERSION", "value": source_version},
                    {
                        "name": "AZCOPY_CONCURRENCY_VALUE",
                        "value": str(azcopy_concurrency),
                    },
                    # Required by ``completionMode: Indexed`` — K8s also
                    # exposes it via the downward API path on the file
                    # system, but the env-var form is what the script
                    # actually reads.
                    {
                        "name": "JOB_COMPLETION_INDEX",
                        "valueFrom": {
                            "fieldRef": {
                                "fieldPath": (
                                    "metadata.annotations"
                                    "['batch.kubernetes.io/job-completion-index']"
                                ),
                            }
                        },
                    },
                ],
                "resources": {
                    "requests": {"cpu": "200m", "memory": "256Mi"},
                    "limits": {"memory": "1Gi"},
                },
                "volumeMounts": [
                    {"name": "scripts", "mountPath": "/scripts"},
                    {"name": "azcopy-cache", "mountPath": "/root/.azcopy"},
                ],
            }
        ],
        "volumes": [
            {
                "name": "scripts",
                "configMap": {
                    "name": scripts_configmap,
                    "defaultMode": 0o755,
                },
            },
            # Azcopy writes plan files to ~/.azcopy. PipeBlob mode does
            # not create plan files, but the login flow + occasional
            # diagnostic state still need a few KiB. 64Mi is plenty and
            # tmpfs-backed so a stale state from a backoff retry never
            # touches node disk.
            {"name": "azcopy-cache", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
        ],
    }

    pod_template: dict[str, Any] = {
        "metadata": {
            "labels": pod_metadata_labels,
            "annotations": annotations,
        },
        "spec": pod_spec,
    }

    job_spec: dict[str, Any] = {
        "completionMode": "Indexed",
        "completions": shard_count,
        "parallelism": shard_count,
        # PER-INDEX backoff, not a global budget. `backoffLimitPerIndex`
        # (Kubernetes >= 1.33 stable; AKS here is 1.34) gives each shard its
        # OWN retry budget, so a transient pod failure in one shard does not
        # count against the others.
        #
        # Why this matters: the previous spec set only the global
        # `backoffLimit`, which is a TOTAL failure budget across ALL indexes
        # ("the number of Pods with phase=Failed; if it reaches backoffLimit
        # the Job is failed and any running Pods are terminated"). With 10
        # shards each streaming ~486 NCBI files for hours, just `backoffLimit`
        # transient blips ANYWHERE (a single OOM / SNAT reset / NCBI 5xx /
        # node preemption per shard) tripped the global budget, marked the
        # whole Job Failed, and KILLED every still-running pod — discarding
        # nearly-complete work. The next retry re-downloaded everything and
        # died the same way: the unconverging "partial · succeeded=0/10"
        # loop. The almost-complete blob count (e.g. 4866/4874) was just the
        # in-flight files of the killed pods, not an independent failure.
        #
        # When `backoffLimitPerIndex` is set, K8s defaults the global
        # `backoffLimit` to MaxInt32, so it no longer acts as a cross-shard
        # guillotine — we intentionally omit it here.
        "backoffLimitPerIndex": backoff_limit,
        # Keep every healthy shard running to completion even if some shards
        # exhaust their per-index retries. K8s terminates the remaining pods
        # only once the number of failed indexes EXCEEDS `maxFailedIndexes`;
        # pinning it to `shard_count` makes that impossible, so a genuinely
        # broken shard never kills its healthy peers (their blobs persist and
        # those indexes are marked succeeded). The Job is still marked Failed
        # at the end if any index ultimately failed — correctly surfaced as a
        # partial by the Celery task — but transient blips now self-heal via
        # the per-index retry instead of failing the whole download.
        "maxFailedIndexes": shard_count,
        "ttlSecondsAfterFinished": ttl_seconds_after_finished,
        "activeDeadlineSeconds": active_deadline_seconds,
        "template": pod_template,
    }

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": job_labels,
            "annotations": annotations,
        },
        "spec": job_spec,
    }


def _label_value(value: str) -> str:
    """Coerce a free-form string into a valid K8s label value (<=63 chars)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-_.")
    if not cleaned:
        return "x"
    return cleaned[:63].rstrip("-_.") or "x"


def submit_prepare_db_job(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    configmap_manifest: dict[str, Any],
    job_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Apply the ConfigMap (upsert) then create the Job (create-if-missing).

    The Job uses a deterministic name keyed by ``(db, source_version)`` so a
    duplicate submission collides with the in-flight one and the K8s API
    returns 409 — which the caller surfaces as the existing "in progress"
    HTTP 409 instead of spawning a duplicate Job.
    """
    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    try:
        cm_summary = _upsert_configmap(session, server, configmap_manifest)
        if cm_summary.get("status") == "error":
            return {
                "status": "error",
                "stage": "configmap",
                "configmap": cm_summary,
            }
        job_summary = _create_job_if_absent(session, server, job_manifest)
        return {
            "status": job_summary.get("status", "error"),
            "stage": "job",
            "configmap": cm_summary,
            "job": job_summary,
        }
    finally:
        session.close()


def get_prepare_db_job(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str,
    job_name: str,
) -> dict[str, Any]:
    """Return the live Job's status block, or ``{"missing": True}`` on 404."""
    if not _SAFE_LABEL_RE.match(namespace):
        raise ValueError(f"invalid namespace: {namespace!r}")
    if not _SAFE_K8S_NAME_RE.match(job_name):
        raise ValueError(f"invalid job_name: {job_name!r}")
    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    try:
        url = f"{server}/apis/batch/v1/namespaces/{namespace}/jobs/{job_name}"
        response = session.get(url, timeout=10)
        if response.status_code == 404:
            return {"missing": True}
        if response.status_code != 200:
            return {
                "missing": False,
                "status_code": response.status_code,
                "error": response.text[:300],
            }
        body = response.json()
        status = body.get("status", {}) or {}
        spec = body.get("spec", {}) or {}
        return {
            "missing": False,
            "active": int(status.get("active") or 0),
            "succeeded": int(status.get("succeeded") or 0),
            "failed": int(status.get("failed") or 0),
            "completions": int(spec.get("completions") or 0),
            "parallelism": int(spec.get("parallelism") or 0),
            "conditions": status.get("conditions") or [],
            "start_time": status.get("startTime"),
            "completion_time": status.get("completionTime"),
        }
    finally:
        session.close()


def delete_prepare_db_job(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    namespace: str,
    job_name: str,
    configmap_name: str | None = None,
) -> dict[str, Any]:
    """Delete the Job (Background propagation) and optionally its ConfigMap.

    Idempotent — a 404 on either resource is treated as success.
    """
    if not _SAFE_LABEL_RE.match(namespace):
        raise ValueError(f"invalid namespace: {namespace!r}")
    if not _SAFE_K8S_NAME_RE.match(job_name):
        raise ValueError(f"invalid job_name: {job_name!r}")
    if configmap_name is not None and not _SAFE_K8S_NAME_RE.match(configmap_name):
        raise ValueError(f"invalid configmap_name: {configmap_name!r}")
    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    try:
        results: dict[str, Any] = {}
        job_url = f"{server}/apis/batch/v1/namespaces/{namespace}/jobs/{job_name}"
        job_resp = session.delete(
            job_url,
            params={"propagationPolicy": "Background"},
            timeout=10,
        )
        results["job"] = {
            "status_code": job_resp.status_code,
            "ok": job_resp.status_code in (200, 202, 404),
        }
        if configmap_name:
            cm_url = (
                f"{server}/api/v1/namespaces/{namespace}/configmaps/{configmap_name}"
            )
            cm_resp = session.delete(cm_url, timeout=10)
            results["configmap"] = {
                "status_code": cm_resp.status_code,
                "ok": cm_resp.status_code in (200, 202, 404),
            }
        results["status"] = (
            "deleted"
            if all(item.get("ok") for item in results.values() if isinstance(item, dict))
            else "partial"
        )
        return results
    finally:
        session.close()


def _upsert_configmap(session: Any, server: str, manifest: dict[str, Any]) -> dict[str, Any]:
    metadata = manifest.get("metadata", {}) or {}
    namespace = str(metadata.get("namespace") or DEFAULT_NAMESPACE)
    name = str(metadata.get("name") or "")
    if not name:
        return {"status": "error", "error": "configmap name required"}
    get_url = f"{server}/api/v1/namespaces/{namespace}/configmaps/{name}"
    response = session.get(get_url, timeout=10)
    if response.status_code == 404:
        create = session.post(
            f"{server}/api/v1/namespaces/{namespace}/configmaps",
            json=manifest,
            timeout=10,
        )
        if create.status_code not in {200, 201}:
            return {
                "status": "error",
                "name": name,
                "status_code": create.status_code,
                "error": create.text[:300],
            }
        return {"status": "created", "name": name}
    if response.status_code != 200:
        return {
            "status": "error",
            "name": name,
            "status_code": response.status_code,
            "error": response.text[:300],
        }
    existing = response.json()
    if existing.get("data") == manifest.get("data"):
        return {"status": "unchanged", "name": name}
    updated_manifest = {
        **manifest,
        "metadata": {
            **metadata,
            "resourceVersion": existing.get("metadata", {}).get("resourceVersion"),
        },
    }
    update = session.put(get_url, json=updated_manifest, timeout=10)
    if update.status_code not in {200, 201}:
        return {
            "status": "error",
            "name": name,
            "status_code": update.status_code,
            "error": update.text[:300],
        }
    return {"status": "updated", "name": name}


def _create_job_if_absent(
    session: Any,
    server: str,
    manifest: dict[str, Any],
    *,
    terminating_wait_seconds: float = DEFAULT_TERMINATING_WAIT_SECONDS,
    poll_interval_seconds: float = DEFAULT_TERMINATING_POLL_SECONDS,
) -> dict[str, Any]:
    metadata = manifest.get("metadata", {}) or {}
    namespace = str(metadata.get("namespace") or DEFAULT_NAMESPACE)
    name = str(metadata.get("name") or "")
    if not name:
        return {"status": "error", "error": "job name required"}
    jobs_url = f"{server}/apis/batch/v1/namespaces/{namespace}/jobs"
    get_url = f"{jobs_url}/{name}"

    deadline = time.monotonic() + max(0.0, terminating_wait_seconds)
    while True:
        existing = session.get(get_url, timeout=10)
        if existing.status_code == 200:
            # A Job with this deterministic name already exists. If it is
            # healthy (no deletionTimestamp) it is a genuine in-flight
            # duplicate — report "existing" so the caller does not spawn a
            # second Job. If it carries a deletionTimestamp it is a
            # *terminating* Job left by a just-issued cancel; the
            # deterministic name will be reused, so wait for it to be
            # collected before creating a fresh one instead of polling the
            # zombie forever.
            terminating = False
            try:
                body = existing.json()
                terminating = bool(
                    (body.get("metadata") or {}).get("deletionTimestamp")
                )
            except Exception:
                terminating = False
            if not terminating:
                return {"status": "existing", "name": name}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "status": "error",
                    "name": name,
                    "terminating": True,
                    "error": (
                        "previous Job is still terminating after a cancel; "
                        "retry shortly"
                    ),
                }
            time.sleep(min(poll_interval_seconds, remaining))
            continue
        if existing.status_code != 404:
            return {
                "status": "error",
                "name": name,
                "status_code": existing.status_code,
                "error": existing.text[:300],
            }
        # 404 — safe to create.
        create = session.post(jobs_url, json=manifest, timeout=10)
        if create.status_code in (200, 201, 202):
            return {"status": "created", "name": name}
        if create.status_code == 409:
            # Lost a race: either a peer created the Job, or the terminating
            # Job has not finished disappearing yet. Re-evaluate via GET so
            # the deletionTimestamp branch can wait it out; a healthy peer
            # Job is correctly reported as "existing".
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "existing", "name": name}
            time.sleep(min(poll_interval_seconds, remaining))
            continue
        return {
            "status": "error",
            "name": name,
            "status_code": create.status_code,
            "error": create.text[:300],
        }


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
    existing warmup RBAC grant covers this). The pod-side download flow
    (`curl … | azcopy copy --from-to=PipeBlob`) is what actually achieves
    the per-pod NAT parallelism — server-side `azcopy copy <url> <url>`
    would re-use Azure's backend IP and gain no speedup. The script
    bootstraps `azcopy` from `aka.ms/downloadazcopy-v10-linux` at pod
    start because the pinned `mcr.microsoft.com/azure-cli:2.81.0` image
    does not bundle it; the download depends on egress to `aka.ms` and
    `github.com` (release redirect) being reachable from the pod NIC.
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
# The script reads its assigned NCBI keys from `/scripts/shard-NN.txt`,
# streams each file through `curl | azcopy copy --from-to=PipeBlob` so the
# pod NIC -> AKS node's outbound NAT IP is what NCBI sees (per-node distinct
# source IPs = real parallelism), and the bytes never touch the pod's
# filesystem. Server-side `azcopy copy <url> <url>` would reuse Azure's
# backend IP and yield no NCBI-side speedup.
#
# The base image (`mcr.microsoft.com/azure-cli`) does NOT ship `azcopy`, so
# the script downloads the official build from aka.ms once per pod and
# extracts the single `azcopy` binary using Python's `tarfile` (the image
# does not have GNU `tar` either). Install size is ~30 MiB tgz on the
# container's writable rootfs; no extra emptyDir is needed.
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
# ship GNU `tar`), so download the official linux build and pull the single
# `azcopy` binary out using Python's stdlib tarfile module.
if ! command -v azcopy >/dev/null 2>&1; then
    log "Installing azcopy from aka.ms..."
    curl -fsSL --retry 5 --retry-delay 5 --max-time 120 \
        https://aka.ms/downloadazcopy-v10-linux -o /tmp/azcopy.tgz
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

# Intentionally NOT exporting AZCOPY_BUFFER_GB: the container memory
# limit is 1Gi (see build_prepare_db_job_manifest) and azcopy auto-tunes
# the in-memory block buffer to 25% of the cgroup limit (~256 MiB), which
# stays safely under the limit even with concurrency=16 and 64MiB blocks.
# A larger explicit value risks OOMKilled mid-shard.

DEST_BASE="https://${STORAGE_ACCOUNT}.${BLOB_SUFFIX}/blast-db/${DB_NAME}"

# Integrity verification cadence. Every Nth uploaded file gets a full
# round-trip check (NCBI Content-Length captured pre-flight + `azcopy
# list` post-upload). Each verify costs ~1-2s (RBAC token refresh +
# ARM call), so 1/10 keeps wall time bounded while still catching
# NCBI rolling-restart truncations within a single shard. Override
# with `ELB_VERIFY_EVERY_N=1` to verify every file (debug) or `=0`
# to disable verify entirely (azcopy's own Content-MD5 check is the
# only safety net then).
VERIFY_EVERY_N="${ELB_VERIFY_EVERY_N:-10}"

# Echo the integer ContentLength of a single blob to stdout, or nothing
# if the blob is absent. Emits the literal "PARSE_FAIL" when azcopy
# returns a shape we don't understand (version/schema drift) so callers
# can distinguish "blob missing" (empty) from "can't tell" (PARSE_FAIL).
# Shared by the per-file idempotency check and the post-upload verify.
blob_content_length() {
    azcopy list "$1" --output-type=json 2>/dev/null \
        | python3 -c 'import json,re,sys
def to_bytes(v):
    # azcopy <= ~10.31 emitted ContentLength as a raw integer byte count;
    # azcopy >= 10.32 emits a human-readable string ("2.79 GiB", "512 B").
    # Normalize BOTH to an integer byte count so the verify step can
    # compare against the raw NCBI Content-Length. Returns None on shapes
    # we cannot interpret so the caller falls back to PARSE_FAIL.
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMGTPE]?i?B)$", s, re.IGNORECASE)
    if not m:
        return None
    mult = {
        "B": 1,
        "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3,
        "TIB": 1024 ** 4, "PIB": 1024 ** 5,
        "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
        "TB": 1000 ** 4, "PB": 1000 ** 5,
    }.get(m.group(2).upper())
    if mult is None:
        return None
    return int(float(m.group(1)) * mult)
got = False
for line in sys.stdin:
    line=line.strip()
    if not line:
        continue
    try:
        obj=json.loads(line)
    except Exception:
        continue
    msg=obj.get("MessageContent") or obj.get("messageContent")
    cl=None
    if isinstance(msg, str) and msg:
        try:
            inner=json.loads(msg)
        except Exception:
            inner=None
        if isinstance(inner, dict):
            cl=inner.get("ContentLength") or inner.get("contentLength")
    if cl is None:
        cl=obj.get("ContentLength") or obj.get("contentLength")
    if cl is not None:
        b=to_bytes(cl)
        if b is not None:
            print(b); got=True; break
if not got:
    print("PARSE_FAIL")'
}

ok=0
fail=0
skip=0
file_index=0
# Read the shard file list on fd 3, NOT stdin. azcopy subcommands invoked
# inside the loop without an explicit stdin redirect (e.g. `azcopy remove`)
# will otherwise DRAIN fd 0 — when that fd is the FILE_LIST, the very first
# such call swallows every remaining line and the loop ends silently after
# one file. Isolating the list on fd 3 keeps the loop fed regardless of what
# any in-loop process does to stdin.
while IFS= read -r KEY <&3; do
    [ -z "$KEY" ] && continue
    file_index=$((file_index + 1))
    file_basename="${KEY##*/}"
    src_url="${NCBI_BASE}/${KEY}"
    dst_url="${DEST_BASE}/${file_basename}"
    # Per-file idempotency: skip ONLY blobs that are already FULLY
    # uploaded. A previous attempt (or an aborted server-side copy from
    # the legacy prepare path) can leave a 0-byte placeholder whose
    # ContentLength KEY exists but whose value is 0 — the old key-presence
    # check skipped those too, leaving a corrupt BLAST database. Require
    # ContentLength > 0; missing blobs (empty), 0-byte placeholders, and
    # parse-fail all fall through to a clean re-download (azcopy
    # overwrites).
    existing_len=$(blob_content_length "$dst_url")
    if [ -n "$existing_len" ] && [ "$existing_len" != "PARSE_FAIL" ] \
            && [ "$existing_len" -gt 0 ] 2>/dev/null ; then
        skip=$((skip + 1))
        # Throttled heartbeat for resume runs. Re-checking thousands of
        # already-staged blobs (one `azcopy list` per file) is silent and
        # can take minutes for a big DB like `nt`; emit a line every 50
        # skips so the pod still looks alive while it scans.
        if [ $((skip % 50)) -eq 0 ]; then
            log "[${file_index}/${TOTAL}] scanned; ${skip} already staged, ${ok} copied"
        fi
        continue
    fi
    # Decide upfront whether THIS file is one of the sampled ones. We
    # only pay the pre-flight HEAD + post-upload verify cost on the
    # sampled subset; the rest trust the curl|azcopy pipeline exit
    # code (already gated by `set -euo pipefail`).
    verify_this=""
    if [ "$VERIFY_EVERY_N" != "0" ] && [ $((file_index % VERIFY_EVERY_N)) -eq 0 ]; then
        verify_this="1"
    fi
    expected_size=""
    if [ -n "$verify_this" ]; then
        # Pre-flight: NCBI Content-Length tells us how many bytes to expect.
        # Captured here so the post-upload verification step can compare
        # apples to apples without trusting curl's exit code alone (a HTTP
        # 200 + truncated body would otherwise upload silently).
        #
        # Parse with python3 (always present — this is the azure-cli image,
        # and the script already relies on python3 above). The
        # mcr.microsoft.com/azure-cli base image ships no GNU text tools,
        # so a `BEGIN{IGNORECASE=1}`-style one-liner would abort the pod
        # under `set -euo pipefail` on the first sampled file (command not
        # found) and fail the whole shard. curl -sIL follows redirects and
        # prints headers for EVERY hop, so take the LAST content-length seen
        # (the final 200 response's real size), not the first (a 301/302 hop
        # has none or the wrong one).
        expected_size=$(curl -sIL --retry 3 --retry-delay 10 --max-time 60 \
            "$src_url" \
            | python3 -c 'import re,sys
val=""
for line in sys.stdin:
    m=re.match(r"(?i)^content-length:\s*([0-9]+)\s*$", line.strip())
    if m:
        val=m.group(1)
print(val)')
        if [ -z "${expected_size:-}" ] || [ "$expected_size" = "0" ]; then
            log "WARN no Content-Length for ${KEY}; integrity check will be skipped"
            expected_size=""
        fi
    fi
    # Progress heartbeat. The copy itself runs with `--log-level=ERROR
    # >/dev/null`, so a healthy multi-GB file (e.g. an `nt` shard) would
    # otherwise emit ZERO output for several minutes, making `kubectl logs`
    # look hung even though azcopy is streaming at full NIC speed. Logging
    # the file index/total + name before each copy turns that opaque
    # silence into a visibly-advancing counter so operators can tell a
    # slow-but-healthy run from a genuine stall. One line per file
    # (<= a few hundred per shard) is cheap for `kubectl logs`.
    log "[${file_index}/${TOTAL}] copy ${file_basename}${expected_size:+ (~${expected_size} bytes)}"
    # Stream NCBI -> pod NIC -> Azure Blob with no on-disk staging. Peak
    # memory ≈ block-size-mb × azcopy's internal buffer count (~200 MiB),
    # well under the 1 GiB container memory limit even for 10+ GiB files.
    # `set -euo pipefail` makes the pipeline fail if either side errors.
    #
    # PipeBlob takes the destination as a SINGLE positional argument
    # (`azcopy copy "<dst>" --from-to=PipeBlob`); stdin is the implicit
    # source. The older two-positional form `azcopy copy --from-to=PipeBlob
    # "" "<dst>"` is rejected by azcopy >= 10.32 (the empty first positional
    # is parsed as the source and the copy aborts immediately with no
    # transfer and a non-zero exit), which silently uploaded 0 bytes for
    # every file. Keep the destination first and do NOT reintroduce the
    # empty `""` placeholder.
    if curl -sSfL --retry 5 --retry-delay 30 --retry-all-errors \
            --max-time 1800 "$src_url" \
        | azcopy copy "$dst_url" --from-to=PipeBlob \
            --block-size-mb=64 --log-level=ERROR >/dev/null ; then
        # Post-upload integrity check ONLY on sampled files (see
        # VERIFY_EVERY_N). Mismatch means NCBI gave us a truncated body
        # or served an HTML error with status 200 — both have been
        # observed during NCBI rolling restarts. Delete the bad blob
        # so the next retry of this shard re-fetches it cleanly rather
        # than leaving garbage to confuse BLAST.
        #
        # The parser emits one of:
        #   "<int>"      — uploaded ContentLength successfully extracted
        #   "PARSE_FAIL" — azcopy list returned a shape we don't
        #                  understand (azcopy version upgrade, schema
        #                  drift); skip verify and assume the upload is
        #                  good rather than enter a delete-and-retry
        #                  loop that would burn through the backoffLimit
        if [ -n "$expected_size" ]; then
            uploaded_size=$(blob_content_length "$dst_url")
            if [ "$uploaded_size" = "PARSE_FAIL" ] || [ -z "$uploaded_size" ]; then
                log "WARN verify parse-fail ${KEY}; trusting upload exit code"
                ok=$((ok + 1))
                continue
            fi
            # blob_content_length normalizes the uploaded size to an integer
            # byte count even when azcopy >= 10.32 reports a human-readable
            # "2.79 GiB" string. That conversion keeps only ~3 significant
            # figures, so an EXACT compare against the raw NCBI byte count
            # would false-fail every multi-GB file (2999987448 vs 2.79 GiB =
            # 2995639357 after round-trip). Accept a 1% / 1 KiB tolerance: a
            # genuinely truncated body or an HTML error page is orders of
            # magnitude smaller and still trips the check.
            if ! python3 -c 'import sys
up=int(sys.argv[1]); exp=int(sys.argv[2])
sys.exit(0 if abs(up - exp) <= max(1024, exp // 100) else 1)' \
                    "$uploaded_size" "$expected_size"; then
                log "ERROR size mismatch ${KEY} exp=${expected_size} got=${uploaded_size}"
                # </dev/null so azcopy cannot drain the loop's fd-3 list.
                azcopy remove "$dst_url" --log-level=ERROR </dev/null >/dev/null 2>&1 || true
                fail=$((fail + 1))
                continue
            fi
        fi
        ok=$((ok + 1))
    else
        log "ERROR pipeline failed for ${KEY}"
        fail=$((fail + 1))
    fi
done 3< "$FILE_LIST"

log "DONE shard=${SHARD_INDEX} ok=${ok} fail=${fail} skip=${skip}"

# backoffLimit launches a fresh pod with a clean emptyDir for retries
# (restartPolicy=Never + emptyDir{medium: Memory}), so no per-pod
# cleanup is needed before exit.

if [ "$fail" -gt 0 ]; then
    exit 1
fi
exit 0
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
        "backoffLimit": backoff_limit,
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


"""Shell-script text fragments injected into BLAST DB warmup Kubernetes Jobs.

Used by [api/services/warmup/jobs.py](./jobs.py) when assembling the
container `command` and the shared `elb-warmup-scripts` ConfigMap. These are
plain shell-script strings — no Python logic, no f-strings — kept here so
that the manifest builder stays focused on Kubernetes shape.

Responsibility: Provide the three shell-script texts the BLAST DB warmup
Kubernetes Job needs (container entrypoint + two ConfigMap helpers).
Edit boundaries: Shell text only. Do not add Python helpers, regexes, or
Kubernetes client calls here.
Key entry points: `warmup_shell_command()`, `INIT_DB_SHARD_AKS_SCRIPT`,
`BLAST_VMTOUCH_AKS_SCRIPT`.
Risky contracts: The scripts reference the ConfigMap mount path
`/scripts/init-db-shard-aks.sh` and `/scripts/blast-vmtouch-aks.sh`; keep
those paths in lock-step with `build_warmup_scripts_configmap()`.
The warmup Job entrypoint deliberately does NOT call `blast-vmtouch-aks.sh`
any more (kept in ConfigMap only for the equivalence-experiment shell
scripts that exec it directly): on the DOWNLOAD path, pages staged by
``azcopy`` already sit in the OS page cache as a side effect of the
download, so an extra vmtouch is a noop. On the DOWNLOAD_SKIP path
(node_disk / data_disk restart where the shard survived on the node disk
and azcopy was skipped) that side effect never happened, so RAM is cold —
there the entrypoint runs an inline ``blastdb_path | vmtouch -t`` step to
read the shard into the node page cache off the first search's critical
path (opt out with ``ELB_WARMUP_VMTOUCH_DISABLE=1``). See
[docs/features_change/2026-06/2026-06-06-warmup-drop-fake-vmtouch.md] and
the 2026-07 node_disk warm-on-skip change.
Validation: `uv run pytest -q api/tests/test_warmup_*.py`.
"""

from __future__ import annotations


def warmup_shell_command() -> str:
    return """
set -euo pipefail
cd /blast/blastdb
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
log "START shard=${ELB_SHARD_IDX} db=${ELB_DB} node=$(hostname)"
EXPECTED_SOURCE_VERSION="${ELB_DB_SOURCE_VERSION:-}"
if find . -maxdepth 1 -name '.azDownload-*' | grep -q .; then
    log "CLEANUP partial downloads"
    find . -maxdepth 1 -name '.azDownload-*' -exec rm -rf {} +
fi
valid_nsq_count=$(find . -maxdepth 1 -name '*.nsq' ! -name '.azDownload-*' | wc -l)
if [ -f .download-complete ] && [ "$valid_nsq_count" = "0" ]; then
    log "CACHE_INCOMPLETE missing nucleotide volume files"
    rm -f .download-complete
fi
if [ -f .download-complete ] && [ -n "$EXPECTED_SOURCE_VERSION" ]; then
    if [ ! -f .download-source-version ]; then
        log "CACHE_STALE missing source-version marker"
        rm -f .download-complete
    elif [ "$(cat .download-source-version)" != "$EXPECTED_SOURCE_VERSION" ]; then
        log "CACHE_STALE source-version mismatch"
        rm -f .download-complete
    fi
fi
# Integrity gate: the file-presence checks above catch a MISSING volume, but a
# cache whose volume files exist yet disagree with the alias/LMDB metadata
# (a partially-overwritten or truncated prior download) passes them and then
# fails the search with "Input db vol does not match lmdb vol". blastdbcmd -info
# reads exactly that vol<->lmdb<->alias consistency, so a failing probe means
# the staged DB is corrupt: invalidate the marker to force a clean re-download
# instead of skipping onto a broken cache. A healthy cache probes in well under
# a second (local metadata only).
if [ -f .download-complete ]; then
    if ! blastdbcmd -db "$ELB_DB" -info >/dev/null 2>&1; then
        log "CACHE_CORRUPT blastdbcmd integrity probe failed - invalidating"
        rm -f .download-complete
    fi
fi
if [ ! -f .download-complete ]; then
  /scripts/init-db-shard-aks.sh
  partials=$(find . -maxdepth 1 -name '.azDownload-*' | wc -l)
  if [ "$partials" != "0" ]; then
    log "ERROR partial downloads remain: $partials"
    exit 1
  fi
    nsq_count=$(find . -maxdepth 1 -name '*.nsq' ! -name '.azDownload-*' | wc -l)
  if [ "$nsq_count" = "0" ]; then
    log "ERROR no nucleotide volume files downloaded"
    exit 1
  fi
    if [ ! -s taxdb.btd ] || [ ! -s taxdb.bti ]; then
        log "TAXDB_SKIP taxdb files not present in DB prefix"
    fi
    printf '%s' ok > .download-complete
    if [ -n "$EXPECTED_SOURCE_VERSION" ]; then
        printf '%s' "$EXPECTED_SOURCE_VERSION" > .download-source-version
    fi
else
  log "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}"
  # Persistent-cache (node_disk / data_disk) restart path. The shard survived
  # an `az aks stop`/`start` on the node disk, so azcopy was skipped — which
  # means the download's page-cache side effect did NOT happen and node RAM is
  # cold. Read the shard volumes into the node page cache HERE, off the first
  # BLAST search's critical path, so the first query does not pay the full
  # disk->RAM fault cost inside the search pod. This is self-adapting: on a
  # genuinely cold cache (node_disk restart) vmtouch does real work; on an
  # already-warm cache (a re-run in the same node lifecycle) it is a fast noop.
  # Best-effort — a vmtouch failure never fails staging. Opt out with
  # ELB_WARMUP_VMTOUCH_DISABLE=1.
  if [ "${ELB_WARMUP_VMTOUCH_DISABLE:-0}" = "1" ]; then
    log "VMTOUCH_SKIP disabled via ELB_WARMUP_VMTOUCH_DISABLE"
  elif ! command -v vmtouch >/dev/null 2>&1 || ! command -v blastdb_path >/dev/null 2>&1; then
    # Without vmtouch/blastdb_path this warm is impossible; log it so a silent
    # no-op on an image that lacks the tools is visible to operators instead of
    # leaving them to wonder why the first search is still cold.
    log "VMTOUCH_SKIP vmtouch/blastdb_path not available in warmup image"
  else
    vm_start=$(date +%s)
    # vmtouch -m caps the per-FILE size it will touch (skips any single volume
    # larger than the cap), not a cumulative budget; 60% of MemAvailable leaves
    # any realistic GB-scale volume well under the cap. Floor at >=1G and fall
    # back to a fixed budget when MemAvailable is absent/zero so the warm never
    # degrades to a silent `-m 0G` / `-m ''` noop. Mirrors the search-pod
    # vmtouch step in terminal/patch_elastic_blast.py.
    vm_gib=$(awk '/MemAvailable/ {print int($2/1024/1024*0.6)}' /proc/meminfo)
    [ "${vm_gib:-0}" -ge 1 ] 2>/dev/null || vm_gib=4
    vm_budget="${vm_gib}G"
    vm_mol="${ELB_DB_MOL_TYPE:-nucl}"
    vm_paths=$(blastdb_path -dbtype "$vm_mol" -db "$ELB_DB" -getvolumespath 2>/dev/null || true)
    if [ -n "$vm_paths" ]; then
      log "VMTOUCH_WARM shard=${ELB_SHARD_IDX} db=${ELB_DB} budget=${vm_budget}"
      printf '%s' "$vm_paths" | tr ' ' '\n' | xargs -r -n1 vmtouch -tqm "$vm_budget" || true
      vm_end=$(date +%s)
      log "RUNTIME vmtouch-warm-shard-${ELB_SHARD_IDX} $((vm_end - vm_start)) seconds"
    else
      log "VMTOUCH_SKIP could not resolve volume paths for ${ELB_DB}"
    fi
  fi
fi
blastdbcmd -db "$ELB_DB" -info | tee warmup-db-info.txt
log "STAGING_COMPLETE shard=${ELB_SHARD_IDX}"
log "DONE shard=${ELB_SHARD_IDX} size=$(du -sh . | cut -f1)"
""".strip()


INIT_DB_SHARD_AKS_SCRIPT = r"""
#!/bin/bash
set -euo pipefail

echo "BASH version ${BASH_VERSION}"
echo "Shard download: idx=${ELB_SHARD_IDX} prefix=${ELB_PARTITION_PREFIX} db=${ELB_DB}"

cd "${ELB_BLASTDB_DIR:-/blast/blastdb}"

EXPECTED_SOURCE_VERSION="${ELB_DB_SOURCE_VERSION:-}"
if find . -maxdepth 1 -name '.azDownload-*' | grep -q .; then
    echo "CLEANUP partial downloads"
    find . -maxdepth 1 -name '.azDownload-*' -exec rm -rf {} +
fi
valid_nsq_count=$(find . -maxdepth 1 -name '*.nsq' ! -name '.azDownload-*' | wc -l)
if [ -f .download-complete ] && [ "$valid_nsq_count" = "0" ]; then
    echo "CACHE_INCOMPLETE missing nucleotide volume files"
    rm -f .download-complete
fi
if [ -f .download-complete ] && [ -n "$EXPECTED_SOURCE_VERSION" ]; then
    if [ ! -f .download-source-version ]; then
        echo "CACHE_STALE missing source-version marker"
        rm -f .download-complete
    elif [ "$(cat .download-source-version)" != "$EXPECTED_SOURCE_VERSION" ]; then
        echo "CACHE_STALE source-version mismatch"
        rm -f .download-complete
    fi
fi
# Integrity gate (see the warmup entrypoint): file-presence checks miss a cache
# whose volumes exist but disagree with the alias/LMDB metadata, which fails the
# search with "Input db vol does not match lmdb vol". blastdbcmd -info reads that
# vol<->lmdb<->alias consistency; a failing probe means the staged DB is corrupt,
# so invalidate the marker and re-download rather than skip onto a broken cache.
if [ -f .download-complete ]; then
    if ! blastdbcmd -db "$ELB_DB" -info >/dev/null 2>&1; then
        echo "CACHE_CORRUPT blastdbcmd integrity probe failed - invalidating"
        rm -f .download-complete
    fi
fi
if [ -f .download-complete ]; then
    echo "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}"
    exit 0
fi

start=$(date +%s)
log_runtime() {
    local ts
    ts=$(date +'%F %T')
    printf '%s RUNTIME %s %f seconds\n' "$ts" "$1" "$2"
}

azcopy login --identity || { echo "ERROR: azcopy login failed"; exit 1; }
# Do not pin AZCOPY_CONCURRENCY_VALUE / AZCOPY_BUFFER_GB inside the script. The
# production warmup task injects a bounded concurrency default through the Job
# environment, while operators can override it with WARMUP_AZCOPY_CONCURRENCY.
# Keeping policy at Job creation also leaves this reusable script compatible
# with direct benchmark plans that intentionally omit the env vars and let
# azcopy auto-tune.

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
    --overwrite=ifSourceNewer \
    --log-level=WARNING

find . -maxdepth 1 -name '.azDownload-*' -exec rm -rf {} +

end=$(date +%s)
log_runtime "download-shard-${ELB_SHARD_IDX}" $((end - start))

nsq_count=$(find . -maxdepth 1 -name '*.nsq' ! -name '.azDownload-*' | wc -l)
echo "DB files downloaded: ${nsq_count} .nsq files"
echo "Total size: $(du -sh . 2>/dev/null | cut -f1)"
if [ "$nsq_count" = "0" ]; then
    echo "ERROR: no nucleotide volume files downloaded"
    exit 1
fi
if [ ! -s taxdb.btd ] || [ ! -s taxdb.bti ]; then
    echo "TAXDB_SKIP taxdb files not present in DB prefix"
fi

VOLPATHS=""
for VOL in $VOLUMES; do
    [ -n "$VOLPATHS" ] && VOLPATHS="$VOLPATHS "
    VOLPATHS="${VOLPATHS}$(pwd)/${VOL}"
done
echo "VOLPATHS=${VOLPATHS}" > /tmp/shard_volpaths.txt
echo "Volume paths: ${VOLPATHS}"
printf '%s' ok > .download-complete
if [ -n "$EXPECTED_SOURCE_VERSION" ]; then
    printf '%s' "$EXPECTED_SOURCE_VERSION" > .download-source-version
fi
pkill -f azcopy 2>/dev/null || true
rm -rf /root/.azcopy 2>/dev/null || true
""".strip()


BLAST_VMTOUCH_AKS_SCRIPT = r"""
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


__all__ = (
    "BLAST_VMTOUCH_AKS_SCRIPT",
    "INIT_DB_SHARD_AKS_SCRIPT",
    "warmup_shell_command",
)

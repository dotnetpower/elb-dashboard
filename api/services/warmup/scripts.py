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
scripts that exec it directly): pages staged by ``azcopy`` already sit in
the OS page cache as a side effect of the download, and with no mmap
holder process in the warmup pod the vmtouch step was a 1-second noop on
already-cached pages — see [docs/features_change/2026-06/].
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
# Do NOT pin AZCOPY_CONCURRENCY_VALUE / AZCOPY_BUFFER_GB here. When the warmup
# Job does not inject them (the default), leaving them unset lets azcopy use its
# own CPU-based auto-tuning (16 * vCPU, capped at 300, with dynamic CPU
# feedback). A live benchmark (Standard_E16s_v5, core_nt, 256 MiB blocks)
# measured the old hard-coded concurrency=16 at 158 MB/s vs azcopy auto (256
# connections) at 281 MB/s — a 1.78x speedup. Operators can still override via
# the Job env (WARMUP_AZCOPY_CONCURRENCY / WARMUP_AZCOPY_BUFFER_GB on the
# worker), which the Job injects as these env vars and azcopy honours.

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

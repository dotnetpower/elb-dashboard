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
ORIG_DB="$ELB_DB"
if [[ "$ELB_DB" =~ ^(.+)_shard_([0-9]+)$ ]]; then
    ORIG_DB="${BASH_REMATCH[1]}"
fi
if [[ ! "$ELB_DB" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}$ ]]; then
    echo "ERROR: unsafe shard DB name for cache markers: ${ELB_DB}"
    exit 64
fi
CACHE_COMPLETE=".elb-cache.${ELB_DB}.complete"
STAGE_LOCK_WAIT_SECONDS="${ELB_STAGE_LOCK_TIMEOUT_SECONDS:-2400}"
case "$STAGE_LOCK_WAIT_SECONDS" in
    ''|*[!0-9]*) log "ERROR invalid stage lock timeout: ${STAGE_LOCK_WAIT_SECONDS}"; exit 64 ;;
esac
if [ "${#STAGE_LOCK_WAIT_SECONDS}" -gt 4 ] \
    || [ "$STAGE_LOCK_WAIT_SECONDS" -lt 1 ] \
    || [ "$STAGE_LOCK_WAIT_SECONDS" -gt 5400 ]; then
    log "ERROR stage lock timeout must be between 1 and 5400 seconds"
    exit 64
fi
if ! command -v flock >/dev/null 2>&1; then
    log "ERROR flock is required for safe node-local DB staging"
    exit 69
fi
STAGE_LOCK_FILE=".elb-stage.lock"
exec 9>"$STAGE_LOCK_FILE"
log "STAGE_LOCK_WAIT file=${STAGE_LOCK_FILE} timeout=${STAGE_LOCK_WAIT_SECONDS}s"
STAGE_LOCK_WAIT_STARTED=$(date +%s)
if ! flock -w "$STAGE_LOCK_WAIT_SECONDS" 9; then
    STAGE_LOCK_WAIT_ELAPSED=$(( $(date +%s) - STAGE_LOCK_WAIT_STARTED ))
    log "ERROR stage lock timeout file=${STAGE_LOCK_FILE} waited_seconds=${STAGE_LOCK_WAIT_ELAPSED}"
    exit 75
fi
export ELB_STAGE_LOCK_HELD=1
STAGE_LOCK_WAIT_ELAPSED=$(( $(date +%s) - STAGE_LOCK_WAIT_STARTED ))
log "STAGE_LOCK_ACQUIRED file=${STAGE_LOCK_FILE} waited_seconds=${STAGE_LOCK_WAIT_ELAPSED}"
/scripts/init-db-shard-aks.sh
if [ ! -f /tmp/elb-stage-result ]; then
    log "ERROR staging helper did not report downloaded/skipped result"
    exit 1
fi
STAGE_RESULT=$(cat /tmp/elb-stage-result)
if [ "$STAGE_RESULT" = "downloaded" ]; then
  partials=$(find . -maxdepth 1 -name '.azDownload-*' | wc -l)
  if [ "$partials" != "0" ]; then
    log "ERROR partial downloads remain: $partials"
    exit 1
  fi
    payload_ext="nsq"
    if [ "${ELB_DB_MOL_TYPE:-nucl}" = "prot" ]; then
        payload_ext="psq"
    fi
    payload_count=$(find . -maxdepth 1 -name "*.${payload_ext}" ! -name '.azDownload-*' | wc -l)
    if [ "$payload_count" = "0" ]; then
        log "ERROR no ${payload_ext} volume files downloaded"
    exit 1
  fi
    if [ ! -f "$CACHE_COMPLETE" ]; then
        log "ERROR staging helper did not commit completion marker"
        exit 1
    fi
elif [ "$STAGE_RESULT" = "skipped" ]; then
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
else
    log "ERROR invalid staging helper result: ${STAGE_RESULT}"
    exit 1
fi
if ! blastdbcmd -db "$ELB_DB" -info | tee warmup-db-info.txt; then
    rm -f "$CACHE_COMPLETE"
    log "ERROR final blastdbcmd integrity probe failed"
    exit 1
fi
log "STAGING_COMPLETE shard=${ELB_SHARD_IDX}"
log "DONE shard=${ELB_SHARD_IDX} size=$(du -sh . | cut -f1)"
""".strip()


INIT_DB_SHARD_AKS_SCRIPT = r"""
#!/bin/bash
set -euo pipefail

echo "BASH version ${BASH_VERSION}"
echo "Shard download: idx=${ELB_SHARD_IDX} prefix=${ELB_PARTITION_PREFIX} db=${ELB_DB}"

cd "${ELB_BLASTDB_DIR:-/blast/blastdb}"

ORIG_DB="$ELB_DB"
if [[ "$ELB_DB" =~ ^(.+)_shard_([0-9]+)$ ]]; then
    ORIG_DB="${BASH_REMATCH[1]}"
fi
if [[ ! "$ELB_DB" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}$ ]]; then
    echo "ERROR: unsafe shard DB name for cache markers: ${ELB_DB}"
    exit 64
fi
CACHE_COMPLETE=".elb-cache.${ELB_DB}.complete"
CACHE_SOURCE_VERSION=".elb-cache.${ELB_DB}.source-version"
CACHE_MANIFEST=".elb-cache.${ELB_DB}.manifest"
CACHE_LAYOUT_SHA=".elb-cache.${ELB_DB}.layout-sha256"
if [ "${ELB_STAGE_LOCK_HELD:-0}" = "1" ]; then
    if ! flock -n 9; then
        echo "ERROR: inherited stage lock descriptor is unavailable"
        exit 70
    fi
    echo "STAGE_LOCK_REUSE file=.elb-stage.lock"
else
    STAGE_LOCK_WAIT_SECONDS="${ELB_STAGE_LOCK_TIMEOUT_SECONDS:-2400}"
    case "$STAGE_LOCK_WAIT_SECONDS" in
      ''|*[!0-9]*) echo "ERROR: invalid stage lock timeout: ${STAGE_LOCK_WAIT_SECONDS}"; exit 64 ;;
    esac
        if [ "${#STAGE_LOCK_WAIT_SECONDS}" -gt 4 ] \
                || [ "$STAGE_LOCK_WAIT_SECONDS" -lt 1 ] \
                || [ "$STAGE_LOCK_WAIT_SECONDS" -gt 5400 ]; then
                echo "ERROR: stage lock timeout must be between 1 and 5400 seconds"
                exit 64
        fi
    if ! command -v flock >/dev/null 2>&1; then
        echo "ERROR: flock is required for safe node-local DB staging"
        exit 69
    fi
    STAGE_LOCK_FILE=".elb-stage.lock"
    exec 9>"$STAGE_LOCK_FILE"
    echo "STAGE_LOCK_WAIT file=${STAGE_LOCK_FILE} timeout=${STAGE_LOCK_WAIT_SECONDS}s"
    STAGE_LOCK_WAIT_STARTED=$(date +%s)
    if ! flock -w "$STAGE_LOCK_WAIT_SECONDS" 9; then
        STAGE_LOCK_WAIT_ELAPSED=$(( $(date +%s) - STAGE_LOCK_WAIT_STARTED ))
        echo "ERROR: stage lock timeout file=${STAGE_LOCK_FILE}" \
            "waited_seconds=${STAGE_LOCK_WAIT_ELAPSED}"
        exit 75
    fi
    export ELB_STAGE_LOCK_HELD=1
    STAGE_LOCK_WAIT_ELAPSED=$(( $(date +%s) - STAGE_LOCK_WAIT_STARTED ))
    echo "STAGE_LOCK_ACQUIRED file=${STAGE_LOCK_FILE}" \
        "waited_seconds=${STAGE_LOCK_WAIT_ELAPSED}"
fi

if [ -f .download-complete ]; then
    echo "CACHE_MIGRATE invalidating legacy global completion marker"
    rm -f .download-complete
fi

rm -f /tmp/elb-stage-result

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
LAYOUT_URL="${SHARD_URL}${ELB_DB}.layout"
echo "Downloading manifest: ${MANIFEST_URL}"
retry_azcopy cp "${MANIFEST_URL}" /tmp/manifest.txt --log-level=ERROR || {
    echo "ERROR: manifest download failed"
    exit 1
}
retry_azcopy cp "${NAL_URL}" /tmp/shard.nal --log-level=ERROR || {
    echo "ERROR: shard alias download failed"
    rm -f "$CACHE_COMPLETE"
    exit 1
}

valid_volume_name() {
    local volume="$1" suffix
    if [ "$volume" = "$ORIG_DB" ]; then
        return 0
    fi
    if [[ "$volume" != "$ORIG_DB".* ]]; then
        return 1
    fi
    suffix="${volume#"$ORIG_DB"}"
    [[ "$suffix" =~ ^\.[0-9]+$ ]]
}

mapfile -t VOLUMES < /tmp/manifest.txt
if [ "${#VOLUMES[@]}" -lt 1 ]; then
    echo "ERROR: shard manifest is empty"
    rm -f "$CACHE_COMPLETE"
    exit 65
fi
declare -A SEEN_VOLUMES=()
for volume in "${VOLUMES[@]}"; do
    if ! valid_volume_name "$volume"; then
        echo "ERROR: invalid volume name in shard manifest: ${volume}"
        rm -f "$CACHE_COMPLETE"
        exit 65
    fi
    if [ -n "${SEEN_VOLUMES[$volume]+x}" ]; then
        echo "ERROR: duplicate volume name in shard manifest: ${volume}"
        rm -f "$CACHE_COMPLETE"
        exit 65
    fi
    SEEN_VOLUMES["$volume"]=1
done
echo "Volumes: ${VOLUMES[*]}"

DB_BASE_URL=$(echo "${ELB_PARTITION_PREFIX}" | sed 's|/[^/]*/[^/]*$|/|')
DB_URL="${ELB_DB_URL:-${DB_BASE_URL}${ORIG_DB}/}"
echo "DB base URL: ${DB_URL}"

EXPECTED_SOURCE_VERSION="${ELB_DB_SOURCE_VERSION:-}"
METADATA_SOURCE_VERSION=""
SHARD_LAYOUT_SCHEMA="0"
METADATA_URL="${ELB_METADATA_URL:-${DB_BASE_URL}${ORIG_DB}-metadata.json}"
echo "Resolving DB metadata: ${METADATA_URL}"
if retry_azcopy cp "${METADATA_URL}" /tmp/db-metadata.json --log-level=ERROR; then
    if command -v python3 >/dev/null 2>&1; then
        METADATA_SOURCE_VERSION=$(python3 -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(str(json.load(handle).get("source_version") or ""))
' /tmp/db-metadata.json 2>/dev/null || true)
        SHARD_LAYOUT_SCHEMA=$(python3 -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle).get("shard_layout_schema", 0)
print(value if type(value) is int else "invalid")
' /tmp/db-metadata.json 2>/dev/null || printf invalid)
    else
        METADATA_SOURCE_VERSION=$(sed -n \
            's/.*"source_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            /tmp/db-metadata.json | head -1)
        SHARD_LAYOUT_SCHEMA=$(sed -n \
            's/.*"shard_layout_schema"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
            /tmp/db-metadata.json | head -1)
        SHARD_LAYOUT_SCHEMA="${SHARD_LAYOUT_SCHEMA:-0}"
    fi
else
    echo "ERROR: DB metadata lookup failed after retries; refusing unversioned shard staging"
    rm -f "$CACHE_COMPLETE"
    exit 75
fi
case "$SHARD_LAYOUT_SCHEMA" in
    ''|*[!0-9]*)
        echo "ERROR: invalid shard_layout_schema: ${SHARD_LAYOUT_SCHEMA}"
        rm -f "$CACHE_COMPLETE"
        exit 65
        ;;
esac
if [ "${#SHARD_LAYOUT_SCHEMA}" -gt 2 ] || [ "$SHARD_LAYOUT_SCHEMA" -gt 1 ]; then
    echo "ERROR: unsupported shard_layout_schema: ${SHARD_LAYOUT_SCHEMA}"
    rm -f "$CACHE_COMPLETE"
    exit 65
fi
if [ -n "$EXPECTED_SOURCE_VERSION" ]; then
    if [ -z "$METADATA_SOURCE_VERSION" ]; then
        echo "ERROR: DB metadata is missing source_version required by this Job"
        rm -f "$CACHE_COMPLETE"
        exit 75
    fi
    if [ "$EXPECTED_SOURCE_VERSION" != "$METADATA_SOURCE_VERSION" ]; then
        echo "ERROR: DB source version changed after Job creation" \
            "expected=${EXPECTED_SOURCE_VERSION} actual=${METADATA_SOURCE_VERSION}"
        rm -f "$CACHE_COMPLETE"
        exit 75
    fi
fi
if [ -z "$EXPECTED_SOURCE_VERSION" ]; then
    EXPECTED_SOURCE_VERSION="$METADATA_SOURCE_VERSION"
fi
if [ -n "$EXPECTED_SOURCE_VERSION" ]; then
    echo "DB source version: ${EXPECTED_SOURCE_VERSION}"
else
    echo "WARNING: DB metadata did not contain source_version"
fi

LAYOUT_AVAILABLE="0"
rm -f /tmp/shard-layout.txt
if [ "$SHARD_LAYOUT_SCHEMA" -ge 1 ]; then
    if ! retry_azcopy cp "${LAYOUT_URL}" /tmp/shard-layout.txt --log-level=ERROR; then
        echo "ERROR: schema ${SHARD_LAYOUT_SCHEMA} requires shard layout metadata"
        rm -f "$CACHE_COMPLETE"
        exit 65
    fi
elif ! azcopy cp "${LAYOUT_URL}" /tmp/shard-layout.txt --log-level=ERROR; then
    echo "WARNING: LEGACY_LAYOUT no authoritative disk-size metadata; preflight is degraded"
    rm -f /tmp/shard-layout.txt
fi
if [ -f /tmp/shard-layout.txt ]; then
    layout_extra=""
    if ! read -r EXPECTED_LAYOUT_SHA REQUIRED_BYTES layout_extra < /tmp/shard-layout.txt \
            || [ -n "$layout_extra" ]; then
        echo "ERROR: malformed shard layout metadata"
        rm -f "$CACHE_COMPLETE"
        exit 65
    fi
    case "$EXPECTED_LAYOUT_SHA" in
    *[!0-9a-f]*) echo "ERROR: invalid shard layout digest"; rm -f "$CACHE_COMPLETE"; exit 65 ;;
    esac
    case "$REQUIRED_BYTES" in
    ''|*[!0-9]*) echo "ERROR: invalid shard required_bytes"; rm -f "$CACHE_COMPLETE"; exit 65 ;;
    esac
    if [ "${#EXPECTED_LAYOUT_SHA}" -ne 64 ] \
            || [ "${#REQUIRED_BYTES}" -gt 18 ] \
            || [ "$REQUIRED_BYTES" -lt 1 ]; then
        echo "ERROR: shard layout metadata values are out of range"
        rm -f "$CACHE_COMPLETE"
        exit 65
    fi
    if ! command -v sha256sum >/dev/null 2>&1; then
        echo "ERROR: sha256sum is required for shard layout validation"
        rm -f "$CACHE_COMPLETE"
        exit 69
    fi
    ACTUAL_LAYOUT_SHA=$( \
        { cat /tmp/manifest.txt; printf '\0'; cat /tmp/shard.nal; } \
        | sha256sum | awk '{print $1}'
    )
    if [ "$ACTUAL_LAYOUT_SHA" != "$EXPECTED_LAYOUT_SHA" ]; then
        echo "ERROR: shard layout digest mismatch"
        rm -f "$CACHE_COMPLETE"
        exit 65
    fi
    LAYOUT_AVAILABLE="1"
    echo "LAYOUT_VERIFIED sha256=${EXPECTED_LAYOUT_SHA} required_bytes=${REQUIRED_BYTES}"
fi

write_volpaths() {
    local volpaths=""
    for volume in "${VOLUMES[@]}"; do
        [ -n "$volpaths" ] && volpaths="$volpaths "
        volpaths="${volpaths}$(pwd)/${volume}"
    done
    echo "VOLPATHS=${volpaths}" > /tmp/shard_volpaths.txt
    echo "Volume paths: ${volpaths}"
}

commit_layout_markers() {
    cp /tmp/manifest.txt "${CACHE_MANIFEST}.tmp"
    mv "${CACHE_MANIFEST}.tmp" "$CACHE_MANIFEST"
    if [ "$LAYOUT_AVAILABLE" = "1" ]; then
        printf '%s' "$EXPECTED_LAYOUT_SHA" > "${CACHE_LAYOUT_SHA}.tmp"
        mv "${CACHE_LAYOUT_SHA}.tmp" "$CACHE_LAYOUT_SHA"
    else
        rm -f "$CACHE_LAYOUT_SHA"
    fi
}

rm -f "${CACHE_COMPLETE}.tmp" "${CACHE_SOURCE_VERSION}.tmp" \
    "${CACHE_LAYOUT_SHA}.tmp" "${CACHE_MANIFEST}.tmp" "./${ELB_DB}.nal.tmp"
if [ -f "$CACHE_COMPLETE" ] && [ -z "$EXPECTED_SOURCE_VERSION" ]; then
    echo "CACHE_UNVERIFIED expected source version is unavailable"
    rm -f "$CACHE_COMPLETE"
fi
if find . -maxdepth 1 -name '.azDownload-*' | grep -q .; then
    echo "CLEANUP partial downloads"
    find . -maxdepth 1 -name '.azDownload-*' -exec rm -rf {} +
fi

payload_ext="nsq"
if [ "${ELB_DB_MOL_TYPE:-nucl}" = "prot" ]; then
    payload_ext="psq"
fi
missing_volume="0"
if [ -f "$CACHE_COMPLETE" ]; then
    for volume in "${VOLUMES[@]}"; do
        if [ ! -s "${volume}.${payload_ext}" ]; then
            missing_volume="1"
            echo "CACHE_INCOMPLETE missing ${volume}.${payload_ext}"
        fi
    done
    if [ "$missing_volume" != "0" ]; then
        rm -f "$CACHE_COMPLETE"
    fi
fi
if [ -f "$CACHE_COMPLETE" ] && [ -s "${ORIG_DB}.ntf" ] \
    && { [ ! -s "${ORIG_DB}.not" ] || [ ! -s "${ORIG_DB}.nos" ]; }; then
    echo "CACHE_INCOMPLETE missing taxonomy filter index ${ORIG_DB}.not/.nos"
    rm -f "$CACHE_COMPLETE"
fi
if [ -f "$CACHE_COMPLETE" ] && [ -n "$EXPECTED_SOURCE_VERSION" ]; then
    if [ ! -f "$CACHE_SOURCE_VERSION" ]; then
        echo "CACHE_STALE missing source-version marker"
        rm -f "$CACHE_COMPLETE"
    elif [ "$(cat "$CACHE_SOURCE_VERSION")" != "$EXPECTED_SOURCE_VERSION" ]; then
        echo "CACHE_STALE source-version mismatch"
        rm -f "$CACHE_COMPLETE"
    fi
fi
if [ -f "$CACHE_COMPLETE" ]; then
    if [ ! -f "$CACHE_MANIFEST" ]; then
        echo "CACHE_STALE missing shard-manifest marker"
        rm -f "$CACHE_COMPLETE"
    elif ! cmp -s /tmp/manifest.txt "$CACHE_MANIFEST"; then
        echo "CACHE_STALE shard manifest mismatch"
        rm -f "$CACHE_COMPLETE"
    fi
fi
if [ -f "$CACHE_COMPLETE" ]; then
    if [ ! -f "./${ELB_DB}.nal" ]; then
        echo "CACHE_STALE missing shard alias"
        rm -f "$CACHE_COMPLETE"
    elif ! cmp -s /tmp/shard.nal "./${ELB_DB}.nal"; then
        echo "CACHE_STALE shard alias mismatch"
        rm -f "$CACHE_COMPLETE"
    fi
fi
if [ -f "$CACHE_COMPLETE" ] && [ "$LAYOUT_AVAILABLE" = "1" ]; then
    if [ ! -f "$CACHE_LAYOUT_SHA" ]; then
        echo "CACHE_STALE missing shard-layout marker"
        rm -f "$CACHE_COMPLETE"
    elif [ "$(cat "$CACHE_LAYOUT_SHA")" != "$EXPECTED_LAYOUT_SHA" ]; then
        echo "CACHE_STALE shard layout mismatch"
        rm -f "$CACHE_COMPLETE"
    fi
fi
if [ -f "$CACHE_COMPLETE" ]; then
    if ! blastdbcmd -db "$ELB_DB" -info >/dev/null 2>&1; then
        echo "CACHE_CORRUPT blastdbcmd integrity probe failed - invalidating"
        rm -f "$CACHE_COMPLETE"
    fi
fi
if [ -f "$CACHE_COMPLETE" ]; then
    echo "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}"
    commit_layout_markers
    write_volpaths
    printf '%s' skipped > /tmp/elb-stage-result
    exit 0
fi

remove_volume_payloads() {
    local volume candidate
    for volume in "$@"; do
        for candidate in "${volume}".*; do
            if [ -e "$candidate" ] || [ -L "$candidate" ]; then
                rm -f -- "$candidate"
            fi
        done
    done
}
remove_volume_payloads "${VOLUMES[@]}"
for previous_manifest in "$CACHE_MANIFEST" .download-manifest; do
    if [ -f "$previous_manifest" ]; then
        mapfile -t PREVIOUS_VOLUMES < "$previous_manifest"
        for previous_volume in "${PREVIOUS_VOLUMES[@]}"; do
            if valid_volume_name "$previous_volume"; then
                remove_volume_payloads "$previous_volume"
            else
                echo "WARNING: ignoring unsafe volume in previous cache manifest"
            fi
        done
    fi
done
# Shared taxonomy files have no DB prefix and may still be required by another
# prepared database in this flat node-local cache. Preserve them here; the
# transfer below overwrites them when the current DB prefix supplies a newer
# authoritative copy.
rm -f -- "${ORIG_DB}.ndb" "${ORIG_DB}.ntf" "${ORIG_DB}.nto" \
    "${ORIG_DB}.nos" "${ORIG_DB}.not"

if [ "$LAYOUT_AVAILABLE" = "1" ]; then
    RESERVE_BYTES="${ELB_STAGE_DISK_RESERVE_BYTES:-}"
    if [ -z "$RESERVE_BYTES" ]; then
        RESERVE_BYTES=$(( REQUIRED_BYTES / 20 ))
        if [ "$RESERVE_BYTES" -lt 1073741824 ]; then
            RESERVE_BYTES=1073741824
        fi
    fi
    case "$RESERVE_BYTES" in
      ''|*[!0-9]*) echo "ERROR: invalid ELB_STAGE_DISK_RESERVE_BYTES"; exit 64 ;;
    esac
    if [ "${#RESERVE_BYTES}" -gt 18 ]; then
        echo "ERROR: ELB_STAGE_DISK_RESERVE_BYTES is out of range"
        exit 64
    fi
    AVAILABLE_BYTES=$(df -B1 --output=avail . | tail -n 1 | tr -d '[:space:]')
    case "$AVAILABLE_BYTES" in
      ''|*[!0-9]*) echo "ERROR: unable to determine node-local available bytes"; exit 74 ;;
    esac
    TOTAL_REQUIRED_BYTES=$(( REQUIRED_BYTES + RESERVE_BYTES ))
    if [ "$TOTAL_REQUIRED_BYTES" -lt "$REQUIRED_BYTES" ]; then
        echo "ERROR: disk preflight byte calculation overflow"
        exit 65
    fi
    echo "DISK_PREFLIGHT required_bytes=${REQUIRED_BYTES}" \
        "reserve_bytes=${RESERVE_BYTES} available_bytes=${AVAILABLE_BYTES}"
    if [ "$AVAILABLE_BYTES" -lt "$TOTAL_REQUIRED_BYTES" ]; then
        echo "ERROR: insufficient node-local disk required_bytes=${REQUIRED_BYTES}" \
            "reserve_bytes=${RESERVE_BYTES} available_bytes=${AVAILABLE_BYTES};" \
            "free node disk space or use a larger node OS disk"
        exit 28
    fi
else
    echo "WARNING: DISK_PREFLIGHT_SKIP legacy shard layout has no authoritative byte count"
fi

PATTERN=""
for VOL in "${VOLUMES[@]}"; do
    [ -n "$PATTERN" ] && PATTERN="${PATTERN};"
    PATTERN="${PATTERN}${VOL}.*"
done
PATTERN="${PATTERN};taxdb.btd;taxdb.bti;taxonomy4blast.sqlite3;${ORIG_DB}.ndb;${ORIG_DB}.ntf;${ORIG_DB}.nto;${ORIG_DB}.nos;${ORIG_DB}.not"
echo "Downloading with pattern: ${PATTERN}"

retry_azcopy cp "${DB_URL}*" . \
    --include-pattern "${PATTERN}" \
    --block-size-mb=256 \
    --overwrite=true \
    --log-level=WARNING

find . -maxdepth 1 -name '.azDownload-*' -exec rm -rf {} +

end=$(date +%s)
log_runtime "download-shard-${ELB_SHARD_IDX}" $((end - start))

payload_count=$(find . -maxdepth 1 -name "*.${payload_ext}" ! -name '.azDownload-*' | wc -l)
echo "DB files downloaded: ${payload_count} .${payload_ext} files"
echo "Total size: $(du -sh . 2>/dev/null | cut -f1)"
if [ "$payload_count" = "0" ]; then
    echo "ERROR: no ${payload_ext} volume files downloaded"
    exit 1
fi
for volume in "${VOLUMES[@]}"; do
    if [ ! -s "${volume}.${payload_ext}" ]; then
        echo "ERROR: required payload is missing after download: ${volume}.${payload_ext}"
        exit 1
    fi
done
if [ ! -s taxdb.btd ] || [ ! -s taxdb.bti ]; then
    echo "TAXDB_SKIP taxdb files not present in DB prefix"
fi
if [ -s "${ORIG_DB}.ntf" ] \
    && { [ ! -s "${ORIG_DB}.not" ] || [ ! -s "${ORIG_DB}.nos" ]; }; then
    echo "ERROR: downloaded taxonomy filter index is incomplete ${ORIG_DB}.not/.nos"
    exit 1
fi
cp /tmp/shard.nal "./${ELB_DB}.nal.tmp"
mv "./${ELB_DB}.nal.tmp" "./${ELB_DB}.nal"
if ! blastdbcmd -db "$ELB_DB" -info >/dev/null 2>&1; then
    echo "ERROR: downloaded DB failed blastdbcmd integrity probe"
    exit 1
fi

write_volpaths
commit_layout_markers
if [ -n "$EXPECTED_SOURCE_VERSION" ]; then
    printf '%s' "$EXPECTED_SOURCE_VERSION" > "${CACHE_SOURCE_VERSION}.tmp"
    mv "${CACHE_SOURCE_VERSION}.tmp" "$CACHE_SOURCE_VERSION"
else
    rm -f "$CACHE_SOURCE_VERSION"
fi
printf '%s' ok > "${CACHE_COMPLETE}.tmp"
mv "${CACHE_COMPLETE}.tmp" "$CACHE_COMPLETE"
rm -f .download-source-version .download-layout-sha256 .download-manifest
printf '%s' downloaded > /tmp/elb-stage-result
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

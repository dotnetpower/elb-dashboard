#!/usr/bin/env bash
# Shared terminal toolchain base-image helper.
#
# The terminal sidecar is intentionally heavy: Ubuntu, Azure CLI, kubectl,
# azcopy, BLAST+, sequence tools, and the patched elastic-blast runtime. This
# helper tags that stable toolchain layer by content hash so deploy scripts can
# rebuild the thin runtime overlay without reinstalling the world on every
# iteration.

terminal_base_log() {
  if declare -F ts >/dev/null 2>&1; then
    ts "$*"
  else
    printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"
  fi
}

# Default sibling ref the terminal toolchain is built from. This MUST point at
# a commit that still ships the `bin/` launcher scripts: setup.cfg installs the
# `elastic-blast` CLI via `scripts = bin/*`, and upstream commit 72a69822
# ("Remove deprecated scripts") deleted `bin/`. Building from `master` (or any
# ref at/after that commit) yields a venv with the elastic_blast PACKAGE but no
# `elastic-blast` EXECUTABLE, so every dashboard BLAST submit fails inside the
# terminal sidecar with `[Errno 2] No such file or directory: 'elastic-blast'`.
# Keep this in lock-step with the `ARG ELASTIC_BLAST_REF` default in
# terminal/Dockerfile.base; the hash, log, and build below all resolve through
# it so the content tag can never drift from the ref actually built.
_ELASTIC_BLAST_REF_DEFAULT='f4b8b734a82285a18a2ca9aadcbe02759d13f903'

terminal_base_hash() {
  local dockerfile="$REPO_ROOT/terminal/Dockerfile.base"
  {
    sha256sum "$dockerfile"
    printf '\n-- patch_elastic_blast.py --\n'
    sha256sum "$REPO_ROOT/terminal/patch_elastic_blast.py"
    printf '\n-- merge-sharded-results.sh --\n'
    sha256sum "$REPO_ROOT/terminal/merge-sharded-results.sh"
    printf '\n-- KUBECTL_VERSION=%s\n' "${KUBECTL_VERSION:-v1.34.2}"
    printf '\n-- TTYD_VERSION=%s\n' "${TTYD_VERSION:-1.7.7}"
    printf '\n-- ELASTIC_BLAST_REF=%s\n' "${ELASTIC_BLAST_REF:-$_ELASTIC_BLAST_REF_DEFAULT}"
    # Part of the base tag so an ELB_JOB_TTL_SECONDS override re-tags + rebuilds
    # instead of silently reusing a cached base built with the old TTL.
    printf '\n-- ELB_JOB_TTL_SECONDS=%s\n' "${ELB_JOB_TTL_SECONDS:-1800}"
  } | sha256sum | awk '{print substr($1, 1, 16)}'
}

terminal_base_tag() {
  printf 'toolchain-%s' "$(terminal_base_hash)"
}

terminal_base_image() {
  printf '%s/elb-terminal-base:%s' "$ACR_LOGIN_SERVER" "$(terminal_base_tag)"
}

terminal_base_exists() {
  local tag
  tag="$(terminal_base_tag)"
  az acr repository show-tags \
    --name "$ACR_NAME" \
    --repository elb-terminal-base \
    -o tsv 2>/dev/null | grep -Fx "$tag" >/dev/null
}

ensure_terminal_base_image() {
  local tag image log force_rebuild
  tag="$(terminal_base_tag)"
  image="$(terminal_base_image)"
  log="${LOG_DIR:-/tmp}/build-elb-terminal-base.log"
  force_rebuild="${TERMINAL_BASE_REBUILD:-${REBUILD_TERMINAL_BASE:-false}}"
  # Ensure the log directory exists before the `> "$log"` redirect below; a
  # caller-supplied LOG_DIR (e.g. quick-deploy.sh) is not guaranteed to be
  # pre-created, and a failed redirect would otherwise skip the base build
  # entirely (bash aborts the command before `az acr build` runs).
  mkdir -p "$(dirname "$log")" 2>/dev/null || true

  if [[ "$force_rebuild" != "true" && "$force_rebuild" != "1" ]] && terminal_base_exists; then
    terminal_base_log "==> Reusing terminal toolchain base: $image"
    return 0
  fi

  terminal_base_log "==> Building terminal toolchain base: $image"
  terminal_base_log "    log: $log"
  terminal_base_log "    ELASTIC_BLAST_REF=${ELASTIC_BLAST_REF:-$_ELASTIC_BLAST_REF_DEFAULT}"
  az acr build \
    --registry "$ACR_NAME" \
    --image "elb-terminal-base:$tag" \
    --image "elb-terminal-base:latest" \
    --file "$REPO_ROOT/terminal/Dockerfile.base" \
    --build-arg "ELASTIC_BLAST_REF=${ELASTIC_BLAST_REF:-$_ELASTIC_BLAST_REF_DEFAULT}" \
    --build-arg "ELB_JOB_TTL_SECONDS=${ELB_JOB_TTL_SECONDS:-1800}" \
    "$REPO_ROOT/terminal" \
    --output none \
    > "$log" 2>&1
  terminal_base_log "==> Terminal toolchain base ready: $image"
}

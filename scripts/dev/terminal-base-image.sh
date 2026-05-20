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

terminal_base_hash() {
  local dockerfile="$REPO_ROOT/terminal/Dockerfile.base"
  {
    sha256sum "$dockerfile"
    printf '\n-- patch_elastic_blast.py --\n'
    sha256sum "$REPO_ROOT/terminal/patch_elastic_blast.py"
    printf '\n-- merge-sharded-results.sh --\n'
    sha256sum "$REPO_ROOT/terminal/merge-sharded-results.sh"
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

  if [[ "$force_rebuild" != "true" && "$force_rebuild" != "1" ]] && terminal_base_exists; then
    terminal_base_log "==> Reusing terminal toolchain base: $image"
    return 0
  fi

  terminal_base_log "==> Building terminal toolchain base: $image"
  terminal_base_log "    log: $log"
  az acr build \
    --registry "$ACR_NAME" \
    --image "elb-terminal-base:$tag" \
    --image "elb-terminal-base:latest" \
    --file "$REPO_ROOT/terminal/Dockerfile.base" \
    "$REPO_ROOT/terminal" \
    --output none \
    > "$log" 2>&1
  terminal_base_log "==> Terminal toolchain base ready: $image"
}

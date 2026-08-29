#!/usr/bin/env bash
# Quick single-sidecar deploy for the bundled Container App.
#
# When a code-only fix in api/ or web/ or terminal/ needs to land on the
# real Azure revision, running the full postprovision (3 parallel ACR
# builds + a Bicep redeploy of all six sidecars) takes 5-10 minutes. This
# script does a far smaller cycle:
#
#   1. Build ONE image via `az acr build` (cached layers, ~30-90 s).
#   2. Patch ONLY that container's image/resources/runtime env through one
#      template-only ARM transaction (does NOT touch secrets, probes, volumes,
#      or scale rules).
#   3. (Optional) tail the new revision's logs.
#
# It refuses to touch sidecar structure (secrets, probes, volumes) — for
# those changes you still need a Bicep redeploy via postprovision.sh
# or `az deployment group create --template-file containerAppControl.bicep`.
# Frontend runtime config and the server-side guard/platform coordinates below
# are exact-key upserts; unrelated environment values stay untouched.
#
# Control-plane GUARD env exception: api/worker/beat PATCHes also upsert the
# policy toggles from infra/control-plane-env.json (ENFORCE_DASHBOARD_RBAC,
# ENFORCE_OPENAPI_EXEC_RBAC, BLAST_GATE_ENABLED, BLAST_JOBS_SHARED_VISIBILITY,
# STRICT_BLUEGREEN, OPENAPI_ALLOW_PUBLIC_LB). That same JSON is the source
# Bicep loads, so a guard-default change lands on BOTH a full `azd provision`
# AND a fast / GitHub-Actions deploy. All other runtime env stays untouched.
#
# Usage:
#   scripts/dev/quick-deploy.sh <sidecar> [tag]
#
# Sidecars: api | worker | beat | frontend | terminal | all
#   (worker and beat reuse the api image — passing either rebuilds api
#    and points the worker / beat container at the new tag.)
#   (all deploys api, frontend, and terminal in sequence; api also patches
#    worker and beat.)
#
# Examples:
#   scripts/dev/quick-deploy.sh api
#   scripts/dev/quick-deploy.sh all
#   scripts/dev/quick-deploy.sh terminal
#   scripts/dev/quick-deploy.sh frontend custom-tag-123
#   scripts/dev/quick-deploy.sh api --logs        # tail after deploy
#   scripts/dev/quick-deploy.sh all --logs        # tail api logs after all deploys
#   scripts/dev/quick-deploy.sh terminal --rebuild-terminal-base
#
# Required env (export them or `source /tmp/azd-env.sh`):
#   AZURE_RESOURCE_GROUP         e.g. rg-elb-dashboard
#   ACR_NAME                     short name (no .azurecr.io)
#   ACR_LOGIN_SERVER             e.g. crelbXYZ.azurecr.io
#   CONTAINER_APP_NAME           e.g. ca-elb-dashboard

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
. "$REPO_ROOT/scripts/dev/acr-build-access.sh"
. "$REPO_ROOT/scripts/dev/terminal-base-image.sh"
. "$REPO_ROOT/scripts/dev/az-context.sh"

ts() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Control-plane GUARD/POLICY env toggles (single source of truth shared with
# infra/modules/containerAppControl.bicep via infra/control-plane-env.json).
#
# Why: this script patches IMAGES only. Container App env vars otherwise land
# exclusively through a full `azd provision` / postprovision Bicep deploy. So
# a guard default changed in Bicep (e.g. ENFORCE_DASHBOARD_RBAC=true) would
# never reach a fast deploy OR the GitHub Actions deploy.yml path (which also
# calls this script) — a no-RBAC user could still load the dashboard after an
# apparent redeploy. We read the SAME JSON Bicep reads and apply each sidecar's
# values through a fresh-snapshot-checked, template-only ARM PATCH so both
# deploy paths converge to the repo's source of truth without Azure CLI
# silently targeting the default API container.
# ---------------------------------------------------------------------------
CONTROL_PLANE_ENV_FILE="$REPO_ROOT/infra/control-plane-env.json"
CONTAINER_APP_API_VERSION="${CONTAINER_APP_API_VERSION:-2026-01-01}"
[[ "$CONTAINER_APP_API_VERSION" =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2}(-preview)?$ ]] \
  || die "invalid CONTAINER_APP_API_VERSION: $CONTAINER_APP_API_VERSION"

# Guard/policy convergence is part of a deploy, not an optional follow-up.
[[ -f "$CONTROL_PLANE_ENV_FILE" ]] \
  || die "control-plane env file missing: $CONTROL_PLANE_ENV_FILE"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$CONTROL_PLANE_ENV_FILE" \
  || die "control-plane-env: $CONTROL_PLANE_ENV_FILE is not valid JSON"

# Echo `KEY=VALUE` lines for the given sidecar (api|worker|beat|...), or
# nothing when the file is absent or the sidecar has no guard toggles.
#
# Per-deployment override: when a control-plane key is ALSO present in the
# process environment (e.g. exported from azd env), that value wins over the
# repo default in control-plane-env.json. This keeps the repo default OFF for
# opt-in guards (charter §12a Rule 4) while letting a specific deployment pin a
# toggle (e.g. SERVICEBUS_ENABLED=true) so it survives every redeploy instead of
# being reset to the JSON default. Set-vs-unset is tested explicitly (a key
# absent from the environment falls through to the JSON value; an exported empty
# string is honoured as an intentional override) to avoid the `${!key:-}`
# empty-vs-unset bug class.
control_plane_env_pairs() {
  local sidecar="$1"
  [[ -f "$CONTROL_PLANE_ENV_FILE" ]] || return 0
  python3 - "$CONTROL_PLANE_ENV_FILE" "$sidecar" <<'PY'
import json, os, sys
path, sidecar = sys.argv[1], sys.argv[2]
data = json.load(open(path))
section = data.get(sidecar) or {}
for k, v in section.items():
    if k.startswith("_"):
        continue
    # azd-env / process-env override wins when the key is SET (even to ""),
    # otherwise fall back to the repo default from the JSON.
    value = os.environ[k] if k in os.environ else v
    print(f"{k}={value}")
PY
  # Core platform coordinates declared on the same four sidecars in Bicep.
  # Backfill them on every fast patch so an older live template converges
  # instead of leaving runtime maintenance without its ACR or Storage target.
  if [[ "$sidecar" =~ ^(api|worker|beat|terminal)$ ]]; then
    if [[ -n "${ACR_NAME:-}" ]]; then
      printf 'PLATFORM_ACR_NAME=%s\n' "$ACR_NAME"
    fi
    if [[ -n "${STORAGE_ACCOUNT_NAME:-}" ]]; then
      printf 'STORAGE_ACCOUNT_NAME=%s\n' "$STORAGE_ACCOUNT_NAME"
    fi
  fi
  # The Direct/AKS prepare task is emitted by the api but runs in AKS from a
  # dedicated Azure CLI + pinned-azcopy image. Full postprovision wires this
  # value through Bicep; mirror it on fast api deploys so an older live
  # template converges instead of rejecting Direct dispatch as image-missing.
  if [[ "$sidecar" == "api" && -n "${ACR_LOGIN_SERVER:-}" && -n "${TAG:-}" ]]; then
    printf 'PREPARE_DB_AKS_AZCOPY_IMAGE=%s/elb-prepare-db:%s\n' "$ACR_LOGIN_SERVER" "$TAG"
  fi
  # Live Wall queries ContainerAppConsoleLogs_CL through LogsQueryClient,
  # whose workspace_id contract is the customer GUID, not an ARM resource id.
  # az-context resolves that authoritative value from the Container Apps
  # Environment. Upsert it on every api PATCH so an old malformed deployment
  # converges even when the image itself is unchanged.
  if [[ "$sidecar" == "api" && -n "${LOG_ANALYTICS_WORKSPACE_ID:-}" ]]; then
    printf 'LOG_ANALYTICS_WORKSPACE_ID=%s\n' "$LOG_ANALYTICS_WORKSPACE_ID"
  fi
  # Extra M2M wiring for the api sidecar only: when the operator has set
  # AZURE_OPENAPI_SHARED_TOKEN (via `azd env set`), emit an extra pair that
  # references the Container App secret `elb-openapi-api-token`. The secret
  # itself is upserted separately by `sync_openapi_shared_token` right before
  # the PATCH (Bicep also declares the same secret + env for full azd
  # provision paths — quick-deploy just mirrors the same wiring so a fast
  # deploy converges to the same runtime shape). Fail-safe: an unset env
  # var means the pair is omitted entirely and the api container's
  # existing secretRef (if any) is left untouched by the exact-container
  # template patch.
  if [[ "$sidecar" == "api" && -n "${AZURE_OPENAPI_SHARED_TOKEN:-}" ]]; then
    printf 'ELB_OPENAPI_API_TOKEN=secretref:elb-openapi-api-token\n'
  fi
}

# Upsert the shared M2M token as the Container App secret
# `elb-openapi-api-token`, mirroring the Bicep declaration for the same
# secret name. Idempotent: unchanged value is a no-op from the caller's
# perspective (Container Apps returns 200 either way). No-op when the
# operator has not set AZURE_OPENAPI_SHARED_TOKEN so a deployment that
# opts out of the M2M path never gets an empty / stale secret written.
sync_openapi_shared_token() {
  [[ -n "${AZURE_OPENAPI_SHARED_TOKEN:-}" ]] || return 0
  [[ -n "${CONTAINER_APP_NAME:-}" && -n "${AZURE_RESOURCE_GROUP:-}" ]] || return 0
  ts "    + upserting Container App secret 'elb-openapi-api-token' (M2M shared token)"
  az containerapp secret set \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --secrets "elb-openapi-api-token=$AZURE_OPENAPI_SHARED_TOKEN" \
    -o none \
    || die "failed to upsert Container App secret 'elb-openapi-api-token'"
}

# Apply image, resources, and env to one sidecar through one template ARM PATCH.
# Azure CLI 2.81's `containerapp update --container-name <non-default>
# --set-env-vars ...` can report success while applying no env change. Reading
# a fresh template, rechecking its ETag immediately before PATCH, and verifying
# the active revision avoids split image/env revisions and silent sidecar drift.
containerapp_patch_container() {
  local target="${1:?container name required}"
  local image="${2:?image required}"
  local cpu="${3:-}"
  local memory="${4:-}"
  shift 4

  (
    set -Eeuo pipefail
    umask 077
    local snapshot current_snapshot patch suffix status app_id template_hash
    local current_template_hash revision_name active_revision verify_status deadline
    local -a snapshot_meta helper_args
    snapshot="$(mktemp)"
    current_snapshot="$(mktemp)"
    patch="$(mktemp)"
    trap 'rm -f "$snapshot" "$current_snapshot" "$patch"' EXIT
    suffix="env-${target}-$(date +%s)-${RANDOM}"
    app_id="/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}/providers/Microsoft.App/containerApps/${CONTAINER_APP_NAME}"

    # `az containerapp show` and the Container Apps ARM GET currently omit an
    # ETag in both body and response headers. Read the raw ARM resource twice
    # and compare a canonical template fingerprint immediately before PATCH;
    # this preserves the stale-snapshot halt instead of silently dropping the
    # concurrency guard when the service exposes no conditional version token.
    timeout 45s az rest \
      --method get \
      --url "https://management.azure.com${app_id}?api-version=${CONTAINER_APP_API_VERSION}" \
      -o json > "$snapshot"
    helper_args=(
      --input "$snapshot"
      --output "$patch"
      --container "$target"
      --revision-suffix "$suffix"
      --image "$image"
    )
    [[ -n "$cpu" ]] && helper_args+=(--cpu "$cpu")
    [[ -n "$memory" ]] && helper_args+=(--memory "$memory")
    for _pair in "$@"; do
      helper_args+=(--env "$_pair")
    done
    status="$(python3 "$REPO_ROOT/scripts/dev/patch_containerapp_env.py" "${helper_args[@]}")"
    if [[ "$status" == "unchanged" ]]; then
      ts "    = '$target' image/resources/env already converged"
      return 0
    fi
    [[ "$status" == "changed" ]] || die "unexpected container patch status for '$target': $status"

    mapfile -t snapshot_meta < <(python3 - "$snapshot" <<'PY'
import hashlib, json, sys
resource = json.load(open(sys.argv[1], encoding="utf-8"))
template = resource.get("properties", {}).get("template")
print(resource.get("id", ""))
print(hashlib.sha256(json.dumps(template, sort_keys=True, separators=(",", ":")).encode()).hexdigest() if isinstance(template, dict) else "")
PY
)
    app_id="${snapshot_meta[0]:-}"
    template_hash="${snapshot_meta[1]:-}"
    [[ -n "$app_id" && -n "$template_hash" ]] \
      || die "Container App snapshot is missing id/template"
    timeout 30s az rest \
      --method get \
      --url "https://management.azure.com${app_id}?api-version=${CONTAINER_APP_API_VERSION}" \
      -o json > "$current_snapshot"
    current_template_hash="$(python3 - "$current_snapshot" <<'PY'
import hashlib, json, sys
resource = json.load(open(sys.argv[1], encoding="utf-8"))
template = resource.get("properties", {}).get("template")
print(hashlib.sha256(json.dumps(template, sort_keys=True, separators=(",", ":")).encode()).hexdigest() if isinstance(template, dict) else "")
PY
)"
    [[ -n "$current_template_hash" && "$current_template_hash" == "$template_hash" ]] \
      || die "Container App template changed before '$target' PATCH; retry the deploy"

    timeout 120s az rest \
      --method patch \
      --url "https://management.azure.com${app_id}?api-version=${CONTAINER_APP_API_VERSION}" \
      --headers "Content-Type=application/json" "If-Match=*" \
      --body "@$patch" \
      -o none

    revision_name="${CONTAINER_APP_NAME}--${suffix}"
    local ready=false
    deadline=$((SECONDS + 300))
    while (( SECONDS < deadline )); do
      local revision_state provisioning running
      revision_state="$(timeout 15s az containerapp revision show \
        --name "$CONTAINER_APP_NAME" \
        --resource-group "$AZURE_RESOURCE_GROUP" \
        --revision "$revision_name" \
        --query "join(' ', [properties.provisioningState, properties.runningState])" \
        -o tsv 2>/dev/null || true)"
      read -r provisioning running <<< "$revision_state"
      if [[ "$provisioning" == "Provisioned" && "$running" == Running* ]]; then
        ready=true
        break
      fi
      if [[ "$provisioning" == "Failed" || "$provisioning" == "Canceled" ]]; then
        die "container revision failed for '$target': $revision_name ($provisioning/$running)"
      fi
      sleep 5
    done
    $ready || die "timed out waiting for container revision: $revision_name"

    timeout 45s az rest \
      --method get \
      --url "https://management.azure.com${app_id}?api-version=${CONTAINER_APP_API_VERSION}" \
      -o json > "$snapshot"
    helper_args=(
      --input "$snapshot"
      --output "$patch"
      --container "$target"
      --revision-suffix "${suffix}-verify"
      --image "$image"
    )
    [[ -n "$cpu" ]] && helper_args+=(--cpu "$cpu")
    [[ -n "$memory" ]] && helper_args+=(--memory "$memory")
    for _pair in "$@"; do
      helper_args+=(--env "$_pair")
    done
    verify_status="$(python3 "$REPO_ROOT/scripts/dev/patch_containerapp_env.py" "${helper_args[@]}")"
    [[ "$verify_status" == "unchanged" ]] || die "container verification failed for '$target'"
    active_revision="$(timeout 30s az containerapp show \
      --name "$CONTAINER_APP_NAME" \
      --resource-group "$AZURE_RESOURCE_GROUP" \
      --query properties.latestRevisionName \
      -o tsv)"
    [[ "$active_revision" == "$revision_name" ]] || die "active revision changed during '$target' PATCH; retry the deploy"
    ts "    ✓ '$target' image/resources/env converged on $revision_name"
  )
}

# ---------------------------------------------------------------------------
# Per-sidecar resource (cpu/memory) reconciliation.
#
# Why: fast deploy must include the Bicep-owned cpu/memory values in the same
# exact-container template patch as image/env. Otherwise a sizing change lands
# only on a full `azd provision`; the worker previously remained
# under-provisioned and its Celery prefork pool OOM-looped as a result.
#
# Fix: read the DESIRED cpu/memory for each sidecar straight from the Bicep
# template (the single source of truth) and pass them into every container
# PATCH so the running container converges to the committed sizing. Container
# Apps requires the per-replica total to stay a valid combo (sum memory GiB ==
# 2 × sum CPU cores, ≤ 4 vCPU / 8 GiB); every sidecar's Bicep value is
# individually 1 vCPU : 2 GiB, so moving any single container to its Bicep
# value keeps the running total valid.
#
# Best-effort: on any parse failure (template moved / reformatted) the helper
# yields nothing and the exact-container patch preserves live resources with a
# warning — a sizing reconcile must never block a code deploy.
# ---------------------------------------------------------------------------
CONTROL_PLANE_BICEP_FILE="$REPO_ROOT/infra/modules/containerAppControl.bicep"

# Echo "CPU MEMORY" (e.g. "1.0 2.0Gi") for the given sidecar parsed from the
# Bicep six-sidecar template, or nothing when it cannot be resolved. Anchors on
# the container declaration `name: '<sidecar>'`; env entries are `name: 'KEY'`
# in UPPER_SNAKE so they never collide, and the bootstrap container is named
# 'bootstrap' and is never queried.
container_desired_resources() {
  local sidecar="$1"
  [[ -f "$CONTROL_PLANE_BICEP_FILE" ]] || return 0
  python3 - "$CONTROL_PLANE_BICEP_FILE" "$sidecar" <<'PY'
import re, sys
path, name = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
m = re.search(
    r"name:\s*'" + re.escape(name) + r"'"
    r".*?resources:\s*\{\s*cpu:\s*json\('([0-9.]+)'\)\s*memory:\s*'([^']+)'",
    text, re.DOTALL,
)
if m:
    print(m.group(1), m.group(2))
PY
}

# ---------------------------------------------------------------------------
# Service Bus kill-switch deploy notice (one-time, informational only).
#
# SERVICEBUS_ENABLED is a three-state override: explicit falsy forces OFF;
# unset/empty and truthy both defer activation to the saved Settings config.
# Only the falsy state needs a deploy-time warning because it can make an
# otherwise enabled config appear broken.
#
# This helper surfaces that ONCE per deploy when the resolved SERVICEBUS_ENABLED
# is explicitly falsy. It mirrors control_plane_env_pairs override precedence: a SET
# process/azd env value wins over the JSON default; unset falls back to the JSON
# ("" = defer to the Settings config row). It never flips a gate and never
# fails the deploy — purely a discoverability nudge.
# ---------------------------------------------------------------------------
_SB_GATE_NOTICE_DONE=false
servicebus_gate_notice() {
  $_SB_GATE_NOTICE_DONE && return 0
  _SB_GATE_NOTICE_DONE=true
  local resolved=""
  if [[ -n "${SERVICEBUS_ENABLED+x}" ]]; then
    # Explicit process/azd env override (set, even to "") wins.
    resolved="$SERVICEBUS_ENABLED"
  elif [[ -f "$CONTROL_PLANE_ENV_FILE" ]]; then
    resolved="$(python3 - "$CONTROL_PLANE_ENV_FILE" <<'PY' 2>/dev/null || true
import json, sys
try:
    print((json.load(open(sys.argv[1])).get("api") or {}).get("SERVICEBUS_ENABLED", ""))
except Exception:
    print("")
PY
)"
  fi
  case "$(printf '%s' "$resolved" | tr '[:upper:]' '[:lower:]')" in
    false | 0 | no | off)
      ts "    ! Service Bus kill switch is active: SERVICEBUS_ENABLED=$resolved forces the saved Settings config OFF."
      ts "      Clear it for this deployment: azd env set SERVICEBUS_ENABLED true  (then rerun this deploy)"
      ts "      Unset/empty and true both defer activation to Settings -> Service Bus integration."
      ;;
    *) return 0 ;;
  esac
}


release_build_number() {
  local latest_tag=""
  latest_tag="$(git -C "$REPO_ROOT" tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname --merged HEAD 2>/dev/null | head -n1 || true)"
  if [[ -n "$latest_tag" ]]; then
    git -C "$REPO_ROOT" rev-list --count "$latest_tag..HEAD" 2>/dev/null || printf '0\n'
  else
    git -C "$REPO_ROOT" rev-list --count HEAD 2>/dev/null || printf '0\n'
  fi
}

# Env-loading helpers (strip_quotes / load_simple_env_file / load_azd_env)
# live in lib-env.sh so the set-vs-unset guard cannot drift back to the
# buggy `${!key:-}` form — see lib-env.sh "Risky contracts".
. "$REPO_ROOT/scripts/dev/lib-env.sh"

provider_registration_marker() {
  printf '%s/.logs/provider-registration.%s.ok' "$REPO_ROOT" "${AZURE_SUBSCRIPTION_ID:-default}"
}

ensure_provider_registration_once() {
  local marker max_age now mtime age
  if [[ "${SKIP_PROVIDER_REGISTRATION:-false}" == "true" ]]; then
    ts "Skipping provider registration (SKIP_PROVIDER_REGISTRATION=true)"
    return 0
  fi
  marker="$(provider_registration_marker)"
  max_age="${PROVIDER_REGISTRATION_MARKER_TTL_SECONDS:-3600}"
  if [[ -f "$marker" && "$max_age" =~ ^[0-9]+$ ]]; then
    now="$(date +%s)"
    mtime="$(stat -c %Y "$marker" 2>/dev/null || printf '0')"
    age=$(( now - mtime ))
    if [[ "$age" -ge 0 && "$age" -lt "$max_age" ]]; then
      ts "Skipping provider registration (cached ${age}s ago)"
      return 0
    fi
  fi
  mkdir -p "$(dirname "$marker")"
  if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
    bash "$REPO_ROOT/scripts/dev/register-providers.sh" --subscription "$AZURE_SUBSCRIPTION_ID"
  else
    bash "$REPO_ROOT/scripts/dev/register-providers.sh"
  fi
  : > "$marker"
}

# The auth-bypass toggles (VITE_AUTH_DEV_BYPASS / AUTH_DEV_BYPASS) are
# local-debug-only — set by scripts/dev/local-debug-auth.sh and frequently
# left behind in .env / .env.local after a local session. They must NEVER be
# imported from a file into a cloud deploy (doing so bakes an MSAL-skipping
# SPA / bearer-skipping api into the Container App — see the guard below and
# docs/features_change/2026-05/2026-05-25-frontend-env-leak-hardening.md).
# A developer who genuinely wants the bypass in cloud exports it explicitly
# on the command line (an existing shell export wins over the file import) and
# also sets ELB_ALLOW_AUTH_BYPASS_IN_CLOUD=1.
_ELB_AUTH_BYPASS_SKIP=(VITE_AUTH_DEV_BYPASS AUTH_DEV_BYPASS)
load_simple_env_file "$REPO_ROOT/.env" "${_ELB_AUTH_BYPASS_SKIP[@]}"
load_simple_env_file "$REPO_ROOT/.env.local" "${_ELB_AUTH_BYPASS_SKIP[@]}"
load_simple_env_file "$REPO_ROOT/web/.env.production" "${_ELB_AUTH_BYPASS_SKIP[@]}"
# web/.env.local exists for local-dev (vite dev server + local-run.sh web)
# and pins VITE_API_BASE_URL=http://localhost:8085 plus local-debug toggles
# (VITE_AUTH_DEV_BYPASS, AUTH_DEV_BYPASS). It may also carry a developer's
# personal MSAL tenant/client for local SPA debugging. Those values must
# NEVER end up in a cloud frontend's runtime-config.js or container env —
# see the guard below and
# docs/features_change/2026-05/2026-05-25-frontend-env-leak-hardening.md.
load_simple_env_file "$REPO_ROOT/web/.env.local" \
  VITE_API_BASE_URL \
  VITE_AUTH_DEV_BYPASS \
  AUTH_DEV_BYPASS \
  VITE_AZURE_TENANT_ID \
  VITE_AZURE_CLIENT_ID \
  VITE_AZURE_REDIRECT_URI \
  API_CLIENT_ID
# Always import azd env (fills UNSET keys only — explicit CLI/file exports
# always win via the `${!key+x}` guard, and it is a no-op when azd is absent
# or unreachable thanks to the `command -v azd` + `timeout 8s` guards in
# load_azd_env). This must run UNCONDITIONALLY: gating it on the core target
# vars (AZURE_RESOURCE_GROUP / ACR_NAME / ACR_LOGIN_SERVER / CONTAINER_APP_NAME)
# being unset meant that whenever a deploy passed those four explicitly (the
# normal moonchoi flow), azd env was skipped entirely — so a per-deployment
# control-plane toggle that lives ONLY in azd env (e.g. SERVICEBUS_ENABLED=true)
# never reached control_plane_env_pairs and got reset to the control-plane-env
# .json default ("false") on every such redeploy. Importing azd env here keeps
# those pinned toggles alive across redeploys (the survives-redeploy contract;
# see control_plane_env_pairs above and infra/main.parameters.json
# serviceBusEnabled) while leaving explicit target overrides untouched.
load_azd_env

[[ $# -ge 1 ]] || die "usage: $0 <api|worker|beat|frontend|terminal|all> [tag] [--logs] [--rebuild-terminal-base] [--no-build|--build-only] [--no-prune] [--yes]"

SIDECAR="$1"; shift || true
TAG=""
TAIL_LOGS=false
REBUILD_TERMINAL_BASE=false
SKIP_CONFIRM=false
# --no-build: skip the `az acr build` step and patch the Container App
# straight to an EXISTING image tag in ACR. Used by the GitHub Actions
# deploy workflow, which builds in a separate `build-images.yml` job and
# then triggers deploy.yml with the resulting tag. When set, the frontend
# PATCH also skips frontend runtime-env convergence, so values from the last
# full or source-building deploy are preserved.
NO_BUILD=false
# --build-only: opposite of --no-build. Build the image(s) via `az acr build`
# and skip the Container App template PATCH. Used by build-images.yml in
# GitHub Actions so a push to main produces images in ACR without changing
# the running Container App; deploy.yml then triggers a separate run with
# --no-build to actually swap the revision.
BUILD_ONLY=false
# --no-prune: skip the post-deploy ACR retention sweep that keeps only the
# newest ELB_ACR_KEEP_IMAGES (default 3) tags per control-plane repository and
# deletes the older ones. The sweep is best-effort (a delete failure never
# fails the deploy) and only runs when a fresh image was actually built
# (i.e. not on --no-build). Also disabled via ELB_SKIP_ACR_PRUNE=1.
NO_PRUNE=false
[[ "${ELB_SKIP_ACR_PRUNE:-0}" == "1" ]] && NO_PRUNE=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --logs) TAIL_LOGS=true ;;
    --rebuild-terminal-base) REBUILD_TERMINAL_BASE=true ;;
    --no-build) NO_BUILD=true ;;
    --build-only) BUILD_ONLY=true ;;
    --no-prune) NO_PRUNE=true ;;
    --yes|-y) SKIP_CONFIRM=true ;;
    -*)     die "unknown flag: $1" ;;
    *)      TAG="$1" ;;
  esac
  shift
done
$NO_BUILD && $BUILD_ONLY && die "--no-build and --build-only are mutually exclusive"
[[ -n "$TAG" ]] || TAG="$(date +%Y%m%d%H%M%S)"
# ELB_QUICK_DEPLOY_SKIP_CONFIRM=1 (env) is an alternative to --yes for
# automation contexts that cannot easily inject a CLI flag (e.g. CI hooks
# that re-shell into this script).
[[ "${ELB_QUICK_DEPLOY_SKIP_CONFIRM:-0}" == "1" ]] && SKIP_CONFIRM=true

# ---------------------------------------------------------------------------
# Interactive confirmation. Show the discovered subscription/tenant/RG/ACR/
# app so the operator can sanity-check the target before any ACR build or
# Container App PATCH runs. Skipped when:
#   - stdin is not a TTY (CI, piped, etc.)
#   - --yes / -y is passed on the CLI
#   - ELB_QUICK_DEPLOY_SKIP_CONFIRM=1 is exported
#
# Default-Enter = proceed, anything else = abort. The default is "proceed"
# because the alternative (default-abort) would force every operator to
# type a key on every deploy, even when the discovered target is exactly
# what `az account show` already told them on the previous line. No input
# within 10 s also proceeds, so an unattended run is never left blocking on
# the prompt.
# ---------------------------------------------------------------------------
confirm_deploy_target() {
  $SKIP_CONFIRM && return 0
  [[ -t 0 ]] || return 0
  printf '\n' >&2
  printf '\033[1m==> About to deploy to:\033[0m\n' >&2
  printf '      subscription : %s  (%s)\n' "${AZURE_SUBSCRIPTION_ID:-?}" "$(az account show --query name -o tsv 2>/dev/null || printf '?')" >&2
  printf '      tenant       : %s\n' "${AZURE_TENANT_ID:-?}" >&2
  printf '      resourceGroup: %s\n' "${AZURE_RESOURCE_GROUP:-?}" >&2
  printf '      acr          : %s\n' "${ACR_LOGIN_SERVER:-${ACR_NAME:-?}}" >&2
  printf '      containerApp : %s\n' "${CONTAINER_APP_NAME:-?}" >&2
  [[ -n "${CONTAINER_APP_FQDN:-}" ]] && printf '      fqdn         : https://%s\n' "$CONTAINER_APP_FQDN" >&2
  printf '      sidecar(s)   : %s\n' "$SIDECAR" >&2
  printf '      tag          : %s\n\n' "$TAG" >&2
  local reply=""
  # 10 s auto-proceed: no input within the window is treated the same as
  # pressing Enter (proceed). `read -t` returns non-zero on timeout while
  # leaving $reply empty, so the existing "empty == proceed" branch covers it.
  if read -r -t 10 -p "Proceed? [Enter=yes, anything else=abort, auto-yes in 10s] " reply; then
    if [[ -n "$reply" ]]; then
      ts "aborted by user (input: '$reply')"
      exit 1
    fi
  else
    printf '\n' >&2
    ts "no input within 10s — proceeding automatically"
  fi
}

# ---------------------------------------------------------------------------
# Audit: list every active deploy-time override gate so the operator (and
# anyone reading `deploy.log`) can see at a glance which safety nets were
# bypassed for this run. Active set is computed once and emitted on a single
# line so a `grep deploy-override` over the log is enough.
# ---------------------------------------------------------------------------
log_active_overrides() {
  local active=()
  [[ "${ELB_SKIP_ACR_PRUNE:-0}" == "1" ]] && active+=("ELB_SKIP_ACR_PRUNE")
  [[ "${ELB_SKIP_WORKSPACE_TAGS:-0}" == "1" ]] && active+=("ELB_SKIP_WORKSPACE_TAGS")
  [[ "${ELB_ALLOW_SUB_MISMATCH:-0}" == "1" ]] && active+=("ELB_ALLOW_SUB_MISMATCH")
  [[ "${ELB_ALLOW_AUTH_BYPASS_IN_CLOUD:-0}" == "1" ]] && active+=("ELB_ALLOW_AUTH_BYPASS_IN_CLOUD")
  [[ "${ELB_QUICK_DEPLOY_SKIP_CONFIRM:-0}" == "1" ]] && active+=("ELB_QUICK_DEPLOY_SKIP_CONFIRM")
  [[ "${ELB_SKIP_HOOKS:-0}" == "1" ]] && active+=("ELB_SKIP_HOOKS")
  if [[ ${#active[@]} -gt 0 ]]; then
    ts "deploy-override active: ${active[*]}"
  fi
}

# ---------------------------------------------------------------------------
# preflight_permission_check (critique #8) — fail fast with a clear
# remediation message when the caller lacks the four ARM read permissions
# the script will need a few seconds later: read on the resource group,
# read on the ACR, read on the Container App, and an `az acr build`
# preflight (which exercises both ACR read and AcrPush). The read probes
# are cheap (~200 ms each) so the cost is negligible; the value is that
# a 401 / 403 surfaces here with the exact role the operator needs
# instead of after a 30-90 s build.
#
# Skip entirely with ELB_QUICK_DEPLOY_SKIP_PREFLIGHT=1 (CI runners with
# pre-validated SPs do not need this).
# ---------------------------------------------------------------------------
preflight_permission_check() {
  [[ "${ELB_QUICK_DEPLOY_SKIP_PREFLIGHT:-0}" == "1" ]] && return 0
  command -v az >/dev/null 2>&1 || die "az CLI not found on PATH"

  local who="" user_type=""
  who="$(az account show --query 'user.name' -o tsv 2>/dev/null || true)"
  user_type="$(az account show --query 'user.type' -o tsv 2>/dev/null || true)"
  if [[ -z "$who" ]]; then
    die "Not signed in to Azure CLI. Run 'az login' and retry."
  fi
  # Critique-round-1 M2: differentiate user-vs-SP in the diagnostic so
  # a service-principal session in CI does not see a misleading
  # "Run az login" hint when its assignments are missing.
  if [[ "$user_type" == "servicePrincipal" ]]; then
    ts "preflight: signed-in as service principal $who"
  else
    ts "preflight: signed-in as $who"
  fi

  local _hint_who="$who"
  if [[ "$user_type" == "servicePrincipal" ]]; then
    _hint_who="<sp-object-id>"  # az role assignment list --assignee expects the SP object id, not appId
  fi

  if ! az group show -n "$AZURE_RESOURCE_GROUP" -o none 2>/dev/null; then
    die "Cannot read resource group '$AZURE_RESOURCE_GROUP'. The signed-in identity needs at least 'Reader' on the subscription or RG. Run 'az role assignment list --assignee $_hint_who --resource-group $AZURE_RESOURCE_GROUP' to inspect."
  fi

  if ! az acr show -n "$ACR_NAME" -g "$AZURE_RESOURCE_GROUP" -o none 2>/dev/null; then
    die "Cannot read ACR '$ACR_NAME' in '$AZURE_RESOURCE_GROUP'. The signed-in identity needs 'Reader' (or higher). Without 'Contributor' the subsequent 'az acr update' (firewall toggle) and 'az acr build' will fail with AuthorizationFailed."
  fi

  if ! az containerapp show -n "$CONTAINER_APP_NAME" -g "$AZURE_RESOURCE_GROUP" -o none 2>/dev/null; then
    die "Cannot read Container App '$CONTAINER_APP_NAME' in '$AZURE_RESOURCE_GROUP'. The signed-in identity needs 'Contributor' on the Container App for the upcoming ARM template PATCH to succeed."
  fi

  ts "preflight: ARM read access OK on rg/acr/containerApp"
}

# ---------------------------------------------------------------------------
# ensure_workspace_tags -- add the elb-* workspace discovery tags to the
# deployment resource group when they are missing.
#
# The SPA's first-run auto-discovery (web/src/pages/Dashboard/configFromTags.ts)
# only treats a resource group as a BLAST workspace when it carries at least
# one `elb-*` tag, and reads `elb-storage` / `elb-acr` / `elb-region` from
# those tags to populate the dashboard. The full `azd up` path applies these
# via postprovision.sh `tag_workspace_resource_group`, but a fast
# `quick-deploy.sh` cycle never ran provisioning, so a resource group that
# was only ever touched by quick-deploy (or had its tags stripped) leaves
# every signed-in user stuck on the Setup Wizard even when they hold read
# access. This closes that gap.
#
# "Add if missing" semantics: each desired key is written ONLY when it is
# absent (or empty) on the RG, so a pre-existing correct value is never
# clobbered by a stale shell variable. Keys whose value cannot be resolved
# from the environment are skipped rather than written empty. The merge is
# best-effort — a caller without tag-write permission (Reader) gets a warn
# line, not a failed deploy. Skip entirely with ELB_SKIP_WORKSPACE_TAGS=1.
# ---------------------------------------------------------------------------
ensure_workspace_tags() {
  if [[ "${ELB_SKIP_WORKSPACE_TAGS:-0}" == "1" ]]; then
    ts "Skipping workspace RG tagging (ELB_SKIP_WORKSPACE_TAGS=1)"
    return 0
  fi

  local rg_id
  rg_id="$(az group show -n "$AZURE_RESOURCE_GROUP" --query id -o tsv --only-show-errors 2>/dev/null || true)"
  if [[ -z "$rg_id" ]]; then
    ts "    ! cannot resolve resource group id for tagging; skipping workspace tags"
    return 0
  fi

  # Desired discovery tags, mirroring postprovision.sh tag_workspace_resource_group.
  # An empty value means "could not resolve" — we never write an empty tag.
  local -a keys=(elb-workload-rg elb-acr-rg elb-acr elb-storage elb-region)
  local -A desired=(
    [elb-workload-rg]="$AZURE_RESOURCE_GROUP"
    [elb-acr-rg]="$AZURE_RESOURCE_GROUP"
    [elb-acr]="${ACR_NAME:-}"
    [elb-storage]="${STORAGE_ACCOUNT_NAME:-}"
    [elb-region]="${AZURE_LOCATION:-}"
  )

  local -a merge_args=()
  local k v present
  for k in "${keys[@]}"; do
    v="${desired[$k]}"
    [[ -n "$v" ]] || continue
    # Query the single tag value; az prints empty (not "None") for an
    # absent key with `-o tsv`.
    present="$(az group show -n "$AZURE_RESOURCE_GROUP" \
      --query "tags.\"$k\"" -o tsv --only-show-errors 2>/dev/null || true)"
    if [[ -z "$present" || "$present" == "None" ]]; then
      merge_args+=("$k=$v")
    fi
  done

  if [[ ${#merge_args[@]} -eq 0 ]]; then
    ts "==> Workspace RG discovery tags already present; nothing to add"
    return 0
  fi

  ts "==> Adding missing dashboard workspace tags: ${merge_args[*]}"
  if az tag update \
      --resource-id "$rg_id" \
      --operation Merge \
      --tags "${merge_args[@]}" \
      --only-show-errors >/dev/null 2>&1; then
    ts "    ✓ workspace discovery tags merged onto $AZURE_RESOURCE_GROUP"
  else
    ts "    ! tag merge failed (need 'Tag Contributor' or 'Contributor' on the RG); auto-discovery may keep showing the Setup Wizard"
  fi
}

# ---------------------------------------------------------------------------
# resolve_image_digest -- pin a mutable tag to its immutable digest.
#
# Azure Container Apps only rolls a NEW revision when the template's image
# string changes. Patching a mutable tag (latest-main, latest, ...) that the
# active revision already references is a byte-for-byte no-op, so a freshly
# rebuilt image pushed under the SAME tag is silently ignored -- the deploy
# "succeeds" but the old image keeps running and the version stamp never
# changes. Resolving the tag to registry/image@sha256:... makes every
# distinct build a distinct template -> a new revision always rolls. Digest
# resolution is required: after three bounded attempts, fail instead of falling
# back to a mutable tag that could make a deploy report success without rolling.
# ---------------------------------------------------------------------------
resolve_image_digest() {
  local ref="$1" digest attempt
  for attempt in 1 2 3; do
    digest="$(timeout 30s az acr manifest show-metadata "$ref" --query digest -o tsv 2>/dev/null | tr -d '[:space:]')" || true
    if [[ "$digest" == sha256:* ]]; then
      printf '%s@%s' "${ref%:*}" "$digest"
      return 0
    fi
    (( attempt < 3 )) && sleep "$((attempt * 2))"
  done
  printf 'ERROR: could not resolve immutable digest for %s after 3 attempts\n' "$ref" >&2
  return 1
}

# ---------------------------------------------------------------------------
# acr_prune_repo_keep_recent -- bound ACR storage growth.
#
# Every deploy pushes a new tag (e.g. elb-api:20260622_…) so the registry
# accumulates one manifest per deploy forever. This sweep keeps only the
# newest ELB_ACR_KEEP_IMAGES (default 3) manifests per repository, ordered by
# last-update time, and deletes the older ones.
#
# Safety:
#   * Best-effort: a missing 'Contributor'/'AcrDelete' permission, a transient
#     registry error, or a repo with <= keep manifests is a no-op — it never
#     fails the deploy (the caller invokes it with `|| true`).
#   * Keeps the newest N, so the just-pushed image AND the previously-running
#     image (the rollback target) are always retained.
#   * Skipped on --no-prune / ELB_SKIP_ACR_PRUNE=1 and on --no-build (no fresh
#     image was pushed, so nothing new accumulated this run).
#   * MUST be called WHILE the ACR firewall is open (between
#     acr_ensure_build_access and acr_restore_build_access). Steady-state ACR
#     is publicNetworkAccess=Disabled, so the data-plane list/delete calls
#     below are network-refused once the registry is re-locked.
# ---------------------------------------------------------------------------
acr_prune_repo_keep_recent() {
  local acr="$1" repo="$2" keep="${3:-3}"
  [[ -n "$acr" && -n "$repo" ]] || return 0
  # All digests for the repo, newest first. `--orderby time_desc` puts the most
  # recently updated manifest at index 0.
  local -a digests=()
  mapfile -t digests < <(
    az acr manifest list-metadata --registry "$acr" --name "$repo" \
      --orderby time_desc \
      --query "[].digest" -o tsv 2>/dev/null || true
  )
  local total="${#digests[@]}"
  if (( total <= keep )); then
    ts "    (acr prune: $repo has $total manifest(s) <= keep=$keep; nothing to delete)"
    return 0
  fi
  local deleted=0 idx digest
  for (( idx = keep; idx < total; idx++ )); do
    digest="${digests[$idx]}"
    [[ "$digest" == sha256:* ]] || continue
    if az acr manifest delete --registry "$acr" --name "$repo@$digest" --yes -o none 2>/dev/null; then
      deleted=$(( deleted + 1 ))
    else
      ts "    ! acr prune: failed to delete $repo@${digest:0:19}… (need 'Contributor'/'AcrDelete'?); skipping"
    fi
  done
  ts "    ✓ acr prune: $repo kept newest $keep, deleted $deleted older manifest(s)"
}

# acr_prune_targets -- run the retention sweep over one or more repos unless
# pruning is disabled. Honours ELB_ACR_KEEP_IMAGES (default 3).
acr_prune_targets() {
  if [[ "${NO_PRUNE:-false}" == "true" ]]; then
    ts "==> Skipping ACR retention prune (--no-prune / ELB_SKIP_ACR_PRUNE=1)"
    return 0
  fi
  if [[ "${NO_BUILD:-false}" == "true" ]]; then
    ts "==> Skipping ACR retention prune (--no-build: no fresh image pushed)"
    return 0
  fi
  local keep="${ELB_ACR_KEEP_IMAGES:-3}"
  if ! [[ "$keep" =~ ^[0-9]+$ ]] || (( keep < 1 )); then
    ts "    ! invalid ELB_ACR_KEEP_IMAGES='$keep'; falling back to 3"
    keep=3
  fi
  ts "==> ACR retention prune (keep newest $keep per repository) on $ACR_NAME"
  local repo
  for repo in "$@"; do
    acr_prune_repo_keep_recent "$ACR_NAME" "$repo" "$keep" || true
  done
}

# ---------------------------------------------------------------------------
# assert_msal_client_matches_target -- refuse to bake a frontend whose MSAL
# App Registration client id (VITE_AZURE_CLIENT_ID) does not match the one
# the TARGET Container App's api sidecar already validates bearer tokens
# against (its API_CLIENT_ID env).
#
# Why: .env / web/.env.local in a fresh clone frequently carry a developer's
# OWN tenant/client values (see the env-leak hardening note
# docs/features_change/2026-05/2026-05-25-frontend-env-leak-hardening.md).
# When a different operator runs `quick-deploy.sh all` / `frontend`
# without exporting the target's MSAL overrides, the SPA is baked to log
# users in against App Registration A while the api only accepts tokens
# minted for App Registration B -- the deploy "succeeds" but every /api/*
# call returns 401. The existing localhost / auth-bypass guards above catch
# two siblings of this incident class; this catches the wrong-tenant one.
#
# Behaviour (mirrors the abort-with-escape-hatch style of the auth-bypass
# guard, NOT a default-OFF STRICT_* gate -- baking a mismatched audience is
# always a bug, so the safe default is to stop):
#   * target api API_CLIENT_ID present AND differs from the value to bake
#       -> abort with remediation + escape hatch ELB_ALLOW_MSAL_CLIENT_MISMATCH=1
#         (the intended path when deliberately rotating the App Registration).
#   * target api API_CLIENT_ID absent (first-ever deploy / bootstrap) OR the
#     show query fails (transient ARM error / read-only hiccup) -> warn and
#     continue, so a legitimate first rollout is never blocked.
#
# Args: $1 = client id that will be baked into the frontend (API_CLIENT_ID_VAL).
# ---------------------------------------------------------------------------
assert_msal_client_matches_target() {
  local baking="$1" current=""
  [[ -n "$baking" ]] || return 0
  if [[ "${ELB_ALLOW_MSAL_CLIENT_MISMATCH:-0}" == "1" ]]; then
    ts "MSAL client-id match check skipped (ELB_ALLOW_MSAL_CLIENT_MISMATCH=1)"
    return 0
  fi
  current="$(az containerapp show -n "$CONTAINER_APP_NAME" -g "$AZURE_RESOURCE_GROUP" \
    --query "properties.template.containers[?name=='api'].env[] | [?name=='API_CLIENT_ID'].value | [0]" \
    -o tsv 2>/dev/null | tr -d '[:space:]')" || true
  if [[ -z "$current" || "$current" == "None" ]]; then
    ts "MSAL client-id match check: target api has no API_CLIENT_ID yet (first deploy?) — skipping"
    return 0
  fi
  if [[ "$current" != "$baking" ]]; then
    die "MSAL client-id mismatch: about to bake VITE_AZURE_CLIENT_ID='$baking' into the cloud frontend, but the target Container App's api sidecar validates bearer tokens against API_CLIENT_ID='$current'. Deploying would log users in against the wrong App Registration — every /api/* call returns 401. Fix the source value (.env / web/.env.local / azd env) so VITE_AZURE_CLIENT_ID/API_CLIENT_ID matches the target, or set ELB_ALLOW_MSAL_CLIENT_MISMATCH=1 if you are intentionally rotating the App Registration."
  fi
  ts "MSAL client-id match check OK (frontend VITE_AZURE_CLIENT_ID == target api API_CLIENT_ID)"
}

if [[ "$SIDECAR" == "all" ]]; then
  # ---------------------------------------------------------------------------
  # Parallel-build path: api / frontend / terminal images build concurrently
  # via three backgrounded `az acr build` jobs, then we PATCH the Container
  # App containers SEQUENTIALLY. Two races make naive full parallelism
  # unsafe and they're both worth re-stating:
  #
  #   1. ACR firewall toggle. acr_ensure_build_access / acr_restore_build_access
  #      track state in subshell-local vars. If each per-target subshell ran
  #      its own toggle, the first one to finish would close the firewall
  #      while the others were still mid-build, and they'd fail with 401 /
  #      "network not allowed". Open ONCE in the parent, close ONCE after
  #      `wait`.
  #
  #   2. Every exact-container PATCH still starts from a full template snapshot.
  #      Running them in parallel would race the ETag precheck and latest-
  #      revision verification. PATCHes stay sequential.
  #
  # Net result vs the old recursive-sequential loop: build time drops from
  # ~3 min (3 x ~60 s sequential) to ~60-90 s (parallel; bound by the
  # slowest image). PATCH wall time is unchanged (~1 min). Total deploy
  # ~3-4 min vs ~6-8 min.
  # ---------------------------------------------------------------------------
  ts "==> Deploying all quick-deploy targets with tag: $TAG (parallel-build mode)"
  $NO_BUILD && ts "    --no-build: skipping ACR build, will only PATCH Container App"

  # Discover/align env from the active az login BEFORE validating env vars,
  # so a stale `/tmp/azd-env.sh` from a different sub does not block the
  # deploy. The helper exports AZURE_*, ACR_*, CONTAINER_APP_*, etc. from
  # ARM lookups in the active subscription.
  assert_az_subscription_aligned

  for v in AZURE_RESOURCE_GROUP ACR_NAME ACR_LOGIN_SERVER CONTAINER_APP_NAME; do
    [[ -n "${!v:-}" ]] || die "$v is unset and az-context discovery could not populate it (run: az login + verify the active sub has an elb-dashboard RG)"
  done
  confirm_deploy_target
  log_active_overrides
  preflight_permission_check
  ensure_provider_registration_once
  ensure_workspace_tags

  NEW_API="${ACR_LOGIN_SERVER}/elb-api:${TAG}"
  NEW_FRONTEND="${ACR_LOGIN_SERVER}/elb-frontend:${TAG}"
  NEW_TERMINAL="${ACR_LOGIN_SERVER}/elb-terminal:${TAG}"

if ! $NO_BUILD; then
  # Resolve frontend build args + per-PATCH env-vars on the host once. These
  # mirror the single-sidecar `frontend` branch below (line ~228 onward) and
  # MUST stay in sync with it -- if you add a new VITE_FEATURE_* there, add
  # it here too.
  API_CLIENT_ID_VAL="${VITE_AZURE_CLIENT_ID:-${API_CLIENT_ID:-}}"
  [[ -n "$API_CLIENT_ID_VAL" ]] || die "API_CLIENT_ID/VITE_AZURE_CLIENT_ID is unset; set .env, web/.env.local, or azd env before deploying"
  AZURE_TENANT_ID_VAL="${VITE_AZURE_TENANT_ID:-${AZURE_TENANT_ID:-common}}"
  if [[ "$AZURE_TENANT_ID_VAL" == "common" && -n "${AZURE_TENANT_ID:-}" ]]; then
    AZURE_TENANT_ID_VAL="$AZURE_TENANT_ID"
  fi
  VITE_AUTH_DEV_BYPASS_VAL="${VITE_AUTH_DEV_BYPASS:-false}"
  VITE_API_BASE_URL_VAL="${VITE_API_BASE_URL:-}"
  VITE_AZURE_REDIRECT_URI_VAL="${VITE_AZURE_REDIRECT_URI:-__RUNTIME__}"
  VITE_FEATURE_CUSTOM_DB_VAL="${VITE_FEATURE_CUSTOM_DB:-true}"
  VITE_FEATURE_LAB_TOOLS_VAL="${VITE_FEATURE_LAB_TOOLS:-true}"
  VITE_FEATURE_TERMINAL_VAL="${VITE_FEATURE_TERMINAL:-true}"

  if [[ -n "$VITE_API_BASE_URL_VAL" ]] && \
     [[ "$VITE_API_BASE_URL_VAL" =~ ^https?://(localhost|127\.|0\.0\.0\.0|\[::1\]) ]]; then
    die "VITE_API_BASE_URL='$VITE_API_BASE_URL_VAL' points at the local host — refusing to bake that into the cloud frontend. Run 'unset VITE_API_BASE_URL' (or export VITE_API_BASE_URL='') and retry."
  fi
  if [[ "$VITE_AUTH_DEV_BYPASS_VAL" == "true" && "${ELB_ALLOW_AUTH_BYPASS_IN_CLOUD:-0}" != "1" ]]; then
    die "VITE_AUTH_DEV_BYPASS=true — refusing to deploy a cloud frontend that skips MSAL while the api enforces bearer tokens. Run 'unset VITE_AUTH_DEV_BYPASS' (or export VITE_AUTH_DEV_BYPASS=false) and retry."
  fi
  # Guard: a stale .env / web/.env.local carrying a different tenant's MSAL
  # client id would bake an SPA that authenticates against the wrong App
  # Registration -> 401 on every /api/* call. Stop unless the target api has
  # no client id yet or the operator is deliberately rotating it.
  assert_msal_client_matches_target "$API_CLIENT_ID_VAL"

  APP_VERSION_VAL="${APP_VERSION:-$(node -p "require('$REPO_ROOT/web/package.json').version" 2>/dev/null || echo 0.0.0)}"
  APP_BUILD_NUMBER_VAL="${APP_BUILD_NUMBER:-$(release_build_number)}"
  GIT_COMMIT_VAL="${GIT_COMMIT:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)}"
  BUILD_TIME_VAL="${BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

  LOG_DIR="$REPO_ROOT/.logs/quick-deploy/$TAG"
  mkdir -p "$LOG_DIR"
  ts "==> Per-image build logs:   $LOG_DIR/build-<image>.log"
  ts "    Follow live in another terminal:"
  ts "      tail -F $LOG_DIR/build-*.log"
  ts ""

  # Install the restore trap BEFORE opening the firewall. acr_ensure_build_access
  # mutates ACR network state then waits for the policy to take effect; if any
  # step inside the helper fails (set -e), we must still restore. The helper
  # uses ACR_BUILD_ACCESS_RESTORE_NEEDED so a pre-open trap is a safe no-op.
  trap 'acr_restore_build_access "$ACR_NAME"' EXIT
  acr_ensure_build_access "$ACR_NAME"

  # Resolve terminal base in the parent so the three build subshells don't
  # race on `ensure_terminal_base_image` (it can build + push a base image
  # on cache miss, and two concurrent runs of that helper would step on
  # each other's `az acr import` / `az acr build`). When the base image is
  # missing this is the longest single step of the deploy (~2-4 min); the
  # tip above already pointed the operator at $LOG_DIR/build-elb-terminal-base.log.
  TERMINAL_BASE_REBUILD="$REBUILD_TERMINAL_BASE" ensure_terminal_base_image
  TERMINAL_BASE_IMAGE_VAL="$(terminal_base_image)"

  ts "==> Building 4 images in parallel via az acr build"
  {
    echo "[build-elb-api] starting at $(date -u +%H:%M:%S)"
    az acr build \
      --registry "$ACR_NAME" \
      --image "elb-api:${TAG}" \
      --file "api/Dockerfile" \
      --build-arg "APP_VERSION=$APP_VERSION_VAL" \
      --build-arg "APP_GIT_COMMIT=$GIT_COMMIT_VAL" \
      --build-arg "APP_BUILD_TIME=$BUILD_TIME_VAL" \
      "." \
      -o none
    rc=$?
    echo "[build-elb-api] finished at $(date -u +%H:%M:%S), rc=$rc"
    exit $rc
  } > "$LOG_DIR/build-elb-api.log" 2>&1 &
  PID_API=$!

  {
    echo "[build-elb-prepare-db] starting at $(date -u +%H:%M:%S)"
    az acr build \
      --registry "$ACR_NAME" \
      --image "elb-prepare-db:${TAG}" \
      --image "elb-prepare-db:latest" \
      --file "aks/prepare-db/Dockerfile" \
      "aks/prepare-db" \
      -o none
    rc=$?
    echo "[build-elb-prepare-db] finished at $(date -u +%H:%M:%S), rc=$rc"
    exit $rc
  } > "$LOG_DIR/build-elb-prepare-db.log" 2>&1 &
  PID_PREPARE_DB=$!

  {
    echo "[build-elb-frontend] starting at $(date -u +%H:%M:%S)"
    az acr build \
      --registry "$ACR_NAME" \
      --image "elb-frontend:${TAG}" \
      --file "web/Dockerfile" \
      --build-arg "VITE_API_BASE_URL=$VITE_API_BASE_URL_VAL" \
      --build-arg "VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS_VAL" \
      --build-arg "VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI_VAL" \
      --build-arg "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL" \
      --build-arg "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL" \
      --build-arg "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL" \
      --build-arg "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL" \
      --build-arg "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL" \
      --build-arg "APP_VERSION=$APP_VERSION_VAL" \
      --build-arg "APP_BUILD_NUMBER=$APP_BUILD_NUMBER_VAL" \
      --build-arg "GIT_COMMIT=$GIT_COMMIT_VAL" \
      --build-arg "BUILD_TIME=$BUILD_TIME_VAL" \
      "." \
      -o none
    rc=$?
    echo "[build-elb-frontend] finished at $(date -u +%H:%M:%S), rc=$rc"
    exit $rc
  } > "$LOG_DIR/build-elb-frontend.log" 2>&1 &
  PID_FRONTEND=$!

  {
    echo "[build-elb-terminal] starting at $(date -u +%H:%M:%S)"
    az acr build \
      --registry "$ACR_NAME" \
      --image "elb-terminal:${TAG}" \
      --file "terminal/Dockerfile.runtime" \
      --build-arg "TERMINAL_BASE_IMAGE=$TERMINAL_BASE_IMAGE_VAL" \
      "terminal/" \
      -o none
    rc=$?
    echo "[build-elb-terminal] finished at $(date -u +%H:%M:%S), rc=$rc"
    exit $rc
  } > "$LOG_DIR/build-elb-terminal.log" 2>&1 &
  PID_TERMINAL=$!

  ts "    elb-api:      pid=$PID_API"
  ts "    elb-prepare-db: pid=$PID_PREPARE_DB"
  ts "    elb-frontend: pid=$PID_FRONTEND"
  ts "    elb-terminal: pid=$PID_TERMINAL"

  declare -A RUNNING=(
    ["elb-api"]=$PID_API
    ["elb-prepare-db"]=$PID_PREPARE_DB
    ["elb-frontend"]=$PID_FRONTEND
    ["elb-terminal"]=$PID_TERMINAL
  )
  while [ ${#RUNNING[@]} -gt 0 ]; do
    sleep 15
    finished=()
    for name in "${!RUNNING[@]}"; do
      pid=${RUNNING["$name"]}
      if ! kill -0 "$pid" 2>/dev/null; then
        set +e
        wait "$pid"
        rc=$?
        set -e
        if [ "$rc" = "0" ]; then
          ts "    ✓ $name finished (rc=0)"
        else
          ts "    ✗ $name FAILED (rc=$rc) — see $LOG_DIR/build-$name.log"
          tail -30 "$LOG_DIR/build-$name.log" | sed "s/^/      [build-$name] /"
        fi
        finished+=("$name")
      fi
    done
    for name in "${finished[@]}"; do
      unset "RUNNING[$name]"
    done
    if [ ${#RUNNING[@]} -gt 0 ]; then
      ts "    waiting for: ${!RUNNING[*]}"
    fi
  done

  fail=0
  for name in elb-api elb-prepare-db elb-frontend elb-terminal; do
    if ! grep -q "rc=0$" "$LOG_DIR/build-$name.log" 2>/dev/null; then
      fail=1
      ts "✗ build $name did not produce rc=0"
    fi
  done
  if [ "$fail" = "1" ]; then
    ts "Aborting: at least one image build failed (ACR firewall will be restored on exit)."
    exit 1
  fi
  ts "==> All 4 images built and pushed"

  # Prune older manifests WHILE the ACR firewall is still open. Steady-state
  # ACR is publicNetworkAccess=Disabled, so the data-plane list/delete calls
  # below only work before acr_restore_build_access re-locks the registry.
  # Best-effort: a delete failure never aborts the deploy.
  acr_prune_targets elb-api elb-prepare-db elb-frontend elb-terminal

  acr_restore_build_access "$ACR_NAME"
  trap - EXIT
fi  # end: if ! $NO_BUILD (all branch)

if $BUILD_ONLY; then
  ts "==> --build-only: skipping Container App PATCH. Built images:"
  ts "      $NEW_API"
  ts "      $NEW_FRONTEND"
  ts "      $NEW_TERMINAL"
  ts "==> Done. Tag was: $TAG"
  exit 0
fi

  # Pin mutable tags to their immutable digests so the PATCH actually changes
  # the Container App template and rolls a new revision. Without this,
  # `deploy all latest-main` is a silent no-op whenever the active revision
  # already references :latest-main (see resolve_image_digest).
  ts "==> Resolving image tags to digests for a deterministic revision roll"
  NEW_API="$(resolve_image_digest "$NEW_API")"
  NEW_FRONTEND="$(resolve_image_digest "$NEW_FRONTEND")"
  NEW_TERMINAL="$(resolve_image_digest "$NEW_TERMINAL")"
  ts "      api/worker/beat -> $NEW_API"
  ts "      frontend        -> $NEW_FRONTEND"
  ts "      terminal        -> $NEW_TERMINAL"

  # Sequential PATCHes -- see the long comment at the top of this block.
  # api / worker / beat share the elb-api image and are patched one at a
  # time to keep the read-modify-write semantics deterministic.
  declare -a PATCH_PLAN=(
    "api:$NEW_API"
    "worker:$NEW_API"
    "beat:$NEW_API"
    "frontend:$NEW_FRONTEND"
    "terminal:$NEW_TERMINAL"
  )
  for spec in "${PATCH_PLAN[@]}"; do
    tgt="${spec%%:*}"
    img="${spec#*:}"
    ts "==> Patching container '$tgt' on $CONTAINER_APP_NAME → $img"
    # Reconcile cpu/memory to the Bicep template so a committed sizing change
    # lands on a fast deploy instead of being silently preserved at the live
    # (possibly under-provisioned) value. Empty when unparseable preserves live resources.
    _cpu=""
    _memory=""
    _res="$(container_desired_resources "$tgt")"
    if [[ -n "$_res" ]]; then
      _cpu="${_res%% *}"
      _memory="${_res##* }"
      ts "    + reconciling '$tgt' resources from Bicep → cpu=${_res%% *} memory=${_res##* }"
    elif [[ -f "$CONTROL_PLANE_BICEP_FILE" ]]; then
      ts "    ! could not parse '$tgt' resources from Bicep — PATCH keeps live cpu/memory"
    fi
    _env_pairs=()
    if [[ "$tgt" == "frontend" && "$NO_BUILD" != "true" ]]; then
      # Full deploy resolved VITE_* / API_CLIENT_ID on the host; mirror them
      # to the frontend runtime env so runtime-config.js stays in sync with
      # the image we just built.
      _env_pairs=(
        "VITE_API_BASE_URL=$VITE_API_BASE_URL_VAL"
        "VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS_VAL"
        "VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI_VAL"
        "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL"
        "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL"
        "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL"
        "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL"
        "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL"
        "API_CLIENT_ID=$API_CLIENT_ID_VAL"
        "AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL"
      )
    else
      # --no-build frontend keeps its live runtime config. Runtime sidecars
      # converge guard toggles and non-secret platform coordinates.
      mapfile -t _env_pairs < <(control_plane_env_pairs "$tgt")
      case "$tgt" in api | worker | beat) servicebus_gate_notice ;; esac
      # The secret must exist before the exact-container env patch references it.
      [[ "$tgt" == "api" ]] && sync_openapi_shared_token
    fi

    if [[ ${#_env_pairs[@]} -gt 0 ]]; then
      ts "    + applying ${#_env_pairs[@]} exact-container runtime env var(s) for '$tgt'"
    elif [[ ! -f "$CONTROL_PLANE_ENV_FILE" && "$tgt" != "frontend" ]]; then
      ts "    ! control-plane env file missing ($CONTROL_PLANE_ENV_FILE) — '$tgt' guard env NOT applied"
    else
      ts "    (no runtime env changes for '$tgt')"
    fi
    containerapp_patch_container \
      "$tgt" "$img" "$_cpu" "$_memory" "${_env_pairs[@]}"
  done

  ts "==> Latest revision:"
  az containerapp revision list \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --query "sort_by([], &properties.createdTime)[-1].{name:name, active:properties.active, state:properties.runningState, replicas:properties.replicas, created:properties.createdTime}" \
    -o table || true

  if $TAIL_LOGS; then
    ts "==> Tailing logs (Ctrl-C to exit) for container 'api'"
    az containerapp logs show \
      --name "$CONTAINER_APP_NAME" \
      --resource-group "$AZURE_RESOURCE_GROUP" \
      --container api \
      --follow \
      --tail 20
  fi

  ts "==> Done. Tag was: $TAG"
  ts "    To roll back all fast-deployed images, rerun: scripts/dev/quick-deploy.sh all <previous-tag>"
  exit 0
fi

case "$SIDECAR" in
  api|worker|beat) IMAGE_NAME="elb-api";       DOCKERFILE="api/Dockerfile";       BUILD_CTX="." ;;
  frontend)        IMAGE_NAME="elb-frontend";  DOCKERFILE="web/Dockerfile";       BUILD_CTX="." ;;
  terminal)        IMAGE_NAME="elb-terminal";  DOCKERFILE="terminal/Dockerfile.runtime";  BUILD_CTX="terminal/" ;;
  *) die "unknown sidecar '$SIDECAR' (expected: api|worker|beat|frontend|terminal|all)" ;;
esac

# Discover/align env from the active az login BEFORE validating env vars
# (see the matching block in the `all` branch above for the rationale).
assert_az_subscription_aligned

for v in AZURE_RESOURCE_GROUP ACR_NAME ACR_LOGIN_SERVER CONTAINER_APP_NAME; do
  [[ -n "${!v:-}" ]] || die "$v is unset and az-context discovery could not populate it (run: az login + verify the active sub has an elb-dashboard RG)"
done
confirm_deploy_target
log_active_overrides
preflight_permission_check
ensure_provider_registration_once
ensure_workspace_tags

NEW_IMAGE="${ACR_LOGIN_SERVER}/${IMAGE_NAME}:${TAG}"
API_CLIENT_ID_VAL="${VITE_AZURE_CLIENT_ID:-${API_CLIENT_ID:-}}"
AZURE_TENANT_ID_VAL="${VITE_AZURE_TENANT_ID:-${AZURE_TENANT_ID:-common}}"
if [[ "$AZURE_TENANT_ID_VAL" == "common" && -n "${AZURE_TENANT_ID:-}" ]]; then
  AZURE_TENANT_ID_VAL="$AZURE_TENANT_ID"
fi
VITE_AUTH_DEV_BYPASS_VAL="${VITE_AUTH_DEV_BYPASS:-false}"
VITE_API_BASE_URL_VAL="${VITE_API_BASE_URL:-}"
VITE_AZURE_REDIRECT_URI_VAL="${VITE_AZURE_REDIRECT_URI:-__RUNTIME__}"
VITE_FEATURE_CUSTOM_DB_VAL="${VITE_FEATURE_CUSTOM_DB:-true}"
VITE_FEATURE_LAB_TOOLS_VAL="${VITE_FEATURE_LAB_TOOLS:-true}"
VITE_FEATURE_TERMINAL_VAL="${VITE_FEATURE_TERMINAL:-true}"
if ! $NO_BUILD; then
  trap 'acr_restore_build_access "$ACR_NAME"' EXIT

  declare -a BUILD_ARGS=()
  if [[ "$SIDECAR" == "frontend" ]]; then
    [[ -n "$API_CLIENT_ID_VAL" ]] || die "API_CLIENT_ID/VITE_AZURE_CLIENT_ID is unset; set .env, web/.env.local, or azd env before deploying frontend"
    # Guard: a stale local-dev export (e.g. local-run.sh web) leaking
    # VITE_API_BASE_URL=http://localhost:... into this shell would bake the
    # loopback URL into the cloud frontend's runtime-config.js and break every
    # /api/* call from the browser. Force the operator to unset it first.
    if [[ -n "$VITE_API_BASE_URL_VAL" ]] && \
       [[ "$VITE_API_BASE_URL_VAL" =~ ^https?://(localhost|127\.|0\.0\.0\.0|\[::1\]) ]]; then
      die "VITE_API_BASE_URL='$VITE_API_BASE_URL_VAL' points at the local host — refusing to bake that into the cloud frontend. Run 'unset VITE_API_BASE_URL' (or export VITE_API_BASE_URL='') and retry."
    fi
    # Guard: VITE_AUTH_DEV_BYPASS=true makes the SPA skip MSAL while the api
    # sidecar still enforces bearer tokens — users hit a sea of 401s. The flag
    # is meant for local-debug only. Escape hatch (intentionally undocumented
    # in the help text): ELB_ALLOW_AUTH_BYPASS_IN_CLOUD=1.
    if [[ "$VITE_AUTH_DEV_BYPASS_VAL" == "true" && "${ELB_ALLOW_AUTH_BYPASS_IN_CLOUD:-0}" != "1" ]]; then
      die "VITE_AUTH_DEV_BYPASS=true — refusing to deploy a cloud frontend that skips MSAL while the api enforces bearer tokens. Run 'unset VITE_AUTH_DEV_BYPASS' (or export VITE_AUTH_DEV_BYPASS=false) and retry."
    fi
    # Guard: refuse to bake a frontend whose MSAL client id does not match the
    # target api sidecar's API_CLIENT_ID (the wrong-tenant sibling of the two
    # guards above). Escape hatch: ELB_ALLOW_MSAL_CLIENT_MISMATCH=1.
    assert_msal_client_matches_target "$API_CLIENT_ID_VAL"
    # Version stamp: ACR builds run without .git in context, so resolve on host.
    APP_VERSION_VAL="${APP_VERSION:-$(node -p "require('$REPO_ROOT/web/package.json').version" 2>/dev/null || echo 0.0.0)}"
    APP_BUILD_NUMBER_VAL="${APP_BUILD_NUMBER:-$(release_build_number)}"
    GIT_COMMIT_VAL="${GIT_COMMIT:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)}"
    BUILD_TIME_VAL="${BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
    BUILD_ARGS=(
      --build-arg "VITE_API_BASE_URL=$VITE_API_BASE_URL_VAL"
      --build-arg "VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS_VAL"
      --build-arg "VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI_VAL"
      --build-arg "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL"
      --build-arg "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL"
      --build-arg "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL"
      --build-arg "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL"
      --build-arg "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL"
      --build-arg "APP_VERSION=$APP_VERSION_VAL"
      --build-arg "APP_BUILD_NUMBER=$APP_BUILD_NUMBER_VAL"
      --build-arg "GIT_COMMIT=$GIT_COMMIT_VAL"
      --build-arg "BUILD_TIME=$BUILD_TIME_VAL"
    )
  elif [[ "$SIDECAR" == "terminal" ]]; then
    BUILD_ARGS=(
      --build-arg "TERMINAL_BASE_IMAGE=$(terminal_base_image)"
    )
  elif [[ "$SIDECAR" == "api" || "$SIDECAR" == "worker" || "$SIDECAR" == "beat" ]]; then
    # Bake the release version into the api image so /api/health reports it
    # (api/__init__.py reads APP_VERSION). ACR builds run without .git in
    # context, so resolve the values on the host.
    APP_VERSION_VAL="${APP_VERSION:-$(node -p "require('$REPO_ROOT/web/package.json').version" 2>/dev/null || echo 0.0.0)}"
    GIT_COMMIT_VAL="${GIT_COMMIT:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)}"
    BUILD_TIME_VAL="${BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
    BUILD_ARGS=(
      --build-arg "APP_VERSION=$APP_VERSION_VAL"
      --build-arg "APP_GIT_COMMIT=$GIT_COMMIT_VAL"
      --build-arg "APP_BUILD_TIME=$BUILD_TIME_VAL"
    )
  fi

  ts "==> Building $IMAGE_NAME:$TAG via ACR (no local Docker)"
  ts "    dockerfile=$DOCKERFILE  context=$BUILD_CTX"
  acr_ensure_build_access "$ACR_NAME"
  if [[ "$SIDECAR" == "terminal" ]]; then
    TERMINAL_BASE_REBUILD="$REBUILD_TERMINAL_BASE" ensure_terminal_base_image
  fi
  az acr build \
    --registry "$ACR_NAME" \
    --image "${IMAGE_NAME}:${TAG}" \
    --file "$DOCKERFILE" \
    "${BUILD_ARGS[@]}" \
    "$BUILD_CTX" \
    -o none

  if [[ "$SIDECAR" == "api" ]]; then
    ts "==> Building elb-prepare-db:$TAG via ACR for AKS database transfers"
    az acr build \
      --registry "$ACR_NAME" \
      --image "elb-prepare-db:${TAG}" \
      --image "elb-prepare-db:latest" \
      --file "$REPO_ROOT/aks/prepare-db/Dockerfile" \
      "$REPO_ROOT/aks/prepare-db" \
      -o none
  fi

  # Prune older manifests for this repo WHILE the ACR firewall is still open.
  # Steady-state ACR is publicNetworkAccess=Disabled, so this MUST precede
  # acr_restore_build_access below. Best-effort: never aborts the deploy.
  if [[ "$SIDECAR" == "api" ]]; then
    acr_prune_targets "$IMAGE_NAME" elb-prepare-db
  else
    acr_prune_targets "$IMAGE_NAME"
  fi

  acr_restore_build_access "$ACR_NAME"
  trap - EXIT

  ts "==> Build complete: $NEW_IMAGE"
else
  ts "==> --no-build: skipping ACR build; expecting tag '$TAG' to already exist for $IMAGE_NAME"
fi

if $BUILD_ONLY; then
  ts "==> --build-only: skipping Container App PATCH for $SIDECAR ($NEW_IMAGE)"
  ts "==> Done. Tag was: $TAG"
  exit 0
fi

# --------------------------------------------------------------------------
# api / worker / beat all share the elb-api image. When the user runs
# `quick-deploy.sh api` we ALSO bump worker + beat so they pick up the
# new task code; otherwise the worker would keep running stale logic
# while the api fronts new logic — exactly the scenario that caused the
# Celery routing trap to look like an infra bug last week.
# --------------------------------------------------------------------------
declare -a TARGETS
case "$SIDECAR" in
  api)              TARGETS=(api worker beat) ;;
  worker)           TARGETS=(worker) ;;
  beat)             TARGETS=(beat) ;;
  frontend)         TARGETS=(frontend) ;;
  terminal)         TARGETS=(terminal) ;;
esac

# Pin the mutable tag to its digest so the PATCH rolls a new revision even
# when the active revision already references the same tag (see
# resolve_image_digest in the helpers block).
NEW_IMAGE="$(resolve_image_digest "$NEW_IMAGE")"

for tgt in "${TARGETS[@]}"; do
  ts "==> Patching container '$tgt' on $CONTAINER_APP_NAME → $NEW_IMAGE"
  # Reconcile cpu/memory to the Bicep template (single source of truth) so a
  # committed sizing change lands on a fast deploy instead of being preserved
  # at the live value. Empty when unparseable preserves live resources.
  _cpu=""
  _memory=""
  _res="$(container_desired_resources "$tgt")"
  if [[ -n "$_res" ]]; then
    _cpu="${_res%% *}"
    _memory="${_res##* }"
    ts "    + reconciling '$tgt' resources from Bicep → cpu=${_res%% *} memory=${_res##* }"
  elif [[ -f "$CONTROL_PLANE_BICEP_FILE" ]]; then
    ts "    ! could not parse '$tgt' resources from Bicep — PATCH keeps live cpu/memory"
  fi
  _env_pairs=()
  if [[ "$tgt" == "frontend" && "$NO_BUILD" != "true" ]]; then
    _env_pairs=(
      "VITE_API_BASE_URL=$VITE_API_BASE_URL_VAL"
      "VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS_VAL"
      "VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI_VAL"
      "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL"
      "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL"
      "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL"
      "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL"
      "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL"
      "API_CLIENT_ID=$API_CLIENT_ID_VAL"
      "AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL"
    )
  else
    mapfile -t _env_pairs < <(control_plane_env_pairs "$tgt")
    case "$tgt" in api | worker | beat) servicebus_gate_notice ;; esac
    [[ "$tgt" == "api" ]] && sync_openapi_shared_token
  fi

  if [[ ${#_env_pairs[@]} -gt 0 ]]; then
    ts "    + applying ${#_env_pairs[@]} exact-container runtime env var(s) for '$tgt'"
  elif [[ ! -f "$CONTROL_PLANE_ENV_FILE" && "$tgt" != "frontend" ]]; then
    ts "    ! control-plane env file missing ($CONTROL_PLANE_ENV_FILE) — '$tgt' guard env NOT applied"
  else
    ts "    (no runtime env changes for '$tgt')"
  fi
  containerapp_patch_container \
    "$tgt" "$NEW_IMAGE" "$_cpu" "$_memory" "${_env_pairs[@]}"
done

ts "==> Latest revision:"
az containerapp revision list \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query "sort_by([], &properties.createdTime)[-1].{name:name, active:properties.active, state:properties.runningState, replicas:properties.replicas, created:properties.createdTime}" \
  -o table || true

if $TAIL_LOGS; then
  ts "==> Tailing logs (Ctrl-C to exit) for container '${TARGETS[0]}'"
  az containerapp logs show \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --container "${TARGETS[0]}" \
    --follow \
    --tail 20
fi

# --------------------------------------------------------------------------
# Optional Service Bus integration RBAC. The namespace is normally chosen at
# runtime from Settings (so quick-deploy cannot know it), but when the operator
# exports SERVICEBUS_NAMESPACE (+ optional SERVICEBUS_NAMESPACE_RG) we grant the
# shared managed identity the two data-plane roles it needs to drain requests
# and publish completions over Entra. Idempotent: an existing assignment is a
# no-op. SAS-mode / cross-tenant namespaces are skipped (Entra cannot reach
# them) — those use a connection-string secret instead. Never narrows a role
# (charter §12a Rule 1: additive only).
# --------------------------------------------------------------------------
ensure_service_bus_rbac() {
  local ns="${SERVICEBUS_NAMESPACE:-}"
  [[ -n "$ns" ]] || { ts "    (SERVICEBUS_NAMESPACE unset — skipping Service Bus RBAC grant)"; return 0; }
  local ns_rg="${SERVICEBUS_NAMESPACE_RG:-$AZURE_RESOURCE_GROUP}"
  local mi_principal
  mi_principal="$(az identity list \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --query "[?starts_with(name,'id-elb-dashboard')].principalId | [0]" \
    -o tsv 2>/dev/null || true)"
  if [[ -z "$mi_principal" || "$mi_principal" == "None" ]]; then
    ts "    ! could not resolve shared MI principal in '$AZURE_RESOURCE_GROUP' — skipping Service Bus RBAC"
    return 0
  fi
  local ns_id
  ns_id="$(az servicebus namespace show \
    --name "$ns" --resource-group "$ns_rg" --query id -o tsv 2>/dev/null || true)"
  if [[ -z "$ns_id" ]]; then
    ts "    ! Service Bus namespace '$ns' not found in '$ns_rg' — skipping RBAC grant"
    return 0
  fi
  local role
  for role in "Azure Service Bus Data Sender" "Azure Service Bus Data Receiver"; do
    if az role assignment create \
      --assignee-object-id "$mi_principal" --assignee-principal-type ServicePrincipal \
      --role "$role" --scope "$ns_id" --only-show-errors >/dev/null 2>&1; then
      ts "    + granted '$role' to shared MI on $ns"
    else
      ts "    (role '$role' already present or grant skipped for $ns)"
    fi
  done
}

# Only relevant for the api/worker/beat image (which runs the integration).
case "$SIDECAR" in
  api|worker|beat) ensure_service_bus_rbac ;;
esac

ts "==> Done. Tag was: $TAG"
ts "    To roll back: scripts/dev/quick-deploy.sh $SIDECAR <previous-tag>"

#!/usr/bin/env bash
# Quick single-sidecar deploy for the bundled Container App.
#
# When a code-only fix in api/ or web/ or terminal/ needs to land on the
# real Azure revision, running the full postprovision (3 parallel ACR
# builds + a Bicep redeploy of all six sidecars) takes 5-10 minutes. This
# script does a far smaller cycle:
#
#   1. Build ONE image via `az acr build` (cached layers, ~30-90 s).
#   2. Patch ONLY that container's image via `az containerapp update`
#      (one ARM transaction, ~20-30 s — does NOT touch sidecar layout,
#      secrets, probes, or scale rules).
#   3. (Optional) tail the new revision's logs.
#
# It refuses to touch sidecar structure (secrets, probes, volumes) — for
# those changes you still need a Bicep redeploy via postprovision.sh
# or `az deployment group create --template-file containerAppControl.bicep`.
# The frontend sidecar is the only exception for env vars: its runtime
# config is generated from server environment variables at startup, so
# this script keeps those values in sync during fast frontend deploys.
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

ts() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

release_build_number() {
  local latest_tag=""
  latest_tag="$(git -C "$REPO_ROOT" tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname --merged HEAD 2>/dev/null | head -n1 || true)"
  if [[ -n "$latest_tag" ]]; then
    git -C "$REPO_ROOT" rev-list --count "$latest_tag..HEAD" 2>/dev/null || printf '0\n'
  else
    git -C "$REPO_ROOT" rev-list --count HEAD 2>/dev/null || printf '0\n'
  fi
}

strip_quotes() {
  local value="${1:-}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "$value"
}

load_simple_env_file() {
  local file="${1:-}"
  [[ -f "$file" ]] || return 0
  shift || true
  local -A SKIP=()
  local k
  for k in "$@"; do SKIP["$k"]=1; done
  while IFS='=' read -r key value; do
    [[ -n "${key:-}" ]] || continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -z "${SKIP[$key]:-}" ]] || continue
    value="$(strip_quotes "${value:-}")"
    if [[ -z "${!key:-}" ]]; then
      export "$key=$value"
    fi
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$file" || true)
}

load_azd_env() {
  command -v azd >/dev/null 2>&1 || return 0
  command -v timeout >/dev/null 2>&1 || return 0
  local values
  values="$(timeout 8s azd env get-values 2>/dev/null || true)"
  while IFS='=' read -r key value; do
    [[ -n "${key:-}" ]] || continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="$(strip_quotes "${value:-}")"
    if [[ -z "${!key:-}" ]]; then
      export "$key=$value"
    fi
  done <<< "$values"
}

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

load_simple_env_file "$REPO_ROOT/.env"
load_simple_env_file "$REPO_ROOT/.env.local"
load_simple_env_file "$REPO_ROOT/web/.env.production"
# web/.env.local exists for local-dev (vite dev server + local-run.sh web)
# and pins VITE_API_BASE_URL=http://localhost:8085. That value must NEVER
# end up in a cloud frontend's runtime-config.js — see the guard below and
# docs/features_change/2026-05/2026-05-21-frontend-api-base-url-guard.md.
load_simple_env_file "$REPO_ROOT/web/.env.local" VITE_API_BASE_URL
if [[ -z "${AZURE_RESOURCE_GROUP:-}" || -z "${ACR_NAME:-}" || -z "${ACR_LOGIN_SERVER:-}" || -z "${CONTAINER_APP_NAME:-}" ]]; then
  load_azd_env
fi

[[ $# -ge 1 ]] || die "usage: $0 <api|worker|beat|frontend|terminal|all> [tag] [--logs] [--rebuild-terminal-base]"

SIDECAR="$1"; shift || true
TAG=""
TAIL_LOGS=false
REBUILD_TERMINAL_BASE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --logs) TAIL_LOGS=true ;;
    --rebuild-terminal-base) REBUILD_TERMINAL_BASE=true ;;
    -*)     die "unknown flag: $1" ;;
    *)      TAG="$1" ;;
  esac
  shift
done
[[ -n "$TAG" ]] || TAG="$(date +%Y%m%d%H%M%S)"

if [[ "$SIDECAR" == "all" ]]; then
  ts "==> Deploying all quick-deploy targets with tag: $TAG"
  for target in api frontend terminal; do
    ts "==> Dispatching quick deploy target: $target"
    if [[ "$target" == "terminal" && "$REBUILD_TERMINAL_BASE" == "true" ]]; then
      "$REPO_ROOT/scripts/dev/quick-deploy.sh" "$target" "$TAG" --rebuild-terminal-base
    else
      "$REPO_ROOT/scripts/dev/quick-deploy.sh" "$target" "$TAG"
    fi
  done
  if $TAIL_LOGS; then
    for v in AZURE_RESOURCE_GROUP CONTAINER_APP_NAME; do
      [[ -n "${!v:-}" ]] || die "$v is unset (try: source /tmp/azd-env.sh)"
    done
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

for v in AZURE_RESOURCE_GROUP ACR_NAME ACR_LOGIN_SERVER CONTAINER_APP_NAME; do
  [[ -n "${!v:-}" ]] || die "$v is unset (try: source /tmp/azd-env.sh)"
done
if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID"
fi
ensure_provider_registration_once

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

acr_restore_build_access "$ACR_NAME"

ts "==> Build complete: $NEW_IMAGE"

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

for tgt in "${TARGETS[@]}"; do
  ts "==> Patching container '$tgt' on $CONTAINER_APP_NAME → $NEW_IMAGE"
  if [[ "$tgt" == "frontend" ]]; then
    az containerapp update \
      --name "$CONTAINER_APP_NAME" \
      --resource-group "$AZURE_RESOURCE_GROUP" \
      --container-name "$tgt" \
      --image "$NEW_IMAGE" \
      --set-env-vars \
        "VITE_API_BASE_URL=$VITE_API_BASE_URL_VAL" \
        "VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS_VAL" \
        "VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI_VAL" \
        "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL" \
        "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL" \
        "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL" \
        "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL" \
        "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL" \
        "API_CLIENT_ID=$API_CLIENT_ID_VAL" \
        "AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL" \
      -o none
  else
    az containerapp update \
      --name "$CONTAINER_APP_NAME" \
      --resource-group "$AZURE_RESOURCE_GROUP" \
      --container-name "$tgt" \
      --image "$NEW_IMAGE" \
      -o none
  fi
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

ts "==> Done. Tag was: $TAG"
ts "    To roll back: scripts/dev/quick-deploy.sh $SIDECAR <previous-tag>"

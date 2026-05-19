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
# Sidecars: api | worker | beat | frontend | terminal
#   (worker and beat reuse the api image — passing either rebuilds api
#    and points the worker / beat container at the new tag.)
#
# Examples:
#   scripts/dev/quick-deploy.sh api
#   scripts/dev/quick-deploy.sh terminal
#   scripts/dev/quick-deploy.sh frontend custom-tag-123
#   scripts/dev/quick-deploy.sh api --logs        # tail after deploy
#
# Required env (export them or `source /tmp/azd-env.sh`):
#   AZURE_RESOURCE_GROUP         e.g. rg-elb-ca
#   ACR_NAME                     short name (no .azurecr.io)
#   ACR_LOGIN_SERVER             e.g. crelbXYZ.azurecr.io
#   CONTAINER_APP_NAME           e.g. ca-elb-control

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

ts() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

strip_quotes() {
  local value="${1:-}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "$value"
}

load_simple_env_file() {
  local file="${1:-}"
  [[ -f "$file" ]] || return 0
  while IFS='=' read -r key value; do
    [[ -n "${key:-}" ]] || continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
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

load_simple_env_file "$REPO_ROOT/.env"
load_simple_env_file "$REPO_ROOT/.env.local"
load_simple_env_file "$REPO_ROOT/web/.env.production"
load_simple_env_file "$REPO_ROOT/web/.env.local"
if [[ -z "${AZURE_RESOURCE_GROUP:-}" || -z "${ACR_NAME:-}" || -z "${ACR_LOGIN_SERVER:-}" || -z "${CONTAINER_APP_NAME:-}" ]]; then
  load_azd_env
fi

[[ $# -ge 1 ]] || die "usage: $0 <api|worker|beat|frontend|terminal> [tag] [--logs]"

SIDECAR="$1"; shift || true
TAG=""
TAIL_LOGS=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --logs) TAIL_LOGS=true ;;
    -*)     die "unknown flag: $1" ;;
    *)      TAG="$1" ;;
  esac
  shift
done
[[ -n "$TAG" ]] || TAG="$(date +%Y%m%d%H%M%S)"

case "$SIDECAR" in
  api|worker|beat) IMAGE_NAME="elb-api";       DOCKERFILE="api/Dockerfile";       BUILD_CTX="." ;;
  frontend)        IMAGE_NAME="elb-frontend";  DOCKERFILE="web/Dockerfile";       BUILD_CTX="." ;;
  terminal)        IMAGE_NAME="elb-terminal";  DOCKERFILE="terminal/Dockerfile";  BUILD_CTX="terminal/" ;;
  *) die "unknown sidecar '$SIDECAR' (expected: api|worker|beat|frontend|terminal)" ;;
esac

for v in AZURE_RESOURCE_GROUP ACR_NAME ACR_LOGIN_SERVER CONTAINER_APP_NAME; do
  [[ -n "${!v:-}" ]] || die "$v is unset (try: source /tmp/azd-env.sh)"
done

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
ACR_RESTORE_NETWORK=0

restore_acr_network() {
  if [[ "${ACR_RESTORE_NETWORK:-0}" == "1" ]]; then
    ts "==> Restoring ACR public network access to Disabled"
    az acr update \
      --name "$ACR_NAME" \
      --public-network-enabled false \
      --default-action Deny \
      -o none >/dev/null 2>&1 || ts "WARN: failed to restore ACR public network access"
  fi
}
trap restore_acr_network EXIT

declare -a BUILD_ARGS=()
if [[ "$SIDECAR" == "frontend" ]]; then
  [[ -n "$API_CLIENT_ID_VAL" ]] || die "API_CLIENT_ID/VITE_AZURE_CLIENT_ID is unset; set .env, web/.env.local, or azd env before deploying frontend"
  BUILD_ARGS=(
    --build-arg "VITE_API_BASE_URL=$VITE_API_BASE_URL_VAL"
    --build-arg "VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS_VAL"
    --build-arg "VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI_VAL"
    --build-arg "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL"
    --build-arg "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL"
    --build-arg "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL"
    --build-arg "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL"
    --build-arg "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL"
  )
fi

ts "==> Building $IMAGE_NAME:$TAG via ACR (no local Docker)"
ts "    dockerfile=$DOCKERFILE  context=$BUILD_CTX"
ACR_PUBLIC_ACCESS=$(az acr show --name "$ACR_NAME" --query publicNetworkAccess -o tsv 2>/dev/null || echo "")
if [[ "$ACR_PUBLIC_ACCESS" == "Disabled" ]]; then
  ts "==> Temporarily enabling ACR public network access for az acr build"
  az acr update \
    --name "$ACR_NAME" \
    --public-network-enabled true \
    --default-action Allow \
    -o none >/dev/null
  ACR_RESTORE_NETWORK=1
fi
az acr build \
  --registry "$ACR_NAME" \
  --image "${IMAGE_NAME}:${TAG}" \
  --file "$DOCKERFILE" \
  "${BUILD_ARGS[@]}" \
  "$BUILD_CTX" \
  -o none

restore_acr_network
ACR_RESTORE_NETWORK=0

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

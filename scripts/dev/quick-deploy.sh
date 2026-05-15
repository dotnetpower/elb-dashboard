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
#      env vars, secrets, probes, or scale rules).
#   3. (Optional) tail the new revision's logs.
#
# It refuses to touch sidecar structure (env, secrets, probes) — for
# those changes you still need a Bicep redeploy via postprovision.sh
# or `az deployment group create --template-file containerAppControl.bicep`.
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

ts "==> Building $IMAGE_NAME:$TAG via ACR (no local Docker)"
ts "    dockerfile=$DOCKERFILE  context=$BUILD_CTX"
az acr build \
  --registry "$ACR_NAME" \
  --image "${IMAGE_NAME}:${TAG}" \
  --file "$DOCKERFILE" \
  "$BUILD_CTX" \
  --no-logs \
  -o none

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
  az containerapp update \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --container-name "$tgt" \
    --image "$NEW_IMAGE" \
    -o none
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

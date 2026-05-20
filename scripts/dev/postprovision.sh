#!/usr/bin/env bash
# postprovision.sh — runs after `azd provision` succeeds.
#
# Responsibilities:
#   1. Build the api / frontend / terminal images via `az acr build`,
#      IN PARALLEL, with per-image log files so the operator can follow
#      progress in another terminal.
#   2. Swap the Container App to the six-sidecar layout via
#      `az containerapp update --yaml` (much faster than redeploying the
#      Bicep module — no template compile / what-if step).
#   3. Print the application URL and a one-line health summary.
#
# Idempotent. Re-running rebuilds the images with a fresh timestamp tag and
# re-applies the same yaml.
#
# To follow build progress in another terminal:
#   tail -f /tmp/elb-postprov-*-build-{api,frontend,terminal}.log

set -euo pipefail

# ---------------------------------------------------------------------------
# Inputs from azd outputs (loaded automatically into env by `azd up`).
# ---------------------------------------------------------------------------
REQUIRED_VARS=(
  AZURE_LOCATION
  AZURE_RESOURCE_GROUP
  ACR_NAME
  ACR_LOGIN_SERVER
  CONTAINER_APP_NAME
  CONTAINER_APP_FQDN
  SHARED_IDENTITY_RESOURCE_ID
  SHARED_IDENTITY_CLIENT_ID
  AZURE_TENANT_ID
)
for v in "${REQUIRED_VARS[@]}"; do
  if [ -z "${!v:-}" ]; then
    echo "FATAL: required env var $v not set. Did azd provision finish successfully?" >&2
    exit 1
  fi
done

API_CLIENT_ID_VAL="${API_CLIENT_ID:-}"
APPLICATIONINSIGHTS_CONNECTION_STRING_VAL="${APPLICATIONINSIGHTS_CONNECTION_STRING:-}"
VITE_FEATURE_CUSTOM_DB_VAL="${VITE_FEATURE_CUSTOM_DB:-true}"
VITE_FEATURE_LAB_TOOLS_VAL="${VITE_FEATURE_LAB_TOOLS:-true}"
VITE_FEATURE_TERMINAL_VAL="${VITE_FEATURE_TERMINAL:-true}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$REPO_ROOT/scripts/dev/acr-build-access.sh"
. "$REPO_ROOT/scripts/dev/terminal-base-image.sh"
TAG="$(date -u +%Y%m%d%H%M%S)"
T0=$(date +%s)

LOG_DIR="/tmp/elb-postprov-$TAG"
mkdir -p "$LOG_DIR"

trap 'acr_restore_build_access "$ACR_NAME"' EXIT

# ---------------------------------------------------------------------------
# Pretty timestamped logger so the operator can see real-time progress.
# Format: [HH:MM:SS +Xm Ys] message
# ---------------------------------------------------------------------------
ts() {
  local now elapsed mins secs hms
  now=$(date +%s)
  elapsed=$(( now - T0 ))
  mins=$(( elapsed / 60 ))
  secs=$(( elapsed % 60 ))
  hms=$(date -u +%H:%M:%S)
  printf '[%s +%dm%02ds] %s\n' "$hms" "$mins" "$secs" "$1"
}

ts "==> Postprovision starting"
ts "    RG:        $AZURE_RESOURCE_GROUP"
ts "    ACR:       $ACR_NAME ($ACR_LOGIN_SERVER)"
ts "    App:       $CONTAINER_APP_NAME ($CONTAINER_APP_FQDN)"
ts "    Image tag: $TAG"
ts "    Logs:      $LOG_DIR/*.log"
ts "    Tip:       follow in another terminal:"
ts "                 tail -f $LOG_DIR/build-*.log"

# ---------------------------------------------------------------------------
# 1. Parallel image builds. Each build writes to its own log file; the
#    parent process waits for all three and reports any failures.
# ---------------------------------------------------------------------------
build_image() {
  local pid_var="${1:-}"
  local image_name="${2:-}"
  local dockerfile="${3:-}"
  local context="${4:-}"
  local log="$LOG_DIR/build-${image_name}.log"
  local extra_args=()
  if [ -z "$pid_var" ] || [ -z "$image_name" ] || [ -z "$dockerfile" ] || [ -z "$context" ]; then
    echo "build_image: missing arg (image=$image_name dockerfile=$dockerfile context=$context)" >&2
    return 1
  fi
  if [ "$image_name" = "elb-frontend" ]; then
    extra_args=(
      --build-arg "VITE_API_BASE_URL="
      --build-arg "VITE_AUTH_DEV_BYPASS=false"
      --build-arg "VITE_AZURE_REDIRECT_URI=__RUNTIME__"
      --build-arg "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID"
      --build-arg "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL"
      --build-arg "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL"
      --build-arg "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL"
      --build-arg "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL"
    )
  elif [ "$image_name" = "elb-terminal" ]; then
    extra_args=(
      --build-arg "TERMINAL_BASE_IMAGE=$(terminal_base_image)"
    )
  fi
  {
    echo "[build-$image_name] starting at $(date -u +%H:%M:%S)"
    az acr build \
      --registry "$ACR_NAME" \
      --image "${image_name}:${TAG}" \
      --image "${image_name}:latest" \
      --file "$dockerfile" \
      "${extra_args[@]}" \
      "$context" \
      --output none
    rc=$?
    echo "[build-$image_name] finished at $(date -u +%H:%M:%S), rc=$rc"
    exit $rc
  } > "$log" 2>&1 &
  printf -v "$pid_var" '%s' "$!"
}

ts "==> Building 3 images in parallel via az acr build (no local Docker needed)"
acr_ensure_build_access "$ACR_NAME"
ensure_terminal_base_image
build_image PID_API      "elb-api"      "$REPO_ROOT/api/Dockerfile"      "$REPO_ROOT"
build_image PID_FRONTEND "elb-frontend" "$REPO_ROOT/web/Dockerfile"      "$REPO_ROOT"
build_image PID_TERMINAL "elb-terminal" "$REPO_ROOT/terminal/Dockerfile.runtime" "$REPO_ROOT/terminal"

ts "    elb-api:      pid=$PID_API"
ts "    elb-frontend: pid=$PID_FRONTEND"
ts "    elb-terminal: pid=$PID_TERMINAL"

# Poll every 15s and report which builds are still running.
declare -A RUNNING
RUNNING["elb-api"]=$PID_API
RUNNING["elb-frontend"]=$PID_FRONTEND
RUNNING["elb-terminal"]=$PID_TERMINAL

while [ ${#RUNNING[@]} -gt 0 ]; do
  sleep 15
  finished=()
  for name in "${!RUNNING[@]}"; do
    pid=${RUNNING["$name"]}
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null
      rc=$?
      if [ "$rc" = "0" ]; then
        ts "    ✓ $name finished (rc=0)"
      else
        ts "    ✗ $name FAILED (rc=$rc) — see $LOG_DIR/build-$name.log"
        # Echo the last lines of the failed log so the failure is visible
        # even when nobody is following the log file.
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

# Final pass: any non-zero exit means abort.
fail=0
for name in elb-api elb-frontend elb-terminal; do
  if ! grep -q "rc=0$" "$LOG_DIR/build-$name.log" 2>/dev/null; then
    fail=1
    ts "✗ build $name did not produce rc=0"
  fi
done
if [ "$fail" = "1" ]; then
  ts "Aborting: at least one image build failed."
  exit 1
fi
ts "==> All 3 images built and pushed"

# ---------------------------------------------------------------------------
# 2. Swap the Container App template to the six-sidecar layout.
#    `az containerapp update --yaml` is much faster than redeploying the
#    Bicep module because it skips template compilation and what-if.
# ---------------------------------------------------------------------------
ts "==> Building Container App yaml"

ALLOWED_ORIGINS_VAL="${ALLOWED_ORIGINS:-}"
ALLOWED_ORIGINS_JSON="[]"
if [ -n "$ALLOWED_ORIGINS_VAL" ]; then
  ALLOWED_ORIGINS_JSON=$(echo "$ALLOWED_ORIGINS_VAL" | python3 -c 'import json,sys; print(json.dumps([s.strip() for s in sys.stdin.read().split(",") if s.strip()]))')
fi

# Use the same Bicep module so the layout is single-source-of-truth, but go
# through `az deployment group create` once. This is the only step that
# needs the full Bicep flow because the volumes/env wiring is non-trivial.
ts "==> Deploying six-sidecar layout via Bicep (one shot)"
DEPLOY_NAME="ca-swap-$TAG"
SUB_ID=$(az account show --query id -o tsv)
ENV_RID="/subscriptions/$SUB_ID/resourceGroups/$AZURE_RESOURCE_GROUP/providers/Microsoft.App/managedEnvironments/${CONTAINER_ENV_NAME:-cae-elb}"

az deployment group create \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$DEPLOY_NAME" \
  --template-file "$REPO_ROOT/infra/modules/containerAppControl.bicep" \
  --parameters \
      location="$AZURE_LOCATION" \
      appName="$CONTAINER_APP_NAME" \
      environmentResourceId="$ENV_RID" \
      acrLoginServer="$ACR_LOGIN_SERVER" \
      apiImageTag="$TAG" \
      frontendImageTag="$TAG" \
      terminalImageTag="$TAG" \
      useBootstrapImage=false \
      sharedIdentityResourceId="$SHARED_IDENTITY_RESOURCE_ID" \
      sharedIdentityClientId="$SHARED_IDENTITY_CLIENT_ID" \
      tenantId="$AZURE_TENANT_ID" \
      apiClientId="$API_CLIENT_ID_VAL" \
      featureCustomDb="$VITE_FEATURE_CUSTOM_DB_VAL" \
      featureLabTools="$VITE_FEATURE_LAB_TOOLS_VAL" \
      featureTerminal="$VITE_FEATURE_TERMINAL_VAL" \
      applicationInsightsConnectionString="$APPLICATIONINSIGHTS_CONNECTION_STRING_VAL" \
      platformStorageAccountName="${STORAGE_ACCOUNT_NAME:-}" \
      subscriptionId="$(az account show --query id -o tsv)" \
      allowedOrigins="$ALLOWED_ORIGINS_JSON" \
  --output none \
  > "$LOG_DIR/swap.log" 2>&1 &
SWAP_PID=$!

# While the swap deploys, print a heartbeat so the operator knows we're alive.
while kill -0 "$SWAP_PID" 2>/dev/null; do
  sleep 10
  ts "    swap deployment $DEPLOY_NAME still running ..."
done
wait "$SWAP_PID"
SWAP_RC=$?
if [ "$SWAP_RC" != "0" ]; then
  ts "✗ swap deployment failed (rc=$SWAP_RC). Last lines:"
  tail -30 "$LOG_DIR/swap.log" | sed 's/^/      /'
  exit "$SWAP_RC"
fi
ts "==> Container App updated to six-sidecar layout"

# ---------------------------------------------------------------------------
# 3. Wait for /api/health on the new revision and print URL.
# ---------------------------------------------------------------------------
ts "==> Waiting up to 180s for /api/health on the new revision..."
URL="https://$CONTAINER_APP_FQDN/api/health"
ok=0
for i in $(seq 1 36); do
  status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$URL" 2>/dev/null || echo "000")
  if [ "$status" = "200" ]; then
    ok=1
    ts "    ✓ /api/health → 200 OK (attempt $i)"
    break
  fi
  if (( i % 4 == 0 )); then
    ts "    health check attempt $i: status=$status (sleeping 5s)"
  fi
  sleep 5
done

echo
echo "============================================================"
if [ "$ok" = "1" ]; then
  ts "✓ Deployment OK."
else
  ts "⚠ Container App deployed but /api/health did not respond 200 within 180s."
  ts "  Check container logs:"
  ts "    az containerapp logs show -n $CONTAINER_APP_NAME -g $AZURE_RESOURCE_GROUP --container api --tail 100"
  ts "  Check system events:"
  ts "    az containerapp logs show -n $CONTAINER_APP_NAME -g $AZURE_RESOURCE_GROUP --type system --tail 30"
fi
ts "  URL:        https://$CONTAINER_APP_FQDN"
ts "  RG:         $AZURE_RESOURCE_GROUP"
ts "  Logs dir:   $LOG_DIR"
echo "============================================================"

# Soft-fail policy: do not break azd up just because health was slow to
# come up. Hard-fail above stays for image-build / swap-deploy errors.
exit 0

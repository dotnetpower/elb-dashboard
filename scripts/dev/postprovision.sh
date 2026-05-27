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
  CONTAINER_ENV_NAME
  SHARED_IDENTITY_RESOURCE_ID
  SHARED_IDENTITY_CLIENT_ID
  AZURE_TENANT_ID
  AZURE_SUBSCRIPTION_ID
  STORAGE_ACCOUNT_NAME
)
for v in "${REQUIRED_VARS[@]}"; do
  if [ -z "${!v:-}" ]; then
    echo "FATAL: required env var $v not set. azd provision may have failed or the azd env was not loaded." >&2
    echo "       Re-run 'azd provision' or inspect 'azd env get-values' before postprovision." >&2
    exit 1
  fi
done

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

progress() {
  bash "$REPO_ROOT/scripts/dev/azd-progress.sh" "$@"
}

ensure_spa_redirect_uri() {
  local app_id="$1"
  local redirect_uri="$2"
  local object_id current_redirects redirects

  if ! command -v jq >/dev/null 2>&1; then
    echo "FATAL: jq is required to update App Registration redirect URIs." >&2
    exit 1
  fi

  object_id="$(az ad app show --id "$app_id" --query id -o tsv --only-show-errors 2>/dev/null || true)"
  if [ -z "$object_id" ]; then
    cat >&2 <<EOF
FATAL: cannot find App Registration '$app_id' in the active Azure CLI tenant.

The Azure CLI subscription must match AZURE_SUBSCRIPTION_ID before postprovision
updates the SPA redirect URI for the deployed Container App origin.
EOF
    exit 1
  fi

  current_redirects="$(az rest \
    --method GET \
    --uri "https://graph.microsoft.com/v1.0/applications/$object_id?\$select=spa" \
    --only-show-errors \
    | jq -c '.spa.redirectUris // []')"

  if jq -e --arg uri "$redirect_uri" 'index($uri) != null' <<<"$current_redirects" >/dev/null; then
    progress note "App Registration already includes SPA redirect URI $redirect_uri."
    return 0
  fi

  redirects="$(jq -cn \
    --arg uri "$redirect_uri" \
    --argjson existing "$current_redirects" \
    '$existing + [$uri] | unique')"
  az rest \
    --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/applications/$object_id" \
    --headers "Content-Type=application/json" \
    --body "{\"spa\":{\"redirectUris\":$redirects}}" \
    --only-show-errors >/dev/null
  progress note "Added SPA redirect URI $redirect_uri to the App Registration."
}

# Ensure az CLI uses the correct subscription for all subsequent calls
# (including App Registration setup and sourced helper scripts). This matters
# when the script is run directly after a failed azd hook rather than through
# `azd up`.
if [ -n "${AZURE_SUBSCRIPTION_ID:-}" ]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID"
fi
if [ "${ELB_PROVIDER_REGISTRATION_READY:-}" = "true" ]; then
  progress note "Provider registration already completed by ./deploy.sh; skipping postprovision refresh."
else
  progress note "Refreshing provider registration state before postprovision work."
  bash "$REPO_ROOT/scripts/dev/register-providers.sh" --subscription "$AZURE_SUBSCRIPTION_ID"
fi

progress step 4 "App registration" "Create/reuse the Entra App Registration if API_CLIENT_ID is empty."
API_CLIENT_ID_VAL="${API_CLIENT_ID:-}"
# Defensive: a non-empty API_CLIENT_ID inherited from a previous deploy in a
# different tenant (e.g. azd env retargeted, or a process env var leaking
# in from another shell) would otherwise FATAL inside
# `ensure_spa_redirect_uri` with "cannot find App Registration ... in the
# active Azure CLI tenant". Re-running deploy.sh on a fresh subscription
# is a common, supported workflow, so we silently clear the stale id and
# fall through to the standard create/reuse path instead of refusing to
# proceed.
if [ -n "$API_CLIENT_ID_VAL" ]; then
  if ! az ad app show --id "$API_CLIENT_ID_VAL" --query id -o tsv --only-show-errors >/dev/null 2>&1; then
    progress note "API_CLIENT_ID '$API_CLIENT_ID_VAL' does not exist in the active Azure CLI tenant; clearing it and creating a fresh App Registration."
    if command -v azd >/dev/null 2>&1; then
      # Persist the clear back to the azd env file so a subsequent
      # `azd env get-values` does not surface the stale value again.
      # `apiClientId` is the Bicep parameter alias also set by some
      # earlier deploy.sh revisions — clear both to stay idempotent.
      azd env set API_CLIENT_ID "" >/dev/null 2>&1 || true
      azd env set apiClientId "" >/dev/null 2>&1 || true
    fi
    API_CLIENT_ID_VAL=""
    unset API_CLIENT_ID
  fi
fi
if [ -z "$API_CLIENT_ID_VAL" ]; then
  echo "==> API_CLIENT_ID is not set; creating or reusing the Entra App Registration..."
  ADDITIONAL_REDIRECT_URIS="https://$CONTAINER_APP_FQDN" \
    bash "$REPO_ROOT/scripts/dev/setup-app-registration.sh" "${APP_REGISTRATION_NAME:-elastic-blast-control-plane}"
  if command -v azd >/dev/null 2>&1; then
    API_CLIENT_ID_VAL="$(azd env get-values 2>/dev/null | awk -F= '/^API_CLIENT_ID=/{gsub(/"/, "", $2); print $2; exit}')"
  fi
  API_CLIENT_ID_VAL="${API_CLIENT_ID_VAL:-}"
  if [ -z "$API_CLIENT_ID_VAL" ]; then
    cat >&2 <<'EOF'
FATAL: App Registration setup finished, but API_CLIENT_ID is still unset.

Run scripts/dev/setup-app-registration.sh manually and re-run azd up.
EOF
    exit 1
  fi
  export API_CLIENT_ID="$API_CLIENT_ID_VAL"
else
  progress note "API_CLIENT_ID is already set; reusing the configured App Registration."
fi
ensure_spa_redirect_uri "$API_CLIENT_ID_VAL" "https://$CONTAINER_APP_FQDN"
progress "done" 4 "App registration"
APPLICATIONINSIGHTS_CONNECTION_STRING_VAL="${APPLICATIONINSIGHTS_CONNECTION_STRING:-}"
# Live Wall log-tail fallback target. Empty is acceptable (the api sidecar
# then renders the historical "blank tile" state); when present, the api
# uses LogsQueryClient to KQL `ContainerAppConsoleLogs_CL`.
LOG_ANALYTICS_WORKSPACE_ID_VAL="${LOG_ANALYTICS_WORKSPACE_ID:-}"
VITE_FEATURE_CUSTOM_DB_VAL="${VITE_FEATURE_CUSTOM_DB:-true}"
VITE_FEATURE_LAB_TOOLS_VAL="${VITE_FEATURE_LAB_TOOLS:-true}"
VITE_FEATURE_TERMINAL_VAL="${VITE_FEATURE_TERMINAL:-true}"

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

release_version() {
  local value=""
  if command -v node >/dev/null 2>&1; then
    value="$(node -p "require('$REPO_ROOT/web/package.json').version" 2>/dev/null || true)"
  fi
  if [ -z "$value" ]; then
    value="$(grep -E '"version"[[:space:]]*:' "$REPO_ROOT/web/package.json" | head -n1 | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' || true)"
  fi
  printf '%s\n' "${value:-0.0.0}"
}

release_build_number() {
  local latest_tag=""
  latest_tag="$(git -C "$REPO_ROOT" tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname --merged HEAD 2>/dev/null | head -n1 || true)"
  if [ -n "$latest_tag" ]; then
    git -C "$REPO_ROOT" rev-list --count "$latest_tag..HEAD" 2>/dev/null || printf '0\n'
  else
    git -C "$REPO_ROOT" rev-list --count HEAD 2>/dev/null || printf '0\n'
  fi
}

short_commit() {
  git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || printf 'dev\n'
}

ts "==> Postprovision starting"
ts "    RG:        $AZURE_RESOURCE_GROUP"
ts "    ACR:       $ACR_NAME ($ACR_LOGIN_SERVER)"
ts "    App:       $CONTAINER_APP_NAME ($CONTAINER_APP_FQDN)"
ts "    Storage:   $STORAGE_ACCOUNT_NAME"
ts "    Image tag: $TAG"
ts "    Logs:      $LOG_DIR/*.log"
ts "    Tip:       follow in another terminal:"
ts "                 tail -f $LOG_DIR/build-*.log"

validate_storage_account() {
  local hns kind public_network_access

  ts "==> Validating platform Storage account ARM properties"
  kind="$(az storage account show -g "$AZURE_RESOURCE_GROUP" -n "$STORAGE_ACCOUNT_NAME" --query kind -o tsv --only-show-errors 2>/dev/null || true)"
  hns="$(az storage account show -g "$AZURE_RESOURCE_GROUP" -n "$STORAGE_ACCOUNT_NAME" --query isHnsEnabled -o tsv --only-show-errors 2>/dev/null || true)"
  public_network_access="$(az storage account show -g "$AZURE_RESOURCE_GROUP" -n "$STORAGE_ACCOUNT_NAME" --query publicNetworkAccess -o tsv --only-show-errors 2>/dev/null || true)"
  if [ -z "$kind" ] || [ -z "$hns" ]; then
    cat >&2 <<EOF
FATAL: cannot read Storage account ARM properties for '$STORAGE_ACCOUNT_NAME' in '$AZURE_RESOURCE_GROUP'.

This is an ARM/provider/RBAC lookup failure, not a data-plane or browser issue.
Confirm Microsoft.Storage is registered and the deployer can read Microsoft.Storage/storageAccounts/read.
EOF
    exit 1
  fi
  if [ "$(printf '%s' "$hns" | tr '[:upper:]' '[:lower:]')" != "true" ]; then
    cat >&2 <<EOF
FATAL: Storage account '$STORAGE_ACCOUNT_NAME' was created with isHnsEnabled=$hns.

The Bicep contract requires ADLS Gen2 / HNS for ElasticBLAST workload containers.
Delete the failed environment or use a new azd environment name so azd up can recreate storage with HNS enabled.
EOF
    exit 1
  fi
  ts "    ✓ Storage kind=$kind HNS=$hns publicNetworkAccess=${public_network_access:-unknown}"
  # Production posture per .github/copilot-instructions.md §9 is
  # `publicNetworkAccess: Disabled` (private endpoints only). The first
  # `azd up` intentionally provisions Storage / KV / ACR with
  # `lockdownPrivateNetworking=false` so postprovision can push images
  # and seed secrets over the public path. Surface a clear next-step
  # banner whenever the Storage account is still in that bootstrap
  # state so the operator does not forget to flip the lockdown on a
  # follow-up `azd provision`. The dashboard's Storage card relies on
  # this — see web/src/components/cards/storage/StorageWarnings.tsx
  # "Private only" banner.
  if [ "${public_network_access:-}" = "Enabled" ]; then
    ts ""
    ts "    ℹ Storage / Key Vault / ACR are still in BOOTSTRAP posture (public path open)."
    ts "       This is the expected first-deploy state so postprovision can push images and"
    ts "       seed secrets. Once the workspace is ready, lock the data plane down with:"
    ts ""
    ts "           azd env set --environment \"\$AZURE_ENV_NAME\" LOCKDOWN_PRIVATE_NETWORKING true"
    ts "           azd provision"
    ts ""
    ts "       After the second provision, every workload Storage account flips to"
    ts "       publicNetworkAccess=Disabled and the dashboard's Storage card shows"
    ts "       'Private only'. Charter §9 — production posture is private-only."
  fi
}

progress step 5 "Resource validation" "Validate Storage HNS and merge dashboard discovery tags."
validate_storage_account

resolve_platform_private_endpoint_subnet_id() {
  local explicit subnet_id vnet_id resolved

  explicit="${PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID:-}"
  if [ -n "$explicit" ]; then
    printf '%s' "$explicit"
    return 0
  fi

  subnet_id="$(az containerapp env show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$CONTAINER_ENV_NAME" \
    --query 'properties.vnetConfiguration.infrastructureSubnetId' \
    -o tsv \
    --only-show-errors 2>/dev/null || true)"
  if [ -z "$subnet_id" ] || [[ "$subnet_id" != */subnets/* ]]; then
    cat >&2 <<EOF
FATAL: cannot resolve Container Apps Environment infrastructure subnet for '$CONTAINER_ENV_NAME'.

The six-sidecar template needs PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID so runtime
resource creation can attach workload Storage private endpoints to this
deployment's VNet. Re-run azd provision, or set PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID
to the snet-private-endpoints subnet resource id and re-run postprovision.
EOF
    exit 1
  fi

  vnet_id="${subnet_id%/subnets/*}"
  resolved="$vnet_id/subnets/snet-private-endpoints"
  if ! az network vnet subnet show --ids "$resolved" --query id -o tsv --only-show-errors >/dev/null 2>&1; then
    cat >&2 <<EOF
FATAL: cannot find private endpoint subnet derived from Container Apps VNet:
  $resolved

The deployment expects the network module to create a sibling subnet named
'snet-private-endpoints'. Verify infra/modules/network.bicep and re-run azd provision.
EOF
    exit 1
  fi
  printf '%s' "$resolved"
}

tag_workspace_resource_group() {
  local rg_id

  ts "==> Ensuring dashboard workspace tags on resource group"
  rg_id="$(az group show -n "$AZURE_RESOURCE_GROUP" --query id -o tsv --only-show-errors 2>/dev/null || true)"
  if [ -z "$rg_id" ]; then
    echo "FATAL: cannot resolve resource group id for '$AZURE_RESOURCE_GROUP'." >&2
    exit 1
  fi
  az tag update \
    --resource-id "$rg_id" \
    --operation Merge \
    --tags \
      "elb-workload-rg=$AZURE_RESOURCE_GROUP" \
      "elb-acr-rg=$AZURE_RESOURCE_GROUP" \
      "elb-acr=$ACR_NAME" \
      "elb-storage=$STORAGE_ACCOUNT_NAME" \
      "elb-region=$AZURE_LOCATION" \
    --only-show-errors >/dev/null
  ts "    ✓ RG tags include workload=$AZURE_RESOURCE_GROUP acr=$ACR_NAME storage=$STORAGE_ACCOUNT_NAME"
}

progress "done" 5 "Resource validation"

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
      --build-arg "APP_VERSION=$(release_version)"
      --build-arg "APP_BUILD_NUMBER=$(release_build_number)"
      --build-arg "GIT_COMMIT=$(short_commit)"
      --build-arg "BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    )
  elif [ "$image_name" = "elb-api" ]; then
    # Bake the release version into the api runtime so `api/__init__.py`
    # surfaces it via `__version__` and the upgrade reconciler can match
    # `__version__ == target_version` on the freshly booted revision.
    extra_args=(
      --build-arg "APP_VERSION=$(release_version)"
      --build-arg "APP_GIT_COMMIT=$(short_commit)"
      --build-arg "APP_BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
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
      --subscription "${AZURE_SUBSCRIPTION_ID}" \
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

build_terminal_image() {
  local pid_var="${1:-}"
  local log="$LOG_DIR/build-elb-terminal.log"
  local extra_args=()
  if [ -z "$pid_var" ]; then
    echo "build_terminal_image: missing pid var" >&2
    return 1
  fi
  {
    echo "[build-elb-terminal] checking terminal base image at $(date -u +%H:%M:%S)"
    ensure_terminal_base_image
    extra_args=(
      --build-arg "TERMINAL_BASE_IMAGE=$(terminal_base_image)"
    )
    echo "[build-elb-terminal] starting at $(date -u +%H:%M:%S)"
    az acr build \
      --registry "$ACR_NAME" \
      --subscription "${AZURE_SUBSCRIPTION_ID}" \
      --image "elb-terminal:${TAG}" \
      --image "elb-terminal:latest" \
      --file "$REPO_ROOT/terminal/Dockerfile.runtime" \
      "${extra_args[@]}" \
      "$REPO_ROOT/terminal" \
      --output none
    rc=$?
    echo "[build-elb-terminal] finished at $(date -u +%H:%M:%S), rc=$rc"
    exit $rc
  } > "$log" 2>&1 &
  printf -v "$pid_var" '%s' "$!"
}

progress step 6 "Image builds" "Build api and frontend immediately; terminal builds after its base image is ready."
ts "==> Building images via az acr build (api/frontend parallel; terminal chains after base)"
acr_ensure_build_access "$ACR_NAME"
build_image PID_API      "elb-api"      "$REPO_ROOT/api/Dockerfile"      "$REPO_ROOT"
build_image PID_FRONTEND "elb-frontend" "$REPO_ROOT/web/Dockerfile"      "$REPO_ROOT"
build_terminal_image PID_TERMINAL

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
progress "done" 6 "Image builds"

# ---------------------------------------------------------------------------
# 1b. Mirror the redis broker image into the workload ACR.
#     `redis:7-alpine` is otherwise pulled from Docker Hub on every revision
#     start; unauthenticated pulls hit HTTP 429 (TOOMANYREQUESTS), the redis
#     sidecar stays ImagePullBackOff, and the whole replica stays NotRunning
#     -> Container Apps keeps routing traffic to the previous revision.
#     Mirroring once (idempotent) lets the sidecar pull via MI from ACR.
# ---------------------------------------------------------------------------
ts "==> Mirroring redis:7-alpine into ACR (idempotent)"
if az acr repository show \
    -n "$ACR_NAME" --image "library/redis:7-alpine" \
    --only-show-errors >/dev/null 2>&1; then
  ts "    ✓ ACR already has library/redis:7-alpine; skipping import"
else
  if az acr import \
      -n "$ACR_NAME" \
      --source "docker.io/library/redis:7-alpine" \
      --image "library/redis:7-alpine" \
      --only-show-errors >/dev/null 2>&1; then
    ts "    ✓ Imported redis:7-alpine into $ACR_NAME"
  else
    cat >&2 <<EOF
FATAL: az acr import for docker.io/library/redis:7-alpine failed.

The redis sidecar in the six-sidecar template pulls from
'$ACR_LOGIN_SERVER/library/redis:7-alpine'. Without this mirror, redis pulls
from Docker Hub unauthenticated and hits HTTP 429 rate limits, leaving the
replica in NotRunning state and pinning traffic to the previous revision.

Retry after a short wait, or run manually:
  az acr import -n $ACR_NAME --source docker.io/library/redis:7-alpine --image library/redis:7-alpine
EOF
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 2. Swap the Container App template to the six-sidecar layout.
#    `az containerapp update --yaml` is much faster than redeploying the
#    Bicep module because it skips template compilation and what-if.
# ---------------------------------------------------------------------------
progress step 7 "Sidecar swap" "Deploy the six-sidecar Container App template over the bootstrap app."
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
ENV_RID="/subscriptions/$SUB_ID/resourceGroups/$AZURE_RESOURCE_GROUP/providers/Microsoft.App/managedEnvironments/${CONTAINER_ENV_NAME:-cae-elb-dashboard}"
PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID_VAL="$(resolve_platform_private_endpoint_subnet_id)"
ts "    Private endpoint subnet: $PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID_VAL"

# Resolve the Live Wall LA workspace from the Container Apps Environment
# itself when the operator's shell did not export LOG_ANALYTICS_WORKSPACE_ID.
# Container Apps strips env entries with empty values, so deploying with an
# empty string here leaves the api sidecar with NO LOG_ANALYTICS_WORKSPACE_ID
# at all — `_use_la_fallback()` returns False and the Live Wall stays blank.
if [ -z "$LOG_ANALYTICS_WORKSPACE_ID_VAL" ]; then
  LOG_ANALYTICS_WORKSPACE_ID_VAL="$(az containerapp env show \
    --name "${CONTAINER_ENV_NAME:-cae-elb-dashboard}" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --query 'properties.appLogsConfiguration.logAnalyticsConfiguration.customerId' \
    -o tsv 2>/dev/null || true)"
  if [ -n "$LOG_ANALYTICS_WORKSPACE_ID_VAL" ]; then
    ts "    Live Wall LA workspace resolved from env: $LOG_ANALYTICS_WORKSPACE_ID_VAL"
  else
    ts "    Live Wall LA workspace: unset (Live Wall tiles will stay blank)"
  fi
fi

# Grant Log Analytics Reader on the workspace the env actually uses. The
# monitoring.bicep grant only covers the workspace it creates — if a prior
# deployment wired the env to a different workspace (e.g. azd template
# regenerated the resource token) the api sidecar would 403 on KQL and the
# Live Wall would stay blank even with the env var set.
if [ -n "$LOG_ANALYTICS_WORKSPACE_ID_VAL" ] && [ -n "${SHARED_IDENTITY_PRINCIPAL_ID:-}" ]; then
  LA_WS_RID="$(az monitor log-analytics workspace list \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --query "[?customerId=='$LOG_ANALYTICS_WORKSPACE_ID_VAL'].id | [0]" \
    -o tsv 2>/dev/null || true)"
  if [ -n "$LA_WS_RID" ]; then
    az role assignment create \
      --assignee-object-id "$SHARED_IDENTITY_PRINCIPAL_ID" \
      --assignee-principal-type ServicePrincipal \
      --role "Log Analytics Reader" \
      --scope "$LA_WS_RID" \
      --only-show-errors >/dev/null 2>&1 || true
    ts "    Log Analytics Reader granted to shared UAMI on $LA_WS_RID"
  fi
fi

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
      sharedIdentityPrincipalId="$SHARED_IDENTITY_PRINCIPAL_ID" \
      tenantId="$AZURE_TENANT_ID" \
      apiClientId="$API_CLIENT_ID_VAL" \
      featureCustomDb="$VITE_FEATURE_CUSTOM_DB_VAL" \
      featureLabTools="$VITE_FEATURE_LAB_TOOLS_VAL" \
      featureTerminal="$VITE_FEATURE_TERMINAL_VAL" \
      applicationInsightsConnectionString="$APPLICATIONINSIGHTS_CONNECTION_STRING_VAL" \
      logAnalyticsWorkspaceId="$LOG_ANALYTICS_WORKSPACE_ID_VAL" \
      platformStorageAccountName="${STORAGE_ACCOUNT_NAME:-}" \
      platformPrivateEndpointSubnetId="$PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID_VAL" \
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
set +e
wait "$SWAP_PID"
SWAP_RC=$?
set -e
if [ "$SWAP_RC" != "0" ]; then
  ts "✗ swap deployment failed (rc=$SWAP_RC). Last lines:"
  tail -30 "$LOG_DIR/swap.log" | sed 's/^/      /'
  exit "$SWAP_RC"
fi
ts "==> Container App updated to six-sidecar layout"
progress "done" 7 "Sidecar swap"

# ---------------------------------------------------------------------------
# 3. Wait for /api/health on the new revision and print URL.
# ---------------------------------------------------------------------------
progress step 8 "Health check" "Poll /api/health so the final URL is not printed before the app is ready."
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
  tag_workspace_resource_group
  progress "done" 8 "Health check"
else
  ts "⚠ Container App deployed but /api/health did not respond 200 within 180s."
  progress note "Step 8 completed with warning: /api/health was not ready within 180s."
  ts "  Check container logs:"
  ts "    az containerapp logs show -n $CONTAINER_APP_NAME -g $AZURE_RESOURCE_GROUP --container api --tail 100"
  ts "  Check system events:"
  ts "    az containerapp logs show -n $CONTAINER_APP_NAME -g $AZURE_RESOURCE_GROUP --type system --tail 30"
fi
ts "  URL:        https://$CONTAINER_APP_FQDN"
ts "  RG:         $AZURE_RESOURCE_GROUP"
ts "  Logs dir:   $LOG_DIR"
echo "============================================================"

# ---------------------------------------------------------------------------
# 4. Workload-cluster RBAC self-heal.
#
# `infra/modules/workloadClusterRoles.bicep` grants the dashboard MI
# Contributor + User Access Administrator on the AKS cluster's RG, but
# only when `aksClusterResourceGroup` is set on the azd env. The first
# `azd up` cannot set it (the RG does not exist yet); the SPA wizard
# creates AKS later. Call `grant-runtime-rbac.sh` here as a self-healing
# safety net so any existing AKS cluster in the same subscription gets
# the required RBAC immediately without an explicit second `azd provision`.
#
# Soft-fail: missing UAA at the cluster-RG scope is expected on first run.
# The operator is told to run the helper by hand in that case.
# ---------------------------------------------------------------------------
RBAC_SCRIPT="$REPO_ROOT/scripts/dev/grant-runtime-rbac.sh"
if [[ -x "$RBAC_SCRIPT" ]]; then
  ts "==> Self-heal: granting workload-cluster RBAC (best-effort)..."
  if "$RBAC_SCRIPT" \
        --container-app "$CONTAINER_APP_NAME" \
        --rg "$AZURE_RESOURCE_GROUP" \
        --subscription "$(az account show --query id -o tsv)" \
        --yes 2>&1 | sed 's/^/    /'; then
    ts "    ✓ workload-cluster RBAC OK"
  else
    rc=$?
    if [[ "$rc" == "3" ]]; then
      ts "    ⓘ workload-cluster RBAC skipped (no AKS cluster yet — re-run after creating AKS)"
    else
      ts "    ⚠ workload-cluster RBAC self-heal failed (rc=$rc)."
      ts "      OpenAPI deploys may need:  scripts/dev/grant-runtime-rbac.sh"
    fi
  fi
fi

# Soft-fail policy: do not break azd up just because health was slow to
# come up. Hard-fail above stays for image-build / swap-deploy errors.
exit 0

#!/usr/bin/env bash
# One-command bootstrap for a fresh clone.

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: ./deploy.sh [--prepare-only]

Checks Azure CLI login, prepares the default azd environment, runs azd up,
and opens the deployed Container App URL.

Environment overrides:
  AZD_ENV_NAME                 Default: elb-dashboard
  AZURE_LOCATION               Default: koreacentral
  LOCKDOWN_PRIVATE_NETWORKING  Default: false
  ALLOWED_ORIGINS              Default: empty / same-origin
  ENABLE_APPLICATION_INSIGHTS  Default: false

USAGE
}

prepare_only=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prepare-only) prepare_only=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

env_name="${AZD_ENV_NAME:-elb-dashboard}"
location="${AZURE_LOCATION:-koreacentral}"
lockdown="${LOCKDOWN_PRIVATE_NETWORKING:-false}"
allowed_origins="${ALLOWED_ORIGINS:-}"
enable_application_insights="${ENABLE_APPLICATION_INSIGHTS:-false}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: $1 is required." >&2
    exit 1
  fi
}

open_url() {
  local url="$1"
  if [[ -z "$url" ]]; then
    return 0
  fi
  echo "==> Opening $url"
  if command -v xdg-open >/dev/null 2>&1; then
    (xdg-open "$url" >/dev/null 2>&1 &)
  elif command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  elif command -v wslview >/dev/null 2>&1; then
    wslview "$url" >/dev/null 2>&1 || true
  else
    echo "==> Browser opener not found. Open this URL manually: $url"
  fi
}

need_cmd az
need_cmd azd

bash "$repo_root/scripts/dev/azd-progress.sh" plan
export ELB_AZD_PROGRESS_PLAN_SHOWN=true
bash "$repo_root/scripts/dev/azd-progress.sh" step 0 "Local bootstrap" "Checking Azure CLI, azd auth, and azd environment values."

if ! az account show -o none >/dev/null 2>&1; then
  echo "==> Azure CLI is not signed in; starting az login..."
  az login >/dev/null
fi

subscription_id="$(az account show --query id -o tsv)"
tenant_id="$(az account show --query tenantId -o tsv)"
account_name="$(az account show --query name -o tsv)"
user_name="$(az account show --query user.name -o tsv)"

echo "==> Azure CLI account"
echo "    User:         $user_name"
echo "    Subscription: $account_name ($subscription_id)"
echo "    Tenant:       $tenant_id"

azd_status="$(azd auth login --check-status 2>&1 || true)"
if [[ "$azd_status" != *"$user_name"* ]]; then
  echo "==> azd is not signed in as the active Azure CLI user; starting device-code login..."
  azd auth login --use-device-code --tenant-id "$tenant_id"
fi

if azd env get-values --environment "$env_name" >/dev/null 2>&1; then
  echo "==> Selecting existing azd environment: $env_name"
  azd env select "$env_name" --no-prompt >/dev/null
else
  echo "==> Creating azd environment: $env_name"
  azd env new "$env_name" \
    --location "$location" \
    --subscription "$subscription_id" \
    --no-prompt >/dev/null
fi

echo "==> Configuring azd environment"
azd env set --environment "$env_name" AZURE_LOCATION "$location" >/dev/null
azd env set --environment "$env_name" AZURE_SUBSCRIPTION_ID "$subscription_id" >/dev/null
azd env set --environment "$env_name" AZURE_TENANT_ID "$tenant_id" >/dev/null
azd env set --environment "$env_name" ALLOWED_ORIGINS "$allowed_origins" >/dev/null
azd env set --environment "$env_name" LOCKDOWN_PRIVATE_NETWORKING "$lockdown" >/dev/null
azd env set --environment "$env_name" ENABLE_APPLICATION_INSIGHTS "$enable_application_insights" >/dev/null

echo "==> Pre-flight provider check (azd up verifies providers again in step 1)"
bash "$repo_root/scripts/dev/register-providers.sh" --subscription "$subscription_id"

echo "==> Target resource group: rg-elb-dashboard"
if [[ "$prepare_only" == "true" ]]; then
  bash "$repo_root/scripts/dev/azd-progress.sh" done 0 "Local bootstrap"
  echo "==> Prepare-only mode complete. Run azd up to deploy."
  exit 0
fi

bash "$repo_root/scripts/dev/azd-progress.sh" done 0 "Local bootstrap"
echo "==> Running azd up"
azd up \
  --environment "$env_name" \
  --location "$location" \
  --subscription "$subscription_id" \
  --no-prompt

app_url="$(azd env get-values --environment "$env_name" 2>/dev/null | awk -F= '/^CONTAINER_APP_URL=/{gsub(/"/, "", $2); print $2; exit}')"
if [[ -z "$app_url" ]]; then
  app_fqdn="$(azd env get-values --environment "$env_name" 2>/dev/null | awk -F= '/^CONTAINER_APP_FQDN=/{gsub(/"/, "", $2); print $2; exit}')"
  if [[ -n "$app_fqdn" ]]; then
    app_url="https://$app_fqdn"
  fi
fi

if [[ -n "$app_url" ]]; then
  echo "==> Deployment complete: $app_url"
  open_url "$app_url"
else
  echo "==> Deployment complete. Run 'azd env get-values --environment $env_name' to inspect outputs."
fi
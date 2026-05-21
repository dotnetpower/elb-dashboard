#!/usr/bin/env bash
# preflight-check.sh — run before `azd up` to catch missing prerequisites
# and surface configuration that the operator must set.
#
# Exits non-zero if any required tool, login, or env var is missing.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

fail=0

tool_version() {
  local cmd="$1"
  local raw=""

  if [ "$cmd" = "azd" ]; then
    raw="$(azd version 2>/dev/null || true)"
  else
    raw="$($cmd --version 2>/dev/null || true)"
  fi
  printf '%s' "${raw%%$'\n'*}"
}

echo "==> Tool versions"
for cmd in az azd jq curl uv; do
  if command -v "$cmd" >/dev/null 2>&1; then
    v="$(tool_version "$cmd")"
    green "  ✓ $cmd: $v"
  else
    red "  ✗ $cmd: not installed"
    fail=1
  fi
done

# Azure CLI ≥ 2.81 (matches the project's pinned minimum).
if command -v az >/dev/null 2>&1; then
  az_ver=$(az version --query '"azure-cli"' -o tsv 2>/dev/null || echo "0.0.0")
  if [ "$(printf '%s\n' "2.81.0" "$az_ver" | sort -V | head -1)" != "2.81.0" ]; then
    yellow "  ! azure-cli is $az_ver; recommend ≥ 2.81.0"
  fi
fi

# azd ≥ 1.10
if command -v azd >/dev/null 2>&1; then
  azd_raw="$(azd version 2>/dev/null || true)"
  azd_ver=$(printf '%s\n' "$azd_raw" | awk 'NR == 1 { print $NF; exit }' | tr -d 'v' || echo "0.0.0")
  echo "    azd: $azd_ver"
fi

echo
echo "==> Azure context"
if az account show -o none >/dev/null 2>&1; then
  sub=$(az account show --query name -o tsv)
  sub_id=$(az account show --query id -o tsv)
  tenant=$(az account show --query tenantId -o tsv)
  upn=$(az account show --query user.name -o tsv)
  green "  ✓ Signed in as: $upn"
  echo "    Subscription: $sub ($sub_id)"
  echo "    Tenant:       $tenant"
else
  red "  ✗ Not signed in. Run: az login"
  fail=1
fi

echo
echo "==> Azure resource providers"
if az account show -o none >/dev/null 2>&1; then
  if bash "$repo_root/scripts/dev/register-providers.sh" --subscription "$sub_id"; then
    green "  ✓ Required providers are registered"
  else
    red "  ✗ Failed to register required providers"
    fail=1
  fi
else
  yellow "  ! Skipped because Azure CLI is not signed in"
fi

echo
echo "==> azd environment"
if azd env get-values >/dev/null 2>&1; then
  env_name=$(azd env get-values | grep '^AZURE_ENV_NAME' | head -1 | cut -d= -f2- | tr -d '"' || echo "")
  loc=$(azd env get-values | grep '^AZURE_LOCATION' | head -1 | cut -d= -f2- | tr -d '"' || echo "")
  api_cid=$(azd env get-values | grep '^API_CLIENT_ID' | head -1 | cut -d= -f2- | tr -d '"' || echo "")
  green "  ✓ Active env: $env_name (location=$loc)"
  if [ -z "$api_cid" ]; then
    yellow "  ! API_CLIENT_ID not set yet — azd up will create/reuse the App Registration during postprovision."
  else
    green "  ✓ API_CLIENT_ID: $api_cid"
  fi
else
  yellow "  ! No active azd environment. Run: azd env new <name>"
fi

echo
if [ "$fail" = "0" ]; then
  green "Preflight OK — you can run \`azd up\`."
else
  red   "Preflight FAILED — fix the items marked ✗ above and re-run."
  exit 1
fi

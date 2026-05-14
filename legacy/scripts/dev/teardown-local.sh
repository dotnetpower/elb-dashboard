#!/usr/bin/env bash
# Tear down everything created by the local dev bootstrap:
#   - RG hosting Remote Terminal VM
#   - RG hosting Key Vault (platform)
#   - Optional: workload + ACR RGs created by the user during testing
#   - App Registration referenced by api/local.settings.json
#   - Local Azurite container
#
# Usage:
#   scripts/dev/teardown-local.sh [--include-workload]
#
# Pass --include-workload to also delete rg-elb / rg-elbacr (created during the
# README walkthrough). By default only the platform + terminal RGs are deleted.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INCLUDE_WORKLOAD=false
for arg in "$@"; do
  case "$arg" in
    --include-workload) INCLUDE_WORKLOAD=true ;;
  esac
done

if ! command -v az >/dev/null 2>&1; then
  echo "ERROR: Azure CLI (az) is required." >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required (sudo apt install jq)." >&2
  exit 1
fi

api_settings="$repo_root/api/local.settings.json"
app_id=""
if [[ -f "$api_settings" ]]; then
  app_id="$(jq -r '.Values.API_CLIENT_ID // ""' "$api_settings")"
fi

echo "==> Triggering RG deletes (async)..."
RGS=("rg-elb-platform" "rg-elb-terminal")
if [[ "$INCLUDE_WORKLOAD" == "true" ]]; then
  RGS+=("rg-elb" "rg-elbacr")
fi
for rg in "${RGS[@]}"; do
  if az group show -n "$rg" >/dev/null 2>&1; then
    az group delete -n "$rg" --yes --no-wait -o none
    echo "   - $rg deleting"
  else
    echo "   - $rg not present"
  fi
done

if [[ -n "$app_id" && "$app_id" != "null" ]]; then
  echo "==> Deleting App Registration $app_id ..."
  az ad app delete --id "$app_id" 2>&1 | tail -3 || true
fi

echo "==> Stopping Azurite container (if running)..."
docker rm -f azurite-elb 2>/dev/null || true

cat <<EOF

============================================================
 Teardown triggered.
 - RG deletes are asynchronous; check progress with:
     az group list --query "[?starts_with(name,'rg-elb')].{name:name,state:properties.provisioningState}" -o table
 - Local files left behind (delete manually if you want a clean repo):
     api/local.settings.json
     web/.env.local
     ~/.azurite
============================================================
EOF

#!/usr/bin/env bash
# Recover compatible soft-deleted Key Vaults before Bicep creates resources.

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/recover-deleted-keyvault.sh [--subscription <id-or-name>] [--environment <azd-env>]

If a previous failed numbered deployment left this environment's Key Vault in
Azure soft-delete, recover it before `azd provision` so deterministic names can
be reused. Only vaults whose deleted vaultId points at the selected target
resource group and whose tags identify this elb-dashboard azd environment are
recovered.

USAGE
}

subscription_arg=()
environment_name="${AZURE_ENV_NAME:-${AZD_ENV_NAME:-}}"
repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subscription)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --subscription requires a subscription id or name." >&2
        exit 1
      fi
      subscription_arg=(--subscription "$2")
      shift 2
      ;;
    --environment)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --environment requires an azd environment name." >&2
        exit 1
      fi
      environment_name="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: $1 is required." >&2
    exit 1
  }
}

azd_env_value() {
  local key="$1"
  local env_file="${repo_root}/.azure/${environment_name}/.env"
  if [[ -n "$environment_name" && -f "$env_file" ]]; then
    awk -F= -v key="$key" '$1 == key {gsub(/"/, "", $2); print $2; exit}' "$env_file"
    return 0
  fi
  if command -v azd >/dev/null 2>&1; then
    local azd_args=()
    if [[ -n "$environment_name" ]]; then
      azd_args=(--environment "$environment_name")
    fi
    azd env get-values "${azd_args[@]}" 2>/dev/null | \
      awk -F= -v key="$key" '$1 == key {gsub(/"/, "", $2); print $2; exit}'
  fi
}

need az
need jq

slot="${ELB_RESOURCE_NAME_SLOT:-}"
if [[ -z "$slot" ]]; then
  slot="$(azd_env_value ELB_RESOURCE_NAME_SLOT || true)"
fi

if [[ -n "$slot" && ! "$slot" =~ ^slot[0-9][0-9]$ ]]; then
  echo "ERROR: ELB_RESOURCE_NAME_SLOT must be empty or look like slot01." >&2
  exit 1
fi

target_rg="rg-elb-dashboard"
if [[ -n "$slot" ]]; then
  target_rg="${target_rg}-${slot#slot}"
fi

deleted_json="$(az keyvault list-deleted "${subscription_arg[@]}" -o json --only-show-errors 2>/dev/null || printf '[]')"
candidates="$(jq -c \
  --arg rg_segment "/resourceGroups/${target_rg}/" \
  --arg env_name "$environment_name" \
  '[.[]
    | select(((.properties.vaultId // "") | contains($rg_segment)))
    | select((.properties.tags.app // "") == "elb-dashboard")
    | select(($env_name == "") or ((.properties.tags["azd-env-name"] // "") == $env_name))
    | select((.properties.tags.role // "") == "secrets")
  ]' <<<"$deleted_json")"

count="$(jq 'length' <<<"$candidates")"
if [[ "$count" == "0" ]]; then
  echo "==> No compatible soft-deleted Key Vault found for $target_rg."
  exit 0
fi

# `az keyvault recover --resource-group $target_rg` fails with
# ResourceGroupNotFound when the target RG does not exist yet. On a
# fresh-clone deploy that is the common case: Bicep is about to create the
# RG, but the preprovision hook runs before Bicep. Create the RG up front
# so the recover call succeeds; Bicep's RG resource is idempotent and will
# reuse the same RG (provisioningState: Succeeded). Location defaults to
# AZURE_LOCATION (azd env), then $location_arg from the deleted KV, then
# koreacentral as last resort.
if ! az group exists "${subscription_arg[@]}" --name "$target_rg" -o tsv 2>/dev/null | grep -qx true; then
  rg_location="${AZURE_LOCATION:-}"
  if [[ -z "$rg_location" ]]; then
    rg_location="$(azd_env_value AZURE_LOCATION || true)"
  fi
  rg_location="${rg_location:-koreacentral}"
  echo "==> Creating target resource group $target_rg in $rg_location so recover can target it."
  az group create \
    "${subscription_arg[@]}" \
    --name "$target_rg" \
    --location "$rg_location" \
    --tags app=elb-dashboard managedBy=azd repo=elb-dashboard \
    --only-show-errors \
    -o none
fi

echo "==> Recovering $count compatible soft-deleted Key Vault(s) for $target_rg."
jq -c '.[]' <<<"$candidates" | while IFS= read -r vault; do
  name="$(jq -r '.name' <<<"$vault")"
  location="$(jq -r '.properties.location' <<<"$vault")"
  if [[ -z "$name" || "$name" == "null" || -z "$location" || "$location" == "null" ]]; then
    echo "ERROR: deleted Key Vault payload is missing name/location: $vault" >&2
    exit 1
  fi
  echo "    recovering $name in $location"
  az keyvault recover \
    "${subscription_arg[@]}" \
    --name "$name" \
    --resource-group "$target_rg" \
    --location "$location" \
    --only-show-errors \
    -o none
done

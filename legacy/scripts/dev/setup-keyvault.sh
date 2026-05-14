#!/usr/bin/env bash
# Provision (or reuse) the Key Vault used by the API for VM passwords + grant
# the signed-in user + (optionally) the Function App MI access via RBAC.
#
# Usage:
#   scripts/dev/setup-keyvault.sh [VAULT_NAME] [REGION] [RESOURCE_GROUP]
#
# Defaults:
#   VAULT_NAME      = kv-elb-<8-char-hash-of-tenant>
#   REGION          = koreacentral
#   RESOURCE_GROUP  = rg-elb-platform
#
# What this does (idempotent):
#   1. Creates the resource group if missing.
#   2. Creates the Key Vault (RBAC-enabled, soft-delete + purge protection).
#   3. Grants the signed-in user `Key Vault Secrets Officer` on the vault.
#   4. Writes KEY_VAULT_URI into api/local.settings.json.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if ! command -v az >/dev/null 2>&1; then
  echo "ERROR: Azure CLI (az) is required." >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required (sudo apt install jq)." >&2
  exit 1
fi

tenant_id="$(az account show --query tenantId -o tsv)"
caller_oid="$(az ad signed-in-user show --query id -o tsv)"
sub_id="$(az account show --query id -o tsv)"

short_hash="$(printf '%s' "$tenant_id" | sha1sum | cut -c1-8)"
VAULT_NAME="${1:-kv-elb-$short_hash}"
REGION="${2:-koreacentral}"
RG="${3:-rg-elb-platform}"

echo "==> Tenant   : $tenant_id"
echo "==> Sub      : $sub_id"
echo "==> User OID : $caller_oid"
echo "==> Vault    : $VAULT_NAME"
echo "==> Region   : $REGION"
echo "==> RG       : $RG"

# --- 1. Resource group ---
az group create --name "$RG" --location "$REGION" --output none

# --- 2. Key Vault (RBAC mode) ---
if ! az keyvault show --name "$VAULT_NAME" --resource-group "$RG" >/dev/null 2>&1; then
  echo "==> Creating Key Vault..."
  az keyvault create \
    --name "$VAULT_NAME" \
    --resource-group "$RG" \
    --location "$REGION" \
    --enable-rbac-authorization true \
    --enable-purge-protection true \
    --retention-days 7 \
    --output none
else
  echo "==> Reusing existing Key Vault"
fi

vault_uri="$(az keyvault show --name "$VAULT_NAME" --resource-group "$RG" --query properties.vaultUri -o tsv)"
vault_id="$(az keyvault show --name "$VAULT_NAME" --resource-group "$RG" --query id -o tsv)"

# --- 3. RBAC for caller ---
echo "==> Granting 'Key Vault Secrets Officer' to caller..."
az role assignment create \
  --assignee-object-id "$caller_oid" \
  --assignee-principal-type User \
  --role "Key Vault Secrets Officer" \
  --scope "$vault_id" \
  --output none 2>/dev/null || echo "    (already assigned)"

# --- 4. Update api/local.settings.json ---
api_settings="$repo_root/api/local.settings.json"
if [[ -f "$api_settings" ]]; then
  tmp="$(mktemp)"
  jq --arg uri "$vault_uri" '.Values.KEY_VAULT_URI = $uri' "$api_settings" > "$tmp"
  mv "$tmp" "$api_settings"
  echo "==> Wrote KEY_VAULT_URI=$vault_uri to $api_settings"
else
  echo "WARN: $api_settings not found — run setup-app-registration.sh first."
fi

cat <<EOF

============================================================
 Done.
 Vault URI : $vault_uri
============================================================
EOF

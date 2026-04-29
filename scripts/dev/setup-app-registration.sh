#!/usr/bin/env bash
# Provision (or reuse) the Microsoft Entra App Registration used by the SPA + API.
#
# Usage:
#   scripts/dev/setup-app-registration.sh [APP_NAME] [REDIRECT_URI]
#
# Defaults:
#   APP_NAME      = elastic-blast-control-plane
#   REDIRECT_URI  = http://localhost:8090
#
# What this does (idempotent):
#   1. Creates (or reuses) the App Registration.
#   2. Sets the Application ID URI to api://<appId>.
#   3. Exposes a delegated scope `user_impersonation` (admin + user consent).
#   4. Adds the SPA redirect URI (Auth Code + PKCE).
#   5. Adds delegated permissions:
#        - The app's own `user_impersonation` scope.
#        - Azure Service Management `user_impersonation` (so backend OBO can call ARM).
#   6. Writes web/.env.local and api/local.settings.json with the resolved values.
#   7. Prints the admin-consent URL.
set -euo pipefail

APP_NAME="${1:-elastic-blast-control-plane}"
REDIRECT_URI="${2:-http://localhost:8090}"

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
if [[ -z "$tenant_id" ]]; then
  echo "ERROR: not signed in to az. Run 'az login' first." >&2
  exit 1
fi

echo "==> Tenant: $tenant_id"
echo "==> App name: $APP_NAME"
echo "==> Redirect URI: $REDIRECT_URI"

# --- 1. Create or reuse the App Registration ---
app_id="$(az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv || true)"
if [[ -z "$app_id" || "$app_id" == "null" ]]; then
  echo "==> Creating App Registration..."
  app_id="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
else
  echo "==> Reusing existing App Registration appId=$app_id"
fi

object_id="$(az ad app show --id "$app_id" --query id -o tsv)"

# --- 2. Identifier URI ---
identifier_uri="api://${app_id}"
echo "==> Setting identifier URI: $identifier_uri"
az ad app update --id "$app_id" --identifier-uris "$identifier_uri" >/dev/null

# --- 3. Expose `user_impersonation` scope ---
scope_id="$(az ad app show --id "$app_id" --query "api.oauth2PermissionScopes[?value=='user_impersonation'].id | [0]" -o tsv)"
if [[ -z "$scope_id" || "$scope_id" == "null" ]]; then
  scope_id="$(uuidgen)"
  echo "==> Adding 'user_impersonation' scope ($scope_id)"
  api_payload=$(cat <<JSON
{
  "api": {
    "requestedAccessTokenVersion": 2,
    "oauth2PermissionScopes": [
      {
        "id": "$scope_id",
        "adminConsentDescription": "Allow the app to access ElasticBLAST control plane on behalf of the signed-in user.",
        "adminConsentDisplayName": "Access ElasticBLAST control plane",
        "userConsentDescription": "Allow the app to access ElasticBLAST control plane on your behalf.",
        "userConsentDisplayName": "Access ElasticBLAST control plane",
        "value": "user_impersonation",
        "type": "User",
        "isEnabled": true
      }
    ]
  }
}
JSON
)
  az rest --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/applications/$object_id" \
    --headers "Content-Type=application/json" \
    --body "$api_payload" >/dev/null
else
  echo "==> Reusing scope id=$scope_id"
fi

# --- 4. SPA redirect URI ---
echo "==> Configuring SPA redirect URI"
spa_payload=$(cat <<JSON
{
  "spa": { "redirectUris": ["$REDIRECT_URI"] },
  "web": { "redirectUris": [] }
}
JSON
)
az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications/$object_id" \
  --headers "Content-Type=application/json" \
  --body "$spa_payload" >/dev/null

# --- 5. Required resource access (own scope + ARM user_impersonation) ---
arm_app_id="797f4846-ba00-4fd7-ba43-dac1f8f63013"   # Azure Service Management
arm_scope_id="41094075-9dad-400e-a0bd-54e686782033" # user_impersonation on ARM
echo "==> Configuring required resource access (own scope + ARM)"
required_payload=$(cat <<JSON
{
  "requiredResourceAccess": [
    {
      "resourceAppId": "$app_id",
      "resourceAccess": [
        { "id": "$scope_id", "type": "Scope" }
      ]
    },
    {
      "resourceAppId": "$arm_app_id",
      "resourceAccess": [
        { "id": "$arm_scope_id", "type": "Scope" }
      ]
    }
  ]
}
JSON
)
az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications/$object_id" \
  --headers "Content-Type=application/json" \
  --body "$required_payload" >/dev/null

# --- 6. Write local env files ---
web_env="$repo_root/web/.env.local"
echo "==> Writing $web_env"
cat > "$web_env" <<EOF
VITE_API_BASE_URL=http://localhost:7071
VITE_AZURE_TENANT_ID=$tenant_id
VITE_AZURE_CLIENT_ID=$app_id
VITE_AZURE_REDIRECT_URI=$REDIRECT_URI
EOF

api_settings="$repo_root/api/local.settings.json"
if [[ ! -f "$api_settings" ]]; then
  echo "==> Writing $api_settings (template — set API_CLIENT_SECRET and KEY_VAULT_URI manually)"
  cat > "$api_settings" <<EOF
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AZURE_TENANT_ID": "$tenant_id",
    "API_CLIENT_ID": "$app_id",
    "API_CLIENT_SECRET": "",
    "KEY_VAULT_URI": "",
    "TERMINAL_DEFAULT_RG": "rg-elb-terminal",
    "TERMINAL_DEFAULT_REGION": "koreacentral",
    "AUTH_DEV_BYPASS": "false"
  },
  "Host": {
    "CORS": "*",
    "CORSCredentials": false
  }
}
EOF
else
  echo "==> $api_settings already exists — leaving untouched. Verify AZURE_TENANT_ID / API_CLIENT_ID."
fi

# --- 7. Admin consent URL ---
consent_url="https://login.microsoftonline.com/${tenant_id}/adminconsent?client_id=${app_id}&redirect_uri=${REDIRECT_URI}"

cat <<EOF

============================================================
 Done.
 Tenant   : $tenant_id
 App ID   : $app_id
 Scope    : ${identifier_uri}/user_impersonation
 Redirect : $REDIRECT_URI
============================================================

Next steps:
  1. (Recommended) Grant admin consent so users do not need to consent each time:
     $consent_url
  2. Restart the web dev server so .env.local is picked up:
     cd web && npm run dev
  3. To enable the backend (api/), still need to:
       a. Create a client secret:
          az ad app credential reset --id $app_id --append --display-name dev-secret --years 1 \\
            --query password -o tsv
          # paste the output as API_CLIENT_SECRET in api/local.settings.json
       b. Provision a Key Vault and set KEY_VAULT_URI in api/local.settings.json.
EOF

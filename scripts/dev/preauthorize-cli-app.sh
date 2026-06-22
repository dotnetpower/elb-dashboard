#!/usr/bin/env bash
set -euo pipefail

# preauthorize-cli-app.sh — pre-authorize the Azure CLI public client on the
# ElasticBLAST dashboard API app registration.
#
# WHY
#   The Service Bus completion event's result_files[].download_url points at the
#   dashboard's authenticated streaming gateway
#   (GET /api/v1/elastic-blast/jobs/{id}/files/{file_id}) and requires a bearer
#   token for aud=api://<api-client-id>. A consumer acquires it with
#   `az account get-access-token --resource <api-client-id>`. That call fails
#   with AADSTS65001 ("the user or administrator has not consented") unless the
#   well-known Azure CLI public client is pre-authorized for the app's
#   `user_impersonation` scope. This script adds exactly that pre-authorization
#   and nothing else, so the download starts returning bytes instead of 401.
#
# WHO RUNS THIS
#   An Entra administrator with Application.ReadWrite.All (Application
#   Administrator / Cloud Application Administrator) or an owner of the API app
#   registration. A developer who lacks that role hands this script to such an
#   admin: it touches only the single named app registration, is idempotent, and
#   supports --dry-run so the admin can review the exact change first.
#
# USAGE
#   ./preauthorize-cli-app.sh <api-client-id> [tenant-id] [--dry-run]
#   API_CLIENT_ID=<id> [AZURE_TENANT_ID=<tid>] ./preauthorize-cli-app.sh [--dry-run]
#
#   The admin must `az login` first (add `--tenant <tenant-id>` for a guest /
#   multi-tenant account). Passing tenant-id makes the script assert the active
#   az context matches before changing anything.

AZURE_CLI_APP_ID="04b07795-8ddb-461a-bbee-02f9e1bf7b46" # well-known Azure CLI public client
SCOPE_VALUE="user_impersonation"

# --- Parse args (positional api-client-id / tenant-id + optional --dry-run) ---
dry_run=false
positional=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=true ;;
    -h | --help)
      sed -n '3,33p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    --*)
      echo "ERROR: unknown option '$arg'." >&2
      exit 2
      ;;
    *) positional+=("$arg") ;;
  esac
done
api_client_id="${positional[0]:-${API_CLIENT_ID:-}}"
tenant_id="${positional[1]:-${AZURE_TENANT_ID:-}}"

if [[ -z "$api_client_id" ]]; then
  echo "ERROR: API client id is required." >&2
  echo "Usage: $0 <api-client-id> [tenant-id] [--dry-run]" >&2
  exit 2
fi

command -v az >/dev/null || {
  echo "ERROR: Azure CLI (az) not found on PATH." >&2
  exit 2
}
command -v jq >/dev/null || {
  echo "ERROR: jq not found on PATH." >&2
  exit 2
}

# --- Confirm an active az login (and the right tenant, when asserted) ---
if ! account_json="$(az account show -o json 2>/dev/null)"; then
  echo "ERROR: not logged in. Run 'az login${tenant_id:+ --tenant '"$tenant_id"'}' first." >&2
  exit 2
fi
active_tenant="$(jq -r '.tenantId' <<<"$account_json")"
active_user="$(jq -r '.user.name // "unknown"' <<<"$account_json")"
if [[ -n "$tenant_id" && "${active_tenant,,}" != "${tenant_id,,}" ]]; then
  echo "ERROR: active az tenant ($active_tenant) != requested tenant ($tenant_id)." >&2
  echo "       Run 'az login --tenant $tenant_id' and retry." >&2
  exit 2
fi
echo "==> Signed in as $active_user (tenant $active_tenant)"

# --- Resolve the app registration + the user_impersonation scope ---
if ! app_json="$(az ad app show --id "$api_client_id" -o json 2>/dev/null)"; then
  echo "ERROR: app registration '$api_client_id' not found in tenant $active_tenant." >&2
  echo "       Check the API client id, or your account may not have read access to it." >&2
  exit 1
fi
object_id="$(jq -r '.id' <<<"$app_json")"
app_display="$(jq -r '.displayName // "?"' <<<"$app_json")"
scope_id="$(jq -r --arg v "$SCOPE_VALUE" \
  '[.api.oauth2PermissionScopes[]? | select(.value==$v) | .id][0] // ""' <<<"$app_json")"
echo "==> Target app: $app_display ($api_client_id) objectId=$object_id"

if [[ -z "$scope_id" || "$scope_id" == "null" ]]; then
  echo "ERROR: the app does not expose a '$SCOPE_VALUE' delegated scope." >&2
  echo "       Create it first with scripts/dev/setup-app-registration.sh." >&2
  exit 1
fi
echo "==> Found '$SCOPE_VALUE' scope id=$scope_id"

# --- Idempotency: already pre-authorized? ---
already="$(jq -r --arg cli "$AZURE_CLI_APP_ID" --arg sid "$scope_id" '
  [.api.preAuthorizedApplications[]?
   | select(.appId==$cli)
   | select((.delegatedPermissionIds // []) | index($sid))] | length' <<<"$app_json")"
if [[ "$already" -ge 1 ]]; then
  echo "==> Azure CLI ($AZURE_CLI_APP_ID) is already pre-authorized for '$SCOPE_VALUE'."
  echo "    Nothing to do."
  exit 0
fi

# --- Build a staleness-safe full `api` payload ---
# Graph PATCH replaces the provided `api` sub-properties, so we must carry the
# exposed scope forward or it would be wiped. Reconstruct the user_impersonation
# scope from scope_id if the read-back is somehow empty, preserve any other
# scopes / pre-authorized apps, and dedupe a prior Azure CLI entry. Idempotent.
payload="$(jq -cn \
  --argjson scopes "$(jq -c '.api.oauth2PermissionScopes // []' <<<"$app_json")" \
  --argjson preauth "$(jq -c '.api.preAuthorizedApplications // []' <<<"$app_json")" \
  --arg cli "$AZURE_CLI_APP_ID" \
  --arg scope "$scope_id" \
  '
  ($scopes // []) as $existing
  | ($existing | map(select(.value != "user_impersonation"))) as $others
  | (($existing | map(select(.value == "user_impersonation")))[0] // {
      id: $scope,
      adminConsentDescription: "Allow the app to access ElasticBLAST control plane on behalf of the signed-in user.",
      adminConsentDisplayName: "Access ElasticBLAST control plane",
      userConsentDescription: "Allow the app to access ElasticBLAST control plane on your behalf.",
      userConsentDisplayName: "Access ElasticBLAST control plane",
      value: "user_impersonation",
      type: "User",
      isEnabled: true
    }) as $imp
  | {api: {
      requestedAccessTokenVersion: 2,
      oauth2PermissionScopes: ($others + [$imp]),
      preAuthorizedApplications: (
        (($preauth // []) | map(select(.appId != $cli)))
        + [{ appId: $cli, delegatedPermissionIds: [$imp.id] }]
      )
   }}')"

if [[ "$dry_run" == "true" ]]; then
  echo "==> --dry-run: would PATCH https://graph.microsoft.com/v1.0/applications/$object_id"
  echo "    with the following body (no change applied):"
  jq . <<<"$payload"
  exit 0
fi

# --- Apply ---
echo "==> Patching app registration (adds Azure CLI $AZURE_CLI_APP_ID -> $SCOPE_VALUE)"
err_log="$(mktemp)"
if ! az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications/$object_id" \
  --headers "Content-Type=application/json" \
  --body "$payload" >/dev/null 2>"$err_log"; then
  echo "ERROR: PATCH failed — you likely lack write access to this app registration." >&2
  echo "       Required: Application Administrator / Cloud Application Administrator," >&2
  echo "       or ownership of the app registration. Azure CLI said:" >&2
  sed 's/^/       /' "$err_log" >&2 || true
  rm -f "$err_log"
  exit 1
fi
rm -f "$err_log"

# --- Verify (Entra replication can lag a few seconds) ---
verify="$(az ad app show --id "$api_client_id" \
  --query "api.preAuthorizedApplications[?appId=='$AZURE_CLI_APP_ID'].delegatedPermissionIds | [0]" \
  -o json 2>/dev/null || echo '[]')"
if jq -e --arg sid "$scope_id" 'index($sid) != null' <<<"$verify" >/dev/null 2>&1; then
  echo "==> SUCCESS: Azure CLI is now pre-authorized for '$SCOPE_VALUE'."
else
  echo "WARNING: PATCH succeeded but the grant is not visible yet (replication lag)." >&2
  echo "         Re-run this script in a minute to confirm." >&2
fi

cat <<EOF

============================================================
 Done — Azure CLI pre-authorized on '$app_display'
 App ID : $api_client_id
 Scope  : api://$api_client_id/$SCOPE_VALUE
============================================================

A Service Bus result-download consumer can now mint a token without a consent
prompt. Smoke-test token acquisition:

  az account get-access-token --resource $api_client_id --query expiresOn -o tsv

Then run the example consumer end-to-end:

  ELB_API_CLIENT_ID=$api_client_id \\
    python example/servicebus/consume.py --source completions --download --download-dir ./out
EOF
